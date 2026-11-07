#!/usr/bin/env python3
"""Measure TPC-H batch timings and whole-computer energy through an idle Quarch recorder.

Run from the repository root: python -m tools.benchmark.tpch_energy --help
"""

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import statistics
import subprocess
import sys
import time
import uuid

from quarch_capture.commands import DEFAULT_SOCKET, send_command
from tools.benchmark.tpch_timing import DuckDBSession, ResourceObserver, load_queries, query_ids, save_json


BATCH_FIELDS = [
    "query_id", "batch", "executions", "sql_total_s", "sql_mean_s", "driver_wall_s",
    "energy_total_J", "energy_mean_J", "energy_mean_Wh", "recorded_duration_s",
    "duration_minus_sql_s", "duration_minus_sql_percent", "mean_power_W", "samples",
    "interval_s", "channel", "capture_session_id", "capture_json",
]
SUMMARY_FIELDS = [
    "query_id", "batches", "executions_per_batch", "total_executions",
    "sql_median_s", "sql_mean_s", "sql_min_s", "sql_max_s", "sql_cv_percent",
    "energy_median_J", "energy_mean_J", "energy_median_Wh", "energy_min_J",
    "energy_max_J", "energy_cv_percent", "recorded_duration_total_s", "energy_total_J",
    "mean_power_W", "duration_minus_sql_percent",
]


def command(socket_path, request):
    response = send_command(socket_path, request)
    if not response.get("ok"):
        raise RuntimeError(f"Recorder {request['command']} failed: {response}")
    return response


def require_idle(socket_path):
    status = command(socket_path, {"command": "status"})
    if status.get("protocol_version", 0) < 2:
        raise RuntimeError("Recorder protocol 2 is required; restart quarch_capture serve to load the updated service")
    if status.get("state") != "idle" or status.get("reader_active") or status.get("device_record_state") != "OFF":
        raise RuntimeError("Recorder must already be idle with recording OFF; active sessions are not interrupted")
    return status


def batch_size(pilot_seconds, target_seconds, maximum):
    estimate = statistics.median(pilot_seconds)
    if not math.isfinite(estimate) or estimate <= 0:
        raise RuntimeError("Cannot choose a batch size from nonpositive pilot timings")
    return min(maximum, max(1, math.ceil(target_seconds / estimate)))


def preserve_response(capture_path, response):
    """Keep server partial results even when its command reports failure."""
    capture = response.get("result")
    if capture is None:
        capture = {"complete": False, "errors": [response.get("error", "No capture result returned")],
                   "recorder_response": response}
    save_json(capture_path, capture)
    return capture


def stop_owned_capture(socket_path, started, capture_path):
    response = send_command(socket_path, {"command": "stop", "session_id": started["session_id"]})
    capture = preserve_response(capture_path, response)
    if not response.get("ok"):
        raise RuntimeError(f"Recorder stop failed: {response}")
    if response.get("already_stopped"):
        raise RuntimeError("Measurement stopped before the SQL batch finished; partial energy was rejected")
    if capture.get("session_id") != started["session_id"] or capture.get("request_id") != started["request_id"]:
        raise RuntimeError("Recorder session changed; energy cannot be attributed to this batch")
    return capture


