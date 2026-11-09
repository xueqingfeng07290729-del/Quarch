#!/usr/bin/env python3
"""Measure individual TPC-H queries on the existing SF30 database and SSD rail.

Use bin/tpch-ssd-energy, or python -m tools.benchmark.tpch_ssd_energy.
Each run has a new read-only DuckDB process; cache preparation is outside capture.
"""

import argparse
import csv
import ctypes
from datetime import datetime, timezone
import hashlib
import json
import math
import mmap
import os
from pathlib import Path
import re
import shutil
import statistics
import subprocess
import sys
import time

from quarch_capture.channels import channel_name
from quarch_capture.client import DEFAULT_SOCKET, require_idle
from quarch_capture.common import atomic_output, finalize_run, termination_guard, write_json_atomic
from tools.benchmark.tpch_energy import BATCH_FIELDS, measure_batch
from tools.benchmark.tpch_timing import (DuckDBSession, ResourceObserver, cli_args,
                                       cli_capabilities, cli_version, load_queries, query_ids)


DATABASE_DIR = Path.home() / "experiment_dbs/duckdb"
DEFAULT_DATABASE = DATABASE_DIR / "tpch_sf30_uncompressed.duckdb"
DEFAULT_CLI = DATABASE_DIR / "duckdb-v1.5.5/build/release/duckdb"
EXTRA_FIELDS = ["cache_policy", "cached_bytes_before", "cached_bytes_after", "cached_fraction_after",
                "process_read_bytes", "process_write_bytes", "process_rchar", "process_wchar",
                "peak_rss_mib", "peak_spill_bytes"]
SUMMARY_FIELDS = ["query_id", "runs", "sql_median_s", "sql_mean_s", "energy_median_J", "energy_mean_J",
                  "energy_total_J", "mean_power_W", "read_mean_MiB", "write_mean_MiB", "max_rss_MiB",
                  "max_spill_MiB", "recorded_duration_mean_s", "boundary_extra_mean_s"]


def cache_residency(database):
    """Inspect Linux page residency without faulting the database into memory."""
    size = database.stat().st_size
    if size <= 0:
        raise ValueError("Database file is empty")
    pages = (size + mmap.PAGESIZE - 1) // mmap.PAGESIZE
    libc = ctypes.CDLL(None, use_errno=True)
    libc.mmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_long]
    libc.mmap.restype = ctypes.c_void_p
    libc.mincore.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_ubyte)]
    libc.mincore.restype = ctypes.c_int
    libc.munmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    libc.munmap.restype = ctypes.c_int
    with database.open("rb") as handle:
        # PROT_READ avoids reserving 35 GB of writable virtual-memory commit.
        address = libc.mmap(None, size, mmap.PROT_READ, mmap.MAP_PRIVATE, handle.fileno(), 0)
        if address == ctypes.c_void_p(-1).value:
            raise OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno()))
        try:
            vector = (ctypes.c_ubyte * pages)()
            if libc.mincore(address, size, vector):
                raise OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno()))
            resident = sum(value & 1 for value in vector)
        finally:
            if libc.munmap(address, size):
                raise OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno()))
    return {"cached_bytes": min(size, resident * mmap.PAGESIZE), "cached_fraction": resident / pages}


