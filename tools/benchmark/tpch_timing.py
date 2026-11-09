#!/usr/bin/env python3
"""Time TPC-H queries using an existing DuckDB CLI, without a Python DuckDB package."""

import argparse
import csv
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import selectors
import shutil
import statistics
import subprocess
import sys
import threading
import time
import uuid

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from quarch_capture.common import atomic_output, finalize_run, termination_guard, write_json_atomic as save_json

TIMER = re.compile(r"Run Time \(s\): real ([\d.eE+-]+) user ([\d.eE+-]+) sys ([\d.eE+-]+)")
CSV_FIELDS = ["database", "query_id", "phase", "iteration", "elapsed_s", "user_cpu_s", "system_cpu_s", "driver_wall_s", "statement_count"]


def query_ids(value):
    result = []
    try:
        for part in value.split(","):
            bounds = part.split("-")
            if len(bounds) == 1:
                numbers = [int(part)]
            elif len(bounds) == 2 and int(bounds[0]) <= int(bounds[1]):
                numbers = range(int(bounds[0]), int(bounds[1]) + 1)
            else:
                raise ValueError()
            for number in numbers:
                if not 1 <= number <= 22:
                    raise ValueError()
                if number not in result:
                    result.append(number)
        if not result:
            raise ValueError()
    except ValueError:
        raise argparse.ArgumentTypeError("Use query numbers 1-22, e.g. 1-22 or 1,6,9,18,21")
    return result


def timer_totals(lines):
    values = [tuple(map(float, match.groups())) for line in lines for match in TIMER.finditer(line)]
    if not values or any(not math.isfinite(v) or v < 0 for row in values for v in row):
        raise RuntimeError("DuckDB returned no valid SQL timer; no timing result was accepted")
    return {
        "elapsed_s": math.fsum(row[0] for row in values),
        "user_cpu_s": math.fsum(row[1] for row in values),
        "system_cpu_s": math.fsum(row[2] for row in values),
        "statement_count": len(values),
    }


@lru_cache(maxsize=8)
def cli_capabilities(executable):
    """Local/source CLI builds may lack -no-agent and timer precision options."""
    help_result = subprocess.run([str(executable), "-help"], input="", capture_output=True,
                                 text=True, timeout=10)
    help_text = help_result.stdout + getattr(help_result, "stderr", "")
    agent_flags = ("-no-agent",) if "-no-agent" in help_text else ()
    probe = subprocess.run([str(executable), *agent_flags, "-no-init", "-batch", "-bail", ":memory:"],
                           input=".timer on 9\n.timer off\n", capture_output=True, text=True, timeout=10)
    timer = ".timer on 9" if getattr(probe, "returncode", 0) == 0 else ".timer on"
    return agent_flags, timer


def cli_version(executable):
    flags, _ = cli_capabilities(executable)
    return subprocess.run([str(executable), *flags, "-no-init", "-version"], capture_output=True,
                          text=True, check=True, timeout=10).stdout.strip()


def cli_args(executable, database):
    flags, _ = cli_capabilities(executable)
    args = [str(executable), *flags, "-no-init", "-batch", "-bail"]
    if str(database) != ":memory:":
        args.append("-readonly")
    return args + [str(database)]


class DuckDBSession:
    """One persistent process/connection; fully execute queries in trash output mode."""

    def __init__(self, executable, database, timeout=300):
        self.timeout = timeout
        self.timer_command = cli_capabilities(executable)[1]
        self.process = subprocess.Popen(cli_args(executable, database), stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        text=True, bufsize=1)
        os.set_blocking(self.process.stdin.fileno(), False)
        self.closed = False
        self.lines = queue.Queue()

        def read_output():
            try:
                for line in self.process.stdout:
                    self.lines.put(line.rstrip("\r\n"))
            finally:
                self.lines.put(None)

        self.reader = threading.Thread(target=read_output, daemon=True)
        self.reader.start()
        try:
            self.exchange(".mode trash\n.timer off")
        except BaseException:
            self.close()
            raise

    def exchange(self, script):
        marker = "TPCH_DONE_" + uuid.uuid4().hex
        deadline = time.monotonic() + self.timeout
        messages = []
        try:
            pending = memoryview((script.rstrip() + "\n.print " + marker + "\n").encode("utf-8"))
            with selectors.DefaultSelector() as writable:
                writable.register(self.process.stdin.fileno(), selectors.EVENT_WRITE)
                while pending:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not writable.select(remaining):
                        raise queue.Empty()
                    try:
                        count = os.write(self.process.stdin.fileno(), pending[:65536])
                        pending = pending[count:]
                    except BlockingIOError:
                        continue
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise queue.Empty()
                line = self.lines.get(timeout=remaining)
                if line is None:
                    raise RuntimeError("DuckDB exited before completing the query:\n" + "\n".join(messages[-20:]))
                if line == marker:
                    return messages
                messages.append(line)
        except queue.Empty:
            self._terminate()
            raise TimeoutError(f"DuckDB command exceeded {self.timeout:g} seconds") from None
        except BrokenPipeError:
            self._terminate()
            raise RuntimeError("DuckDB process is no longer running") from None
        except BaseException:
            self._terminate()
            raise

    def configure(self, threads, memory_limit, temp_directory):
        temp = str(temp_directory).replace("'", "''")
        self.exchange(f"SET threads={threads};\nSET memory_limit='{memory_limit}';\n"
                      f"SET temp_directory='{temp}';\nSET enable_progress_bar=false;")

    def run(self, sql):
        # CLI timer measures SQL work, excluding process startup, configuration,
        # Python IPC, resource inspection, file output and inter-query sleep.
        sql = sql.rstrip()
        if not sql.endswith(";"):
            sql += ";"
        started = time.perf_counter_ns()
        lines = self.exchange(self.timer_command + "\n" + sql + "\n.timer off")
        wall = (time.perf_counter_ns() - started) / 1e9
        return {**timer_totals(lines), "driver_wall_s": wall}

    def _terminate(self):
        if self.process.poll() is None:
            try:
                self.process.terminate()
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            except ProcessLookupError:
                self.process.wait()

    def close(self):
        if self.closed:
            return
        self.closed = True
        self._terminate()
        self.process.stdin.close()
        self.reader.join(timeout=1)
        self.process.stdout.close()