def measure_batch(session, sql, executions, socket_path, channel, label, save_csv, capture_path):
    # Only start/stop enclose the SQL batch; pilots, CSV/JSON writes and sleeps
    # are outside it. One CLI exchange avoids per-execution Python round trips.
    require_idle(socket_path)
    request_id, start_deadline = uuid.uuid4().hex, time.time() + 45
    try:
        started = command(socket_path, {"command": "start", "channel": channel,
                                       "label": label, "save_csv": save_csv,
                                       "request_id": request_id, "start_deadline": start_deadline})
        if not started.get("session_id") or started.get("request_id") != request_id:
            raise RuntimeError("Invalid start response; request ownership could not be confirmed")
    except BaseException as exc:
        try:
            cancelled = send_command(socket_path, {"command": "cancel_start", "request_id": request_id,
                                                   "start_deadline": start_deadline})
            preserve_response(capture_path, cancelled)
            if not cancelled.get("ok"):
                raise RuntimeError(str(cancelled))
        except BaseException as cleanup:
            exc.add_note(f"Start cancellation could not be confirmed: {cleanup}. Check recorder status.")
        raise
    sql_error = None
    try:
        statement = sql.rstrip()
        if not statement.endswith(";"):
            statement += ";"
        timing = session.run("\n".join([statement] * executions))
    except BaseException as exc:
        sql_error = exc
    try:
        capture = stop_owned_capture(socket_path, started, capture_path)
    except BaseException as cleanup:
        if sql_error is not None:
            sql_error.add_note(f"Capture cleanup also failed: {cleanup}")
            raise sql_error
        raise
    if sql_error is not None:
        raise sql_error
    if not capture.get("complete") or capture.get("errors") or capture.get("device_record_state") != "OFF":
        raise RuntimeError(f"Incomplete capture: {capture.get('errors')}")
    if capture.get("selected_channel") != channel:
        raise RuntimeError("Capture channel does not match the requested channel")
    if not capture.get("summary_saved"):
        raise RuntimeError("Capture summary could not be saved")
    if timing["statement_count"] != executions:
        raise RuntimeError("Expected exactly one SQL statement per TPC-H execution; batch was rejected")
    energy = capture[channel]["energy_J"]
    duration, interval = capture["duration_s"], capture["interval_s"]
    if energy is None or not math.isfinite(energy) or capture["samples"] <= 0 or duration <= 0 or interval <= 0:
        raise RuntimeError("No valid measured energy; zero is not substituted for missing data")
    if not math.isclose(duration, capture["samples"] * interval, rel_tol=1e-9, abs_tol=1e-9):
        raise RuntimeError("Capture duration does not agree with the sample count")
    if not math.isclose(energy, capture[channel]["energy_Wh"] * 3600, rel_tol=1e-9, abs_tol=1e-9):
        raise RuntimeError("Capture joules and watt-hours disagree")
    sql_total = timing["elapsed_s"]
    if sql_total <= 0:
        raise RuntimeError("SQL batch has no positive elapsed time")
    return {
        "executions": executions, "sql_total_s": sql_total, "sql_mean_s": sql_total / executions,
        "driver_wall_s": timing["driver_wall_s"], "energy_total_J": energy,
        "energy_mean_J": energy / executions, "energy_mean_Wh": energy / executions / 3600,
        "recorded_duration_s": duration, "duration_minus_sql_s": duration - sql_total,
        "duration_minus_sql_percent": (duration / sql_total - 1) * 100,
        "mean_power_W": energy / duration, "samples": capture["samples"], "interval_s": interval,
        "channel": channel, "capture_session_id": capture["session_id"], "capture_json": str(capture_path),
    }


def cv(values):
    mean = statistics.mean(values)
    return statistics.stdev(values) / abs(mean) * 100 if len(values) > 1 and mean else 0


def summarize(rows):
    result = []
    for number in sorted({row["query_id"] for row in rows}):
        batches = [row for row in rows if row["query_id"] == number]
        times = [row["sql_mean_s"] for row in batches]
        energies = [row["energy_mean_J"] for row in batches]
        duration = math.fsum(row["recorded_duration_s"] for row in batches)
        energy = math.fsum(row["energy_total_J"] for row in batches)
        sql_total = math.fsum(row["sql_total_s"] for row in batches)
        executions = sum(row["executions"] for row in batches)
        result.append({
            "query_id": number, "batches": len(batches), "executions_per_batch": batches[0]["executions"],
            "total_executions": executions, "sql_median_s": statistics.median(times),
            "sql_mean_s": sql_total / executions, "sql_min_s": min(times), "sql_max_s": max(times),
            "sql_cv_percent": cv(times), "energy_median_J": statistics.median(energies),
            "energy_mean_J": energy / executions, "energy_median_Wh": statistics.median(energies) / 3600,
            "energy_min_J": min(energies), "energy_max_J": max(energies), "energy_cv_percent": cv(energies),
            "recorded_duration_total_s": duration, "energy_total_J": energy,
            "mean_power_W": energy / duration, "duration_minus_sql_percent": (duration / sql_total - 1) * 100,
        })
    return result


