#!/usr/bin/env python3
"""Compare idle, streaming reads and streaming writes on DIMM1's R1/R5 inputs."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout, suppress
import csv
from datetime import datetime, timezone
import math
import os
from pathlib import Path
import platform
import statistics
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from quarch_capture.common import device_lock, finalize_run, interval_seconds, termination_guard, utc_now, write_json_atomic
from quarch_capture.dram import main as capture_dram
from quarch_capture.energy import energy_from_csv

CHANNELS = ("POWER_1", "POWER_2", "Tot")
ORDER = ("idle", "read", "write")


def memory_state():
    values = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
    swap = {k: int(v) for k, v in (line.split() for line in Path("/proc/vmstat").read_text().splitlines())
            if k in ("pswpin", "pswpout")}
    return {"available_bytes": int(values["MemAvailable"].split()[0]) * 1024, **swap}


def cpu_layout(threads):
    """Use separate physical cores for load and QIS; change only our own affinity."""
    allowed = set(os.sched_getaffinity(0))
    cores = {}
    for cpu in sorted(allowed):
        topology = Path(f"/sys/devices/system/cpu/cpu{cpu}/topology")
        key = tuple((topology / name).read_text().strip() for name in ("physical_package_id", "core_id"))
        cores.setdefault(key, []).append(cpu)
    if threads >= len(cores):
        raise ValueError("Reduce --threads to reserve at least one physical core for QIS")
    load = list(cores.values())[1:threads + 1]
    control = sorted(allowed - {cpu for group in load for cpu in group})
    return [group[0] for group in load], control


def memory_worker(buffer, mode, stop, cpu=None):
    import numpy as np
    if cpu is not None:
        os.sched_setaffinity(0, {cpu})
    passes, checksum = 0, 0
    began = time.monotonic()
    while not stop.is_set():
        if mode == "read":
            checksum ^= int(buffer.sum(dtype=np.uint64))
        else:
            buffer.fill(1 + (passes & 1))
        passes += 1
    return {"passes": passes, "logical_bytes": passes * buffer.nbytes,
            "elapsed_s": time.monotonic() - began, "read_checksum": checksum}


def capture_phase(args, buffers, mode, output):
    stop = threading.Event()
    workers = []
    with ThreadPoolExecutor(max_workers=args.threads) as executor:
        try:
            if mode != "idle":
                workers = [executor.submit(memory_worker, b, mode, stop, cpu)
                           for b, cpu in zip(buffers, args.worker_cpus, strict=True)]
            time.sleep(args.warmup)
            with output.with_suffix(".capture.log").open("x") as log, redirect_stdout(log):
                result = capture_dram(["--device", args.device, "--channel", args.channel,
                                       "--seconds", str(args.seconds), "--resample", args.resample,
                                       "--shunt-mohm", *(str(v) for v in args.shunt_mohm),
                                       "--output-dir", str(output)])
            if result:
                raise RuntimeError("DRAM capture failed")
        finally:
            stop.set()
    return [worker.result() for worker in workers]


def summarize(rows):
    baselines = {(r["run"], r["channel"]): r["mean_power_W"] for r in rows if r["phase"] == "idle"}
    result = []
    for mode in ORDER:
        for channel in CHANNELS:
            selected = [r for r in rows if (r["phase"], r["channel"]) == (mode, channel)]
            power = [r["mean_power_W"] for r in selected]
            baseline = statistics.mean(baselines[r["run"], channel] for r in selected)
            delta = statistics.mean(r["mean_power_W"] - baselines[r["run"], channel] for r in selected)
            result.append({"phase": mode, "channel": channel, "runs": len(selected),
                           "mean_power_W": statistics.mean(power),
                           "stdev_power_W": statistics.stdev(power) if len(power) > 1 else 0,
                           "mean_duration_s": statistics.mean(r["duration_s"] for r in selected),
                           "mean_energy_J": statistics.mean(r["energy_J"] for r in selected),
                           "delta_vs_paired_idle_W": delta,
                           "delta_vs_paired_idle_pct": delta / baseline * 100 if baseline else None,
                           "mean_incremental_energy_J": statistics.mean(
                               r["energy_J"] - baselines[r["run"], channel] * r["duration_s"] for r in selected)})
    return result


def write_csv(path, rows):
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


@termination_guard()
def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", required=True)
    parser.add_argument("--channel", required=True, choices=["Tot"], help="Summarize connected CH1 + CH2; both remain separate")
    parser.add_argument("--seconds", type=float, default=15, help="Device capture duration per phase")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--buffer-mib", type=int, default=1024, help="Total resident buffer, split among workers")
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--warmup", type=float, default=2)
    parser.add_argument("--pause", type=float, default=5)
    parser.add_argument("--resample", default="10ms")
    parser.add_argument("--shunt-mohm", nargs=2, type=float, default=[5, 5])
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args(argv)
    period = interval_seconds(args.resample)
    if (not math.isfinite(args.seconds) or args.seconds < 2 * period or args.runs < 1
            or args.threads < 1 or args.threads > len(os.sched_getaffinity(0))
            or args.buffer_mib < args.threads or args.buffer_mib % args.threads
            or any(not math.isfinite(v) or v < 0 for v in (args.warmup, args.pause))
            or any(not math.isfinite(v) or v <= 0 for v in args.shunt_mohm)):
        parser.error("Use positive sizes/counts/shunts, a buffer divisible by threads, and finite durations")
    try:
        args.worker_cpus, control_cpus = cpu_layout(args.threads)
    except ValueError as exc:
        parser.error(str(exc))
    with device_lock(args.device):
        pass  # Fail before allocating memory if a recorder already owns the device.
    initial_memory = memory_state()
    if args.buffer_mib * 1024 ** 2 > initial_memory["available_bytes"] // 4:
        parser.error("Buffer exceeds one quarter of currently available memory")
    import numpy as np
    from quarchpy.qis import closeQis, isQisRunning, startLocalQis
    output = (args.output_dir or ROOT / "results/dram_memory" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")).resolve()
    output.mkdir(parents=True, exist_ok=False)
    report = {"status": "running", "started_at": utc_now(), "device": args.device,
              "requested_seconds": args.seconds, "interval_s": period, "runs_per_phase": args.runs,
              "buffer_mib": args.buffer_mib, "threads": args.threads, "warmup_s": args.warmup, "pause_s": args.pause,
              "worker_cpus": args.worker_cpus, "control_cpus": control_cpus,
              "shunt_mohm": args.shunt_mohm, "python": platform.python_version(), "numpy": np.__version__,
              "host": platform.uname()._asdict(), "memory_before": initial_memory, "phases": [],
              "scope": "DIMM1 riser R1/R5 only; electrical rail names and complete DRAM coverage unverified",
              "method": "Same prefaulted buffers remain resident in all phases; three-phase cyclic order rotates each run",
              "read": "uint64 NumPy sum, no buffer writes during load",
              "write": "uint64 NumPy fill, alternating 1/2; ordinary stores may also read cache lines",
              "placement": "OS page placement; no physical DIMM pinning",
              "throughput_scope": "Logical buffer bytes over full worker lifetime including warmup and capture setup/export, not DRAM bus traffic"}
    write_json_atomic(output / "run.json", report)
    rows, qis = [], None
    owns_qis = False
    original_affinity = os.sched_getaffinity(0)
    try:
        os.sched_setaffinity(0, control_cpus)
        buffers = [np.empty(args.buffer_mib * 1024 ** 2 // args.threads // 8, dtype=np.uint64) for _ in range(args.threads)]
        for buffer in buffers:
            buffer.fill(1)  # Allocate physical pages before measuring any phase.
        owns_qis = not isQisRunning()
        qis = startLocalQis(headless=True, timeout=30)
        if qis is None:
            raise RuntimeError("Could not start headless QIS")
        report["qis_version"] = qis.send_command("$version")
        report["qis_started_by_benchmark"] = owns_qis
        for run in range(1, args.runs + 1):
            order = ORDER[(run - 1) % 3:] + ORDER[:(run - 1) % 3]
            for mode in order:
                print(f"{utc_now()} run {run}/{args.runs}: {mode}, {args.seconds:g}s", flush=True)
                memory_before = memory_state()
                phase = output / f"r{run:02d}_{mode}"
                workloads = capture_phase(args, buffers, mode, phase)
                measures = {ch: energy_from_csv(phase / "raw.csv", channel=ch, expected_duration_s=args.seconds) for ch in CHANNELS}
                if any(not m["requested_duration_matches"] for m in measures.values()):
                    raise RuntimeError("Incomplete phase duration")
                for ch, measure in measures.items():
                    rows.append({"run": run, "phase": mode, "channel": ch, "samples": measure["samples"],
                                 "duration_s": measure["duration_s"], "interval_s": measure["interval_s"],
                                 "mean_power_W": measure[ch]["mean_power_W"], "energy_J": measure[ch]["energy_J"],
                                 "energy_Wh": measure[ch]["energy_Wh"], "raw_csv": str((phase / "raw.csv").relative_to(output))})
                memory_after = memory_state()
                if any(memory_after[k] != initial_memory[k] for k in ("pswpin", "pswpout")):
                    raise RuntimeError("Host swapping detected; reject this memory comparison")
                report["phases"].append({"run": run, "phase": mode, "directory": phase.name,
                                         "workers": workloads, "memory_before": memory_before, "memory_after": memory_after})
                write_csv(output / "comparison.csv", rows)
                write_json_atomic(output / "run.json", report)
                print(f"  Tot {measures['Tot']['Tot']['mean_power_W']:.6f} W, {measures['Tot']['Tot']['energy_J']:.6f} J", flush=True)
                time.sleep(args.pause)
        summary = summarize(rows)
        write_csv(output / "summary.csv", summary)
        report.update(status="complete", summary=summary)
        print("Saved:", output, flush=True)
        return 0
    except BaseException as exc:
        report.update(status="interrupted" if isinstance(exc, KeyboardInterrupt) else "failed", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        os.sched_setaffinity(0, original_affinity)
        if qis is not None:
            with suppress(Exception):
                qis.streamSock.close()
            with suppress(Exception):
                qis.disconnect()
        def save_report():
            report.update(finished_at=utc_now(), memory_after=memory_state())
            write_json_atomic(output / "run.json", report)
        finalize_run(report, sys.exception(), closeQis if owns_qis else lambda: None, save_report)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Interrupted; partial measurements retained.", file=sys.stderr)
        raise SystemExit(130)