class ResourceObserver:
    """Observe process high-water RSS and spill files; do not enable SQL profiling."""

    def __init__(self, pid, spill):
        self.pid, self.spill = pid, spill
        self.stop_event = threading.Event()
        self.peak_rss_mib = self.peak_temp_bytes = 0
        self.thread = threading.Thread(target=self.observe, daemon=True)
        self.thread.start()

    def sample(self):
        try:
            for line in Path(f"/proc/{self.pid}/status").read_text().splitlines():
                if line.startswith("VmHWM:"):
                    self.peak_rss_mib = max(self.peak_rss_mib, int(line.split()[1]) / 1024)
        except FileNotFoundError:
            pass
        size = 0
        for directory, _, files in os.walk(self.spill):
            for name in files:
                try:
                    size += (Path(directory) / name).stat().st_size
                except FileNotFoundError:
                    pass
        self.peak_temp_bytes = max(self.peak_temp_bytes, size)

    def observe(self):
        while not self.stop_event.is_set():
            self.sample()
            self.stop_event.wait(.2)

    def close(self):
        self.stop_event.set()
        self.thread.join()
        self.sample()


def summarize(rows):
    result = []
    for query_id in sorted({row["query_id"] for row in rows}):
        values = [row["elapsed_s"] for row in rows if row["query_id"] == query_id and row["phase"] == "measured"]
        if values:
            result.append({"query_id": query_id, "runs": len(values), "mean_s": statistics.mean(values),
                           "median_s": statistics.median(values), "min_s": min(values), "max_s": max(values),
                           "stdev_s": statistics.stdev(values) if len(values) > 1 else 0})
    return result


def finalize_benchmark(metadata, error, session, observer, database, original, metadata_path, *results):
    def collect():
        if observer:
            metadata.update(process_peak_rss_mib=observer.peak_rss_mib,
                            peak_spill_bytes_observed=observer.peak_temp_bytes,
                            spill_observation_interval_s=.2)
        current = database.stat()
        metadata["database_file_unchanged"] = (current.st_size, current.st_mtime_ns) == (original.st_size, original.st_mtime_ns)
    finalize_run(metadata, error,
                 lambda: observer.close() if observer else None,
                 lambda: session.close() if session else None,
                 collect, *results, lambda: save_json(metadata_path, metadata))


def write_timing_summary(output, rows, runs, metadata):
    summary = summarize(rows)
    with atomic_output(output / "summary.csv", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["query_id", "runs", "mean_s", "median_s", "min_s", "max_s", "stdev_s"])
        writer.writeheader()
        writer.writerows(summary)
    metadata.update(total_query_medians_s=sum(row["median_s"] for row in summary),
                    queries_completed=sum(row["runs"] == runs for row in summary))


def load_queries(executable, database):
    sql = "LOAD tpch; SELECT query_nr, query FROM tpch_queries() ORDER BY query_nr;"
    process = subprocess.run(cli_args(executable, database) + ["-json", "-c", sql],
                             capture_output=True, text=True, timeout=30)
    if process.returncode:
        raise RuntimeError("Cannot load TPC-H query definitions; verify the matching tpch extension is installed:\n" + process.stderr)
    rows = json.loads(process.stdout)
    queries = {int(row["query_nr"]): row["query"] for row in rows}
    if set(queries) != set(range(1, 23)):
        raise RuntimeError("The tpch extension did not provide all 22 queries")
    return queries