def prepare_cache(database, policy):
    before = cache_residency(database)
    if policy == "cold":
        with database.open("rb") as handle:
            os.posix_fadvise(handle.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
        after = cache_residency(database)
        if after["cached_fraction"] > .001:
            raise RuntimeError(f"Database page cache is still {after['cached_fraction']:.2%} resident; "
                               "close other database readers before retrying. No cold-cache result was accepted.")
    else:
        after = before
    return {"cache_policy": policy, "cached_bytes_before": before["cached_bytes"],
            "cached_bytes_after": after["cached_bytes"], "cached_fraction_after": after["cached_fraction"]}


def process_io(pid):
    values = {}
    for line in Path(f"/proc/{pid}/io").read_text().splitlines():
        key, value = line.split(":", 1)
        values[key] = int(value)
    return {key: values[key] for key in ("read_bytes", "write_bytes", "rchar", "wchar")}


def size_bytes(value):
    amount, unit = re.fullmatch(r"([1-9]\d*)([KMGT]i?B)", value, re.IGNORECASE).groups()
    return int(amount) * (1024 if "i" in unit.lower() else 1000) ** ("KMGT".index(unit[0].upper()) + 1)


def inspect_database(executable, database):
    sql = "SELECT table_name, estimated_size AS row_count FROM duckdb_tables() WHERE schema_name='main' ORDER BY table_name;"
    tables = json.loads(subprocess.run(cli_args(executable, database) + ["-json", "-c", sql],
                                      capture_output=True, text=True, check=True, timeout=30).stdout)
    names = {row["table_name"] for row in tables}
    required = {"lineitem", "orders", "customer", "supplier", "part", "partsupp", "nation", "region"}
    if not required <= names:
        raise RuntimeError(f"TPC-H tables missing: {sorted(required - names)}")
    unions = [f"SELECT '{table}' AS table_name, compression, count AS segment_values "
              f"FROM pragma_storage_info('{table}')" for table in sorted(required)]
    sql = ("SELECT table_name, compression, sum(segment_values) AS segment_values FROM (" +
           " UNION ALL ".join(unions) + ") GROUP BY table_name, compression ORDER BY table_name, compression;")
    storage = json.loads(subprocess.run(cli_args(executable, database) + ["-json", "-c", sql],
                                       capture_output=True, text=True, check=True, timeout=30).stdout)
    return {"tables": tables, "storage_compression": storage}


def measure_query(executable, database, sql, number, run, args, output):
    session = observer = error = None
    row = {"query_id": number, "batch": run}
    spill = output / "spill" / f"q{number:02d}_r{run:02d}"
    spill.mkdir()
    try:
        session = DuckDBSession(executable, database, args.timeout)
        session.configure(args.threads, args.memory_limit, spill)
        session.exchange(f"SET max_temp_directory_size='{args.spill_limit}';")
        # The process has loaded catalog metadata but has executed no query.
        # Discard the file's OS cache only after opening/configuring the engine.
        row.update(prepare_cache(database, args.cache))
        observer = ResourceObserver(session.process.pid, spill)
        before = process_io(session.process.pid)
        row.update(measure_batch(session, sql, 1, args.socket, args.channel,
                                 f"ssd_q{number:02d}_r{run:02d}", args.csv == "on",
                                 output / "captures" / f"q{number:02d}_r{run:02d}.json"))
        after = process_io(session.process.pid)
        for key in before:
            delta = after[key] - before[key]
            if delta < 0:
                raise RuntimeError("DuckDB process I/O counters decreased")
            row["process_" + key] = delta
        return row
    except BaseException as exc:
        error = exc
        raise
    finally:
        def resources():
            row.update(peak_rss_mib=observer.peak_rss_mib if observer else 0,
                       peak_spill_bytes=observer.peak_temp_bytes if observer else 0)
        finalize_run(row, error, lambda: observer.close() if observer else None,
                     resources, lambda: session.close() if session else None)


def summarize(rows):
    summary = []
    for number in sorted({row["query_id"] for row in rows}):
        group = [row for row in rows if row["query_id"] == number]
        total_energy = math.fsum(row["energy_total_J"] for row in group)
        duration = math.fsum(row["recorded_duration_s"] for row in group)
        summary.append({
            "query_id": number, "runs": len(group),
            "sql_median_s": statistics.median(row["sql_total_s"] for row in group),
            "sql_mean_s": statistics.mean(row["sql_total_s"] for row in group),
            "energy_median_J": statistics.median(row["energy_total_J"] for row in group),
            "energy_mean_J": total_energy / len(group), "energy_total_J": total_energy,
            "mean_power_W": total_energy / duration,
            "read_mean_MiB": statistics.mean(row["process_read_bytes"] for row in group) / 2**20,
            "write_mean_MiB": statistics.mean(row["process_write_bytes"] for row in group) / 2**20,
            "max_rss_MiB": max(row["peak_rss_mib"] for row in group),
            "max_spill_MiB": max(row["peak_spill_bytes"] for row in group) / 2**20,
            "recorded_duration_mean_s": duration / len(group),
            "boundary_extra_mean_s": statistics.mean(row["duration_minus_sql_s"] for row in group),
        })
    return summary


def write_summary(output, rows):
    with atomic_output(output / "summary.csv", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=SUMMARY_FIELDS)
        writer.writeheader()
        writer.writerows(summarize(rows))


@termination_guard()
def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--duckdb", default=str(DEFAULT_CLI) if DEFAULT_CLI.is_file() else shutil.which("duckdb"))
    parser.add_argument("--channel", type=channel_name, default="3.3V")
    parser.add_argument("--queries", type=query_ids, default=query_ids("1-22"))
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--cache", choices=["cold", "keep"], default="cold")
    parser.add_argument("--threads", type=int, default=min(8, os.cpu_count() or 1))
    parser.add_argument("--memory-limit", default="8GB")
    parser.add_argument("--spill-limit", default="8GB")
    parser.add_argument("--sleep", type=float, default=2, help="Idle seconds between runs, outside capture")
    parser.add_argument("--timeout", type=float, default=600, help="SQL timeout per execution")
    parser.add_argument("--csv", choices=["on", "off"], default="off", help="Raw power CSV; result CSV always saved")
    parser.add_argument("--socket", type=Path, default=DEFAULT_SOCKET)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args(argv)
    if sys.platform != "linux":
        parser.error("This SSD benchmark requires Linux for cache and process I/O checks")
    if min(args.runs, args.threads) < 1:
        parser.error("runs and threads must be positive")
    if not math.isfinite(args.sleep) or args.sleep < 0 or not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("sleep must be nonnegative and timeout must be positive and finite")
    for value in (args.memory_limit, args.spill_limit):
        if not re.fullmatch(r"[1-9]\d*(?:KB|MB|GB|TB|KiB|MiB|GiB|TiB)", value, re.IGNORECASE):
            parser.error("memory/spill limits must be positive sizes such as 8GB")
    executable = shutil.which(args.duckdb) if args.duckdb else None
    if executable is None:
        parser.error("DuckDB CLI not found; use --duckdb /path/to/duckdb")
    database = args.database.expanduser().resolve(strict=True)
    if not database.is_file():
        parser.error("database must be an existing file")
    args.socket = args.socket.expanduser().resolve()
    initial = require_idle(args.socket)
    if args.channel not in initial.get("available_channels", []):
        parser.error("Selected channel is not available in the recorder")
    original = database.stat()
    queries = load_queries(executable, database)
    database_info = inspect_database(executable, database)
    output = (args.output_dir or Path(__file__).resolve().parents[2] / "results/tpch_ssd" /
              datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=False)
    for name in ("queries", "captures", "spill"):
        (output / name).mkdir()
    for number in args.queries:
        (output / "queries" / f"q{number:02d}.sql").write_text(queries[number], encoding="utf-8")
    metadata = {
        "status": "running", "started_at": datetime.now(timezone.utc).isoformat(),
        "database": str(database), "database_bytes": original.st_size, "database_mtime_ns": original.st_mtime_ns,
        "readonly": True, "database_info": database_info, "duckdb_executable": executable,
        "duckdb_version": cli_version(executable), "sql_timer_command": cli_capabilities(executable)[1],
        "queries": args.queries, "runs_per_query": args.runs, "executions_per_capture": 1,
        "query_sha256": hashlib.sha256(json.dumps(queries, sort_keys=True).encode()).hexdigest(),
        "channel": args.channel, "interval_s": initial["interval_s"], "initial_recorder_status": initial,
        "cache_policy": args.cache, "threads": args.threads, "memory_limit": args.memory_limit,
        "spill_limit": args.spill_limit, "sleep_s": args.sleep, "timeout_s": args.timeout,
        "raw_csv_enabled": args.csv == "on", "available_memory_kib_at_start": int(next(
            line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:'))),
        "mount": json.loads(subprocess.run(["findmnt", "-T", str(database), "-J"], capture_output=True, text=True, check=True).stdout),
        "block_devices": json.loads(subprocess.run(["lsblk", "-J", "-o", "NAME,TYPE,SIZE,MODEL,SERIAL,MOUNTPOINTS"],
                                                    capture_output=True, text=True, check=True).stdout),
        "cache_scope": "Fresh DuckDB process for each run, no pilot queries. Cold mode uses file-specific POSIX_FADV_DONTNEED after engine setup and verifies OS residency <=0.1% with mincore; no global cache drop. SSD controller caches are not reset. Keep mode retains OS page cache without guaranteeing warm data.",
        "energy_scope": "Measured selected SSD rail energy over the recording window; includes idle baseline, command boundary overhead, other activity on that SSD and any DuckDB spill. No baseline subtraction. It is not whole-machine energy or isolated NAND energy.",
        "io_scope": "DuckDB /proc/PID/io deltas around capture: read_bytes/write_bytes are storage-accounted I/O, rchar/wchar include cached I/O. Device/controller caching remains possible.",
        "raw_csv_note": "Raw CSV defaults off to avoid extra writes to the measured SSD. Enabling it can affect the measured power.",
    }
    write_json_atomic(output / "run.json", metadata)
    print("Output:", output, flush=True)
    print(f"Cache={args.cache}; {args.runs} runs/query; {args.channel}; {initial['interval_s'] * 1000:g} ms output", flush=True)
    rows = []
    error = None
    try:
        with (output / "runs.csv").open("x", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=BATCH_FIELDS + EXTRA_FIELDS)
            writer.writeheader()
            for index, number in enumerate(args.queries):
                for run in range(1, args.runs + 1):
                    require_idle(args.socket)
                    if shutil.disk_usage(output).free < size_bytes(args.spill_limit) + 2 * 2**30:
                        raise RuntimeError("Free space is below the spill limit plus 2 GiB; stopped before another query")
                    print(f"Q{number:02d} run {run}/{args.runs}: preparing {args.cache} cache", flush=True)
                    row = measure_query(executable, database, queries[number], number, run, args, output)
                    writer.writerow(row)
                    handle.flush()
                    rows.append(row)
                    write_summary(output, rows)
                    print(f"Q{number:02d} run {run}: {row['sql_total_s']:.3f}s, {row['energy_total_J']:.6f}J, "
                          f"{row['mean_power_W']:.3f}W; read={row['process_read_bytes'] / 2**20:.1f}MiB; "
                          f"capture={row['recorded_duration_s']:.2f}s", flush=True)
                    if args.sleep and not (index == len(args.queries) - 1 and run == args.runs):
                        time.sleep(args.sleep)
        metadata["status"] = "complete"
    except BaseException as exc:
        error = exc
        metadata.update(status="failed", error=f"{type(exc).__name__}: {exc}", error_notes=getattr(exc, "__notes__", []))
        raise
    finally:
        def finish_metadata():
            current = database.stat()
            metadata.update(finished_at=datetime.now(timezone.utc).isoformat(), completed_runs=len(rows),
                            completed_queries=sum(row['runs'] == args.runs for row in summarize(rows)),
                            database_file_unchanged=(current.st_ino, current.st_size, current.st_mtime_ns) ==
                            (original.st_ino, original.st_size, original.st_mtime_ns))
            if not metadata["database_file_unchanged"]:
                raise RuntimeError("Database file changed during the benchmark")
        finalize_run(metadata, error, finish_metadata, lambda: write_summary(output, rows),
                     lambda: write_json_atomic(output / "run.json", metadata))
    print("Completed:", output / "summary.csv", flush=True)
    return 0


def cli():
    try:
        return main()
    except KeyboardInterrupt as exc:
        print("Interrupted; completed queries and partial capture evidence are saved.", file=sys.stderr)
        for note in getattr(exc, "__notes__", []):
            print(note, file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        for note in getattr(exc, "__notes__", []):
            print(note, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(cli())
