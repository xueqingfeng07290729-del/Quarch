import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tools.benchmark import tpch_ssd_energy as ssd
from tools.benchmark.tpch_timing import cli_args, cli_capabilities


class SsdBenchmarkTests(unittest.TestCase):
    def test_cold_cache_is_verified_and_database_bytes_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.duckdb"
            payload = bytes(range(256)) * 4096
            with path.open("wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            self.assertEqual(path.read_bytes(), payload)
            self.assertGreater(ssd.cache_residency(path)["cached_bytes"], 0)
            prepared = ssd.prepare_cache(path, "cold")
            self.assertLessEqual(prepared["cached_fraction_after"], .001)
            self.assertEqual(path.read_bytes(), payload)

    def test_failed_cache_eviction_is_rejected_and_keep_does_not_evict(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.duckdb"
            path.write_bytes(b"database")
            with patch.object(ssd, "cache_residency", return_value={"cached_bytes": 4096, "cached_fraction": 1}), \
                    patch.object(ssd.os, "posix_fadvise") as advise:
                with self.assertRaisesRegex(RuntimeError, "No cold-cache result"):
                    ssd.prepare_cache(path, "cold")
                advise.assert_called_once()
                advise.reset_mock()
                self.assertEqual(ssd.prepare_cache(path, "keep")["cached_fraction_after"], 1)
                advise.assert_not_called()

    def test_sql_failure_closes_only_its_process_and_observer(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)
            (output / "spill").mkdir()
            session = Mock()
            observer = Mock(peak_rss_mib=12, peak_temp_bytes=0)
            args = SimpleNamespace(timeout=3, threads=1, memory_limit="1GB", spill_limit="1GB",
                                   cache="cold", socket="test", channel="3.3V", csv="off")
            events = []
            with patch.object(ssd, "DuckDBSession", return_value=session), \
                    patch.object(ssd, "ResourceObserver", return_value=observer), \
                    patch.object(ssd, "prepare_cache", side_effect=lambda *a: events.append("cache") or {}), \
                    patch.object(ssd, "process_io", return_value={"read_bytes": 0}), \
                    patch.object(ssd, "measure_batch", side_effect=lambda *a: events.append("capture") or (_ for _ in ()).throw(ValueError("SQL failure"))):
                with self.assertRaisesRegex(ValueError, "SQL failure"):
                    ssd.measure_query("duckdb", output / "db", "SELECT missing", 1, 1, args, output)
            self.assertEqual(events, ["cache", "capture"])
            session.close.assert_called_once()
            observer.close.assert_called_once()

    def test_summary_uses_total_energy_over_total_duration(self):
        base = {"query_id": 6, "process_read_bytes": 2**20, "process_write_bytes": 0,
                "peak_rss_mib": 10, "peak_spill_bytes": 0, "duration_minus_sql_s": .1}
        rows = [{**base, "sql_total_s": 1, "energy_total_J": 2, "recorded_duration_s": 1},
                {**base, "sql_total_s": 3, "energy_total_J": 12, "recorded_duration_s": 3}]
        summary = ssd.summarize(rows)[0]
        self.assertEqual(summary["runs"], 2)
        self.assertEqual(summary["energy_mean_J"], 7)
        self.assertEqual(summary["mean_power_W"], 3.5)
        self.assertEqual(summary["read_mean_MiB"], 1)

    def test_source_cli_without_agent_or_timer_precision_is_supported(self):
        cli_capabilities.cache_clear()
        with patch("tools.benchmark.tpch_timing.subprocess.run", side_effect=[
                SimpleNamespace(stdout="", stderr="-no-init skip startup file", returncode=0),
                SimpleNamespace(stdout="", stderr="Invalid .timer", returncode=1)]):
            self.assertEqual(cli_capabilities("source-cli"), ((), ".timer on"))
            self.assertNotIn("-no-agent", cli_args("source-cli", "db"))
            self.assertIn("-readonly", cli_args("source-cli", "db"))
        cli_capabilities.cache_clear()


if __name__ == "__main__":
    unittest.main()
