from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from quarch_capture.common import finalize_run
from tools.benchmark import tpch_energy, tpch_timing


class FinalizationTests(unittest.TestCase):
    def test_sql_failure_survives_resource_and_summary_failures(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / "fake.duckdb"
            database.write_bytes(b"never opened")
            output = Path(folder) / "output"
            session = Mock()
            session.process.pid = os.getpid()
            session.run.return_value = {"statement_count": 1, "elapsed_s": .1, "driver_wall_s": .1}
            observer = Mock(peak_rss_mib=1, peak_temp_bytes=0)
            observer.close.side_effect = OSError("observer failure")
            args = ["--database", str(database), "--channel", "L2", "--queries", "1",
                    "--runs", "1", "--warmup", "1", "--sleep", "0", "--output-dir", str(output)]
            with patch.object(tpch_energy, "require_idle", return_value={"protocol_version": 2}), \
                    patch.object(tpch_energy, "load_queries", return_value={1: "SELECT 1;"}), \
                    patch.object(tpch_energy.subprocess, "run", return_value=SimpleNamespace(stdout="fake version")), \
                    patch.object(tpch_energy, "DuckDBSession", return_value=session), \
                    patch.object(tpch_energy, "ResourceObserver", return_value=observer), \
                    patch.object(tpch_energy, "measure_batch", side_effect=ValueError("original SQL failure")), \
                    patch.object(tpch_energy, "write_summary", side_effect=OSError("summary disk full")), \
                    redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(ValueError, "original SQL failure"):
                    tpch_energy.main(args)
            metadata = json.loads((output / "run.json").read_text())
            self.assertEqual(metadata["status"], "failed")
            self.assertIn("original SQL failure", metadata["error"])
            self.assertTrue(any("observer failure" in note for note in metadata["error_notes"]))
            self.assertTrue(any("summary disk full" in note for note in metadata["error_notes"]))
            session.close.assert_called_once()

    def test_cleanup_failure_is_reported_even_after_a_successful_body(self):
        metadata = {"status": "complete"}
        saved = {}
        with self.assertRaisesRegex(OSError, "disk full"):
            finalize_run(metadata, None, Mock(side_effect=OSError("disk full")), lambda: saved.update(metadata))
        self.assertEqual(saved["status"], "failed")

    def test_partial_query_is_not_counted_as_complete(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / "fake.duckdb"
            database.write_bytes(b"never opened")
            output = Path(folder) / "output"
            session = Mock()
            session.process.pid = os.getpid()
            session.run.side_effect = [{"elapsed_s": .1, "user_cpu_s": .1, "system_cpu_s": 0,
                                        "driver_wall_s": .1, "statement_count": 1}, ValueError("query failed")]
            args = SimpleNamespace(threads=1, memory_limit="16GB", warmup=0, runs=3, sleep=0, timeout=3, queries=[1])
            with patch.object(tpch_timing, "DuckDBSession", return_value=session), \
                    patch.object(tpch_timing, "ResourceObserver", return_value=Mock(peak_rss_mib=1, peak_temp_bytes=0)), \
                    redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(ValueError, "query failed"):
                    tpch_timing.benchmark_database("fake", database, {1: "SELECT 1"}, args, output)
            metadata = json.loads((output / "metadata.json").read_text())
            self.assertEqual(metadata["status"], "failed")
            self.assertEqual(metadata["queries_completed"], 0)


if __name__ == "__main__":
    unittest.main()