def benchmark_database(executable, database, queries, args, output):
    output.mkdir()
    spill = output / "spill"
    spill.mkdir()
    original = database.stat()
    metadata = {"database": str(database), "database_bytes": original.st_size,
                "database_mtime_ns": original.st_mtime_ns, "readonly": True,
                "threads": args.threads, "memory_limit": args.memory_limit,
                "warmup_per_query": args.warmup, "measured_runs_per_query": args.runs,
                "sleep_s": args.sleep, "query_timeout_s": args.timeout,
                "cache_note": "Per-query warmup; OS page cache is not cleared. Zero warmup is not a cold-cache guarantee.",
                "timing_note": "DuckDB CLI timer in trash output mode; setup, warmups and sleeps excluded from summary.",
                "status": "running"}
    save_json(output / "metadata.json", metadata)
    rows = []
    session = observer = error = None
    try:
        session = DuckDBSession(executable, database, args.timeout)
        session.configure(args.threads, args.memory_limit, spill)
        observer = ResourceObserver(session.process.pid, spill)
        jobs = [(number, phase, iteration) for number in args.queries
                for phase, count in [("warmup", args.warmup), ("measured", args.runs)]
                for iteration in range(1, count + 1)]
        with (output / "timings.csv").open("x", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
            writer.writeheader()
            for index, (number, phase, iteration) in enumerate(jobs):
                values = session.run(queries[number])
                row = {"database": database.name, "query_id": number, "phase": phase,
                       "iteration": iteration, **values}
                writer.writerow(row)
                handle.flush()
                rows.append(row)
                print(f"{database.stem} Q{number:02d} {phase} {iteration}: {values['elapsed_s']:.6f}s", flush=True)
                if args.sleep and index + 1 < len(jobs):
                    time.sleep(args.sleep)
        metadata["status"] = "complete"
        return summarize(rows)
    except BaseException as exc:
        error = exc
        metadata.update(status="failed", error=f"{type(exc).__name__}: {exc}", error_notes=getattr(exc, "__notes__", []))
        raise
    finally:
        finalize_benchmark(metadata, error, session, observer, database, original, output / "metadata.json",
                           lambda: write_timing_summary(output, rows, args.runs, metadata))


@termination_guard()
def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, action="append", required=True, help="Existing database; repeat to compare SFs")
    parser.add_argument("--duckdb", default=shutil.which("duckdb") or str(Path.home() / ".local/bin/duckdb"))
    parser.add_argument("--queries", type=query_ids, default=query_ids("1-22"))
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--warmup", type=int, default=1, help="Warmup executions before measuring each query (default 1)")
    parser.add_argument("--sleep", type=float, default=5, help="Seconds between executions, outside SQL timing (default 5)")
    parser.add_argument("--threads", type=int, default=min(os.cpu_count() or 1, 12))
    parser.add_argument("--memory-limit", default="16GB")
    parser.add_argument("--timeout", type=float, default=300, help="Timeout per query, seconds")
    parser.add_argument("--output-dir", type=Path, help="New output directory; default results/tpch/<UTC timestamp>")
    args = parser.parse_args(argv)
    if args.runs < 1 or args.warmup < 0 or args.threads < 1:
        parser.error("runs and threads must be positive; warmup must be nonnegative")
    if not math.isfinite(args.sleep) or args.sleep < 0 or not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("sleep must be finite and nonnegative; timeout must be finite and positive")
    if not re.fullmatch(r"[1-9]\d*(?:KB|MB|GB|TB|KiB|MiB|GiB|TiB)", args.memory_limit, re.IGNORECASE):
        parser.error("memory-limit must be a positive size such as 16GB")
    executable = shutil.which(args.duckdb)
    if executable is None:
        parser.error("DuckDB CLI executable not found")
    databases = [path.expanduser().resolve(strict=True) for path in args.database]
    if len(set(databases)) != len(databases) or any(not path.is_file() for path in databases):
        parser.error("Provide distinct existing database files")
    output = (args.output_dir or Path("results/tpch") / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=False)
    version = cli_version(executable)
    queries = load_queries(executable, databases[0])
    query_dir = output / "queries"
    query_dir.mkdir()
    for number in args.queries:
        (query_dir / f"q{number:02d}.sql").write_text(queries[number], encoding="utf-8")
    save_json(output / "run.json", {"duckdb_executable": executable, "duckdb_version": version,
              "query_sha256": hashlib.sha256(json.dumps(queries, sort_keys=True).encode()).hexdigest(),
              "databases": [str(path) for path in databases], "queries": args.queries,
              "threads": args.threads, "memory_limit": args.memory_limit,
              "runs": args.runs, "warmup": args.warmup, "sleep_s": args.sleep,
              "quarch_capture": False})
    print("Output:", output, flush=True)
    comparisons = {}
    for index, database in enumerate(databases, 1):
        summaries = benchmark_database(executable, database, queries, args, output / f"{index:02d}_{database.stem}")
        for row in summaries:
            comparisons.setdefault(row["query_id"], {"query_id": row["query_id"]})[f"{index:02d}_{database.stem}_median_s"] = row["median_s"]
    with (output / "comparison.csv").open("x", newline="", encoding="utf-8") as handle:
        fields = ["query_id"] + [f"{index:02d}_{path.stem}_median_s" for index, path in enumerate(databases, 1)]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(comparisons[number] for number in args.queries)
    print("Completed:", output / "comparison.csv", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Interrupted; completed query timings have been saved.", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        for note in getattr(exc, "__notes__", []):
            print(note, file=sys.stderr)
        raise SystemExit(1)
