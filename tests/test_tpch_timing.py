import argparse
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest

from tools.benchmark.tpch_timing import DuckDBSession, query_ids, summarize, timer_totals


class TimingHelpersTests(unittest.TestCase):
    def test_query_selection_preserves_order_and_rejects_invalid_ids(self):
        self.assertEqual(query_ids("6,1-3,6,22"), [6, 1, 2, 3, 22])
        for value in ("", "0", "23", "2-1", "1,", "1.5", "1-23"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                query_ids(value)

    def test_multistatement_query_sums_engine_timers(self):
        result = timer_totals([
            "Run Time (s): real 0.125000001 user 0.200000 sys 0.010000",
            "Run Time (s): real 0.250000002 user 0.400000 sys 0.020000",
        ])
        self.assertAlmostEqual(result["elapsed_s"], .375000003)
        self.assertAlmostEqual(result["user_cpu_s"], .6)
        self.assertEqual(result["statement_count"], 2)
        for lines in (["Catalog Error: failed"], ["Run Time (s): real nan user 0 sys 0"],
                      ["Run Time (s): real -1 user 0 sys 0"]):
            with self.assertRaises(RuntimeError):
                timer_totals(lines)

    def test_summary_excludes_warmups(self):
        result = summarize([
            {"query_id": 1, "phase": "warmup", "elapsed_s": 999},
            {"query_id": 1, "phase": "measured", "elapsed_s": 1},
            {"query_id": 1, "phase": "measured", "elapsed_s": 3},
        ])
        self.assertEqual(result[0]["runs"], 2)
        self.assertEqual(result[0]["median_s"], 2)
        self.assertEqual(result[0]["mean_s"], 2)


DUCKDB = shutil.which("duckdb")


class TimeoutTests(unittest.TestCase):
    def check_timeout(self, sql):
        with tempfile.TemporaryDirectory() as folder:
            executable = Path(folder) / "fake_duckdb"
            executable.write_text("#!/usr/bin/env python3\nimport sys,time\n"
                                  "for line in sys.stdin:\n"
                                  " if line.startswith('.print '): print(line.split(' ',1)[1].strip(),flush=True)\n"
                                  " elif line.startswith('SELECT'): time.sleep(5)\n")
            executable.chmod(0o700)
            session = DuckDBSession(executable, ":memory:", timeout=3)
            session.timeout = .1
            began = time.monotonic()
            try:
                with self.assertRaisesRegex(TimeoutError, "exceeded"):
                    session.run(sql)
                self.assertLess(time.monotonic() - began, 1)
                self.assertIsNotNone(session.process.poll())
            finally:
                session.close()
                session.close()

    def test_timeout_covers_blocked_stdin_write_and_terminates_child(self):
        self.check_timeout("SELECT 1;\n" * 100000)

    def test_timeout_covers_stdout_wait_and_terminates_child(self):
        self.check_timeout("SELECT 1")


@unittest.skipUnless(DUCKDB, "DuckDB CLI is not installed")
class NativeCliTests(unittest.TestCase):
    def test_persistent_session_times_sql_and_reports_failure(self):
        session = DuckDBSession(DUCKDB, ":memory:", timeout=3)
        try:
            first = session.run("SELECT sum(i) FROM range(10000) t(i)")
            second = session.run("SELECT 42")
            self.assertEqual(first["statement_count"], 1)
            self.assertEqual(second["statement_count"], 1)
            self.assertGreaterEqual(first["driver_wall_s"], first["elapsed_s"])
            with self.assertRaisesRegex(RuntimeError, "does_not_exist"):
                session.run("SELECT * FROM does_not_exist")
        finally:
            session.close()

    def test_database_is_opened_readonly(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.duckdb"
            subprocess.run([DUCKDB, "-no-agent", "-no-init", str(path), "CREATE TABLE t AS SELECT 1 AS x"],
                           capture_output=True, check=True)
            before = path.stat().st_mtime_ns
            session = DuckDBSession(DUCKDB, path, timeout=3)
            try:
                self.assertEqual(session.run("SELECT * FROM t")["statement_count"], 1)
                with self.assertRaises(RuntimeError):
                    session.run("DELETE FROM t")
            finally:
                session.close()
            self.assertEqual(path.stat().st_mtime_ns, before)


if __name__ == "__main__":
    unittest.main()