def write_summary(output, rows):
    with (output / "summary.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=SUMMARY_FIELDS)
        writer.writeheader()
        writer.writerows(summarize(rows))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--channel", choices=["L1", "L2", "L3"], required=True)
    parser.add_argument("--socket", type=Path, default=DEFAULT_SOCKET)
    parser.add_argument("--duckdb", default=shutil.which("duckdb") or str(Path.home() / ".local/bin/duckdb"))
    parser.add_argument("--queries", type=query_ids, default=query_ids("1-22"))
    parser.add_argument("--runs", type=int, default=3, help="Measured batches per query (default 3)")
    parser.add_argument("--warmup", type=int, default=3, help="Unmeasured pilots per query (at least 1, default 3)")
    parser.add_argument("--batch-seconds", type=float, default=5, help="Target SQL time per batch, estimated from pilots")
    parser.add_argument("--max-executions", type=int, default=10000)
    parser.add_argument("--sleep", type=float, default=5, help="Recording-OFF pause between measured batches")
    parser.add_argument("--threads", type=int, default=min(os.cpu_count() or 1, 12))
    parser.add_argument("--memory-limit", default="16GB")
    parser.add_argument("--timeout", type=float, default=300, help="Timeout for the entire SQL batch")
    parser.add_argument("--csv", choices=["on", "off"], default="off", help="Raw instrument CSV; batch/summary CSV always saved")
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args(argv)
    if min(args.runs, args.warmup, args.threads, args.max_executions) < 1:
        parser.error("runs, warmup, threads and max-executions must be positive")
    if any(not math.isfinite(v) or v <= 0 for v in [args.batch_seconds, args.timeout]):
        parser.error("batch-seconds and timeout must be finite and positive")
    if not math.isfinite(args.sleep) or args.sleep < 0:
        parser.error("sleep must be finite and nonnegative")
    if not re.fullmatch(r"[1-9]\d*(?:KB|MB|GB|TB|KiB|MiB|GiB|TiB)", args.memory_limit, re.IGNORECASE):
        parser.error("memory-limit must be a positive size such as 16GB")
    executable = shutil.which(args.duckdb)
    if executable is None:
        parser.error("DuckDB CLI executable not found")
    database = args.database.expanduser().resolve(strict=True)
    if not database.is_file():
        parser.error("database must be an existing file")
    args.socket = args.socket.expanduser().resolve()
    initial_status = require_idle(args.socket)
    queries = load_queries(executable, database)
    output = (args.output_dir or Path("results/tpch_energy") / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=False)
    for directory in ["queries", "captures", "spill"]:
        (output / directory).mkdir()
    for number in args.queries:
        (output / "queries" / f"q{number:02d}.sql").write_text(queries[number], encoding="utf-8")
    original = database.stat()
    metadata = {
        "status": "running", "started_at": datetime.now(timezone.utc).isoformat(),
        "database": str(database), "database_bytes": original.st_size, "database_mtime_ns": original.st_mtime_ns,
        "readonly": True, "duckdb_executable": executable,
        "duckdb_version": subprocess.run([executable, "-no-agent", "-version"], capture_output=True, text=True, check=True).stdout.strip(),
        "query_sha256": hashlib.sha256(json.dumps(queries, sort_keys=True).encode()).hexdigest(),
        "queries": args.queries, "channel": args.channel, "threads": args.threads, "memory_limit": args.memory_limit,
        "runs": args.runs, "warmup": args.warmup, "batch_target_s": args.batch_seconds,
        "max_executions": args.max_executions, "sleep_s": args.sleep, "query_timeout_s": args.timeout,
        "raw_csv_enabled": args.csv == "on", "recorder_socket": str(args.socket), "initial_recorder_status": initial_status,
        "energy_scope": "Whole-computer selected-output energy over the full device recording window, divided by SQL execution count. Includes baseline, QIS/Python and command-boundary overhead; no idle baseline is subtracted.",
        "timing_scope": "DuckDB CLI SQL timer total divided by execution count; includes SQL planning and execution. Persistent readonly connection, trash output mode.",
        "workload_note": "Consecutive repeated SQL in each batch; pilots outside recording, 5s default OFF pauses between batches. OS page cache is not cleared. This differs from isolated single-query timings.",
        "summary_note": "Median of batch means; batch energy and duration are retained. Three batches are repeatability checks, not a meter-accuracy estimate.",
    }
    save_json(output / "run.json", metadata)
    print("Output:", output, flush=True)
    rows = []
    session = observer = None
    try:
        session = DuckDBSession(executable, database, args.timeout)
        session.configure(args.threads, args.memory_limit, output / "spill")
        observer = ResourceObserver(session.process.pid, output / "spill")
        with (output / "pilots.csv").open("x", newline="", encoding="utf-8") as pilot_file, \
                (output / "batches.csv").open("x", newline="", encoding="utf-8") as batch_file:
            pilots = csv.DictWriter(pilot_file, fieldnames=["query_id", "pilot", "elapsed_s", "driver_wall_s"])
            batches = csv.DictWriter(batch_file, fieldnames=BATCH_FIELDS)
            pilots.writeheader()
            batches.writeheader()
            for query_index, number in enumerate(args.queries):
                require_idle(args.socket)
                pilot_times = []
                for pilot in range(1, args.warmup + 1):
                    value = session.run(queries[number])
                    if value["statement_count"] != 1:
                        raise RuntimeError("Expected one SELECT per TPC-H query")
                    pilot_times.append(value["elapsed_s"])
                    pilots.writerow({"query_id": number, "pilot": pilot, "elapsed_s": value["elapsed_s"],
                                     "driver_wall_s": value["driver_wall_s"]})
                    pilot_file.flush()
                executions = batch_size(pilot_times, args.batch_seconds, args.max_executions)
                print(f"Q{number:02d}: pilot median {statistics.median(pilot_times):.6f}s; {executions} executions/batch", flush=True)
                for batch in range(1, args.runs + 1):
                    capture_path = output / "captures" / f"q{number:02d}_r{batch:02d}.json"
                    values = measure_batch(session, queries[number], executions, args.socket, args.channel,
                                           f"tpch_{database.stem}_q{number:02d}_r{batch:02d}", args.csv == "on", capture_path)
                    row = {"query_id": number, "batch": batch, **values}
                    batches.writerow(row)
                    batch_file.flush()
                    rows.append(row)
                    write_summary(output, rows)
                    print(f"Q{number:02d} batch {batch}/{args.runs}: {values['sql_mean_s']:.6f}s, "
                          f"{values['energy_mean_J']:.4f}J/execution; window={values['recorded_duration_s']:.2f}s "
                          f"extra={values['duration_minus_sql_percent']:.1f}%", flush=True)
                    last = query_index + 1 == len(args.queries) and batch == args.runs
                    if args.sleep and not last:
                        time.sleep(args.sleep)
        metadata["status"] = "complete"
    except BaseException as exc:
        metadata.update(status="failed", error=f"{type(exc).__name__}: {exc}",
                        error_notes=getattr(exc, "__notes__", []))
        raise
    finally:
        if observer:
            observer.close()
            metadata.update(process_peak_rss_mib=observer.peak_rss_mib,
                            peak_spill_bytes_observed=observer.peak_temp_bytes,
                            spill_observation_interval_s=.2)
        if session:
            session.close()
        current = database.stat()
        metadata.update(database_file_unchanged=(current.st_size, current.st_mtime_ns) == (original.st_size, original.st_mtime_ns),
                        completed_batches=len(rows),
                        completed_queries=sum(row["batches"] == args.runs for row in summarize(rows)),
                        finished_at=datetime.now(timezone.utc).isoformat())
        write_summary(output, rows)
        save_json(output / "run.json", metadata)
    print("Completed:", output / "summary.csv", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt as exc:
        print("Interrupted; completed batches and partial captures have been saved.", file=sys.stderr)
        for note in getattr(exc, "__notes__", []):
            print(note, file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        for note in getattr(exc, "__notes__", []):
            print(note, file=sys.stderr)
        raise SystemExit(1)
