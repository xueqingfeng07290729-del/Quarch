from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch

from tools.benchmark.tpch_energy import batch_size, measure_batch, require_idle, summarize
from tools.benchmark.tpch_timing import DuckDBSession


class EnergyBatchTests(unittest.TestCase):
    def test_batch_size_uses_pilot_median_with_limits(self):
        self.assertEqual(batch_size([.1, .1, 9], 5, 10000), 50)
        self.assertEqual(batch_size([9], 5, 10000), 1)
        self.assertEqual(batch_size([.0001], 5, 50), 50)
        with self.assertRaises(RuntimeError):
            batch_size([0], 5, 100)

    def test_active_recorder_is_not_interrupted(self):
        with patch("quarch_capture.client.send_command", return_value={"ok": True, "state": "recording", "protocol_version": 2}) as send:
            with self.assertRaisesRegex(RuntimeError, "idle"):
                require_idle("socket")
            self.assertEqual(send.call_count, 1)

    def run_batch(self, capture_changes=None, sql_error=None):
        capture = {"session_id": "s1", "complete": True, "errors": [], "selected_channel": "L2",
                   "summary_saved": True,
                   "device_record_state": "OFF", "samples": 100, "interval_s": .01, "duration_s": 1,
                   "L2": {"energy_J": 20, "energy_Wh": 20 / 3600}}
        capture.update(capture_changes or {})
        events = []

        def control(socket, request):
            events.append(request["command"])
            if request["command"] == "status":
                return {"ok": True, "state": "idle", "reader_active": False, "device_record_state": "OFF", "protocol_version": 2}
            if request["command"] == "start":
                self.assertEqual(request["channel"], "L2")
                self.assertFalse(request["save_csv"])
                capture["request_id"] = request["request_id"]
                return {"ok": True, "session_id": "s1", "request_id": request["request_id"]}
            self.assertEqual(request["session_id"], "s1")
            return {"ok": True, "result": capture}

        def execute(sql):
            events.append("sql")
            self.assertEqual(sql, "SELECT 42;\nSELECT 42;")
            if sql_error:
                raise sql_error
            return {"elapsed_s": .8, "driver_wall_s": .81, "statement_count": 2}

        with tempfile.TemporaryDirectory() as folder, \
                patch("quarch_capture.client.send_command", side_effect=control):
            path = Path(folder) / "capture.json"
            try:
                result = measure_batch(Mock(run=execute), "SELECT 42", 2, "socket", "L2", "test", False, path)
            finally:
                self.assertEqual(events, ["status", "start", "sql", "stop"])
                self.assertTrue(path.exists())
            return result

    def test_energy_is_divided_by_actual_validated_execution_count(self):
        result = self.run_batch()
        self.assertEqual(result["energy_mean_J"], 10)
        self.assertEqual(result["sql_mean_s"], .4)
        self.assertAlmostEqual(result["duration_minus_sql_percent"], 25)

    def test_sql_failure_stops_recording_and_preserves_capture(self):
        with self.assertRaisesRegex(RuntimeError, "SQL failed"):
            self.run_batch(sql_error=RuntimeError("SQL failed"))

    def test_partial_missing_and_wrong_session_energy_are_rejected(self):
        for changes in ({"complete": False}, {"samples": 0}, {"session_id": "other"},
                        {"L2": {"energy_J": None, "energy_Wh": None}}, {"duration_s": 3}):
            with self.subTest(changes=changes), self.assertRaises(RuntimeError):
                self.run_batch(changes)

    def test_summary_preserves_gross_energy_and_excludes_any_unmeasured_pilots(self):
        common = {"query_id": 1, "executions": 2, "recorded_duration_s": 1}
        rows = [{**common, "sql_mean_s": .2, "sql_total_s": .4, "energy_mean_J": 10, "energy_total_J": 20},
                {**common, "sql_mean_s": .4, "sql_total_s": .8, "energy_mean_J": 14, "energy_total_J": 28}]
        result = summarize(rows)[0]
        self.assertAlmostEqual(result["sql_median_s"], .3)
        self.assertEqual(result["energy_median_J"], 12)
        self.assertEqual(result["energy_total_J"], 48)
        self.assertEqual(result["total_executions"], 4)
        self.assertEqual(result["batches"], 2)

    @unittest.skipUnless(shutil.which("duckdb"), "DuckDB CLI is not installed")
    def test_repeated_sql_is_fully_executed_and_each_timer_is_present(self):
        session = DuckDBSession(shutil.which("duckdb"), ":memory:", timeout=3)
        try:
            result = session.run("\n".join(["SELECT sum(i) FROM range(1000000) t(i);"] * 7))
            self.assertEqual(result["statement_count"], 7)
            self.assertGreater(result["elapsed_s"], 0)
        finally:
            session.close()


if __name__ == "__main__":
    unittest.main()
