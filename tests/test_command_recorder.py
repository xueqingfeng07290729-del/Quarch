import csv
from contextlib import redirect_stderr
import io
import json
import math
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from quarch_capture.recorder import (
    CommandHandler, EnergySink, LocalServer, QisBackend, Recorder, main,
    read_stream_block, selected_synthetic_functions, send_command,
)


HEADER = "Time uS,L1_PInst VA mVA,L2_PInst VA mVA,L3_PInst VA mVA,Tot_PInst VA mVA"


class StreamOptimizationTests(unittest.TestCase):
    def setUp(self):
        self.functions = [
            "chan(L1_PInst,VA) pInstantaneous(chan(L1,V),chan(L1,A))",
            "chan(L2_RMS,V) rms(100ms,chan(L2,V))",
            "chan(L2_PInst,VA) pInstantaneous(chan(L2,V),chan(L2,A))",
            "chan(L2_PAct,W) pActive(100ms,chan(L2_PInst,VA))",
            "chan(L2V_Freq,Freq) frequency(100ms,chan(L2,V))",
            "chan(Tot_PInst,VA) sum(chan(L1_PInst,VA),chan(L2_PInst,VA))",
        ]
        self.definitions = [SimpleNamespace(function=f) for f in self.functions]

    def test_energy_only_keeps_instantaneous_power_before_resampling(self):
        self.assertEqual(selected_synthetic_functions(self.definitions, "L2", False), [self.functions[2]])

    def test_csv_details_keep_selected_dependencies_in_vendor_order(self):
        self.assertEqual(selected_synthetic_functions(self.definitions, "L2", True), self.functions[1:5])
        with self.assertRaises(ValueError):
            selected_synthetic_functions(self.definitions, "L3", False)

    def test_repeated_profile_is_reused_but_channel_and_csv_switch_reconfigure(self):
        backend = QisBackend("USB::test", "10ms")
        backend.synthetic_definitions = self.definitions
        backend._command = Mock()
        first = backend.configure_channels("L2", False)
        self.assertEqual(first["synthetic_channels"], 1)
        backend._command.reset_mock()
        backend.configure_channels("L2", False)
        backend._command.assert_not_called()
        detailed = backend.configure_channels("L2", True)
        self.assertEqual(detailed["synthetic_channels"], 4)
        backend._command.assert_any_call("stream created channels clear")
        backend.configure_channels("L1", False)
        self.assertEqual(backend.active_profile, ("L1", False))

    def test_full_blocks_are_drained_without_a_buffer_end_wait(self):
        reader = Mock()
        reader.send_command.return_value = "0 100000\r\n10000 200000\r\n"
        status, text, buffer_end = read_stream_block(reader, "USB::test")
        self.assertEqual(status, "running")
        self.assertFalse(buffer_end)
        self.assertEqual(text, "0 100000\r\n10000 200000\r\n")
        reader.send_command.assert_called_once_with("stream text all", device="USB::test")

    def test_failed_channel_creation_does_not_cache_a_partial_profile(self):
        backend = QisBackend("USB::test", "10ms")
        backend.synthetic_definitions = self.definitions
        backend.active_profile = ("L1", False)
        backend._command = Mock(side_effect=[None, RuntimeError("Create failed")])
        with self.assertRaisesRegex(RuntimeError, "Create failed"):
            backend.configure_channels("L2", False)
        self.assertIsNone(backend.active_profile)
        backend._command.reset_mock(side_effect=True)
        backend.configure_channels("L2", False)
        backend._command.assert_any_call("stream created channels clear")
        self.assertEqual(backend.active_profile, ("L2", False))

    def test_running_eof_is_temporary_and_stopped_eof_keeps_final_samples(self):
        for native, expected in [("Running", "running"), ("Stopped", "stopped"), ("Out of Buffer", "overrun")]:
            reader = Mock()
            reader.send_command.side_effect = ["0 100000\r\neof\r\n", native]
            status, text, buffer_end = read_stream_block(reader, "USB::test")
            self.assertEqual(status, expected)
            self.assertEqual(text, "0 100000\r\n")
            self.assertTrue(buffer_end)

    def test_unknown_stream_state_is_an_error(self):
        reader = Mock()
        reader.send_command.side_effect = ["eof", "USB disconnected"]
        with self.assertRaisesRegex(RuntimeError, "Unexpected QIS"):
            read_stream_block(reader, "USB::test")


class FakeBackend:
    def __init__(self):
        self.active = False
        self.reader_active = False
        self.error = None
        self.begin_count = self.end_count = 0
        self.closed = False
        self.sink = None

    def begin(self, sink):
        self.begin_count += 1
        self.sink = sink
        sink.set_header(HEADER)
        self.active = self.reader_active = True
        return {"record_start_ack_at": "test", "record_start_ack_monotonic": time.monotonic()}

    def emit(self, count=4):
        self.sink.feed("".join(f"{i*50000},-5000,100000,20000,999000\r\n" for i in range(count)))

    def end(self):
        self.end_count += 1
        self.active = self.reader_active = False
        return {"device_record_state": "OFF"}

    def record_state(self):
        return "ON" if self.active else "OFF"

    def close(self):
        self.closed = True


class SinkTests(unittest.TestCase):
    def test_selected_energy_and_fragmented_rows(self):
        sink = EnergySink("L2", .05)
        sink.set_header(HEADER)
        data = "0,-5000,100000,20000,999000\r\n50000,-5000,200000,20000,999000\r\n"
        for i in range(0, len(data), 7):
            sink.feed(data[i:i+7])
        sink.close()
        result = sink.snapshot()
        self.assertEqual(result["samples"], 2)
        self.assertEqual(result["duration_s"], .1)
        self.assertEqual(result["L2"]["energy_J"], 15)
        self.assertEqual(result["L2"]["mean_power_W"], 150)

    def test_csv_contains_only_selected_output(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "L1.csv"
            sink = EnergySink("L1", .05, path)
            sink.set_header(HEADER)
            sink.feed("0,-5000,100000,20000,999000\n50000,-5000,100000,20000,999000\n")
            sink.close()
            self.assertEqual(sink.snapshot()["L1"]["energy_J"], -.5)
            with path.open(newline="") as f:
                rows = list(csv.reader(f))
            self.assertEqual(rows[0], ["Time uS", "L1_PInst VA mVA"])
            self.assertEqual(len(rows), 3)

    def test_timestamp_gaps_and_non_finite_power_are_errors(self):
        for data in ["0,0,1,0,0\n100000,0,1,0,0\n", "0,0,nan,0,0\n"]:
            sink = EnergySink("L2", .05)
            sink.set_header(HEADER)
            with self.assertRaises(ValueError):
                sink.feed(data)

    def test_no_samples_is_unknown_energy_not_zero(self):
        result = EnergySink("L2", .05).snapshot()
        self.assertEqual(result["samples"], 0)
        self.assertIsNone(result["L2"]["energy_J"])


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.backend = FakeBackend()
        self.recorder = Recorder(self.backend, "L2", .05, self.temp.name)

    def tearDown(self):
        self.recorder.close()
        self.temp.cleanup()

    def test_idle_then_two_independent_measurements_and_csv_switch(self):
        self.assertEqual(self.recorder.status()["state"], "idle")
        self.assertEqual(self.backend.begin_count, 0)
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])
        self.recorder.start("sql_001", channel="L2", save_csv=False)
        self.backend.emit()
        first = self.recorder.stop()["result"]
        self.assertTrue(first["complete"])
        self.assertEqual(first["L2"]["energy_J"], 20)
        self.assertIsNone(first["csv_path"])
        self.assertFalse(list(Path(self.temp.name).glob("*.csv")))
        self.assertEqual(self.recorder.status()["device_record_state"], "OFF")
        self.assertFalse(self.recorder.status()["reader_active"])
        self.assertEqual(self.backend.begin_count, 1)
        self.recorder.start("sql_002", channel="L3", save_csv=True)
        self.backend.emit(2)
        second = self.recorder.stop()["result"]
        self.assertTrue(second["complete"])
        self.assertEqual(second["samples"], 2)
        self.assertEqual(second["L3"]["energy_J"], 2)
        self.assertTrue(Path(second["csv_path"]).is_file())
        self.assertEqual(len(list(Path(self.temp.name).glob("*.csv"))), 1)
        self.assertEqual(self.backend.begin_count, 2)
        self.assertEqual(self.backend.end_count, 2)
        self.assertEqual(self.recorder.stop()["result"]["session_id"], second["session_id"])
        self.assertEqual(self.backend.end_count, 2)

    def test_duplicate_start_and_invalid_labels_do_not_restart_recording(self):
        with self.assertRaises(ValueError):
            self.recorder.start("../outside", channel="L2")
        self.assertEqual(self.backend.begin_count, 0)
        self.recorder.start("sql1", channel="L2")
        with self.assertRaises(RuntimeError):
            self.recorder.start("sql2", channel="L2")
        self.assertEqual(self.backend.begin_count, 1)

    def test_every_start_requires_a_valid_explicit_channel(self):
        for channel in (None, "", "L4", False, 2):
            with self.subTest(channel=channel):
                with self.assertRaisesRegex(ValueError, "channel is required"):
                    self.recorder.start("missing_channel", channel=channel, save_csv=True)
        self.assertEqual(self.backend.begin_count, 0)
        self.assertEqual(self.recorder.status()["device_record_state"], "OFF")
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])

    def test_short_empty_capture_is_flagged_incomplete(self):
        self.recorder.start("short", channel="L2")
        result = self.recorder.stop()
        self.assertEqual(result["state"], "error")
        self.assertFalse(result["result"]["complete"])
        self.assertIsNone(result["result"]["L2"]["energy_Wh"])
        self.assertEqual(self.recorder.status()["device_record_state"], "OFF")

    def test_failed_stop_never_claims_idle(self):
        self.recorder.start("failure", channel="L2")
        self.backend.emit()
        def fail():
            raise RuntimeError("USB disconnected")
        self.backend.end = fail
        result = self.recorder.stop()
        self.assertFalse(result["result"]["complete"])
        self.assertEqual(self.recorder.status()["state"], "error")
        with self.assertRaises(RuntimeError):
            self.recorder.start("new", channel="L2")

    def test_shutdown_stops_active_recording_and_rejects_new_start(self):
        self.recorder.start("shutdown", channel="L2")
        self.backend.emit()
        result = self.recorder.request_shutdown()
        self.assertTrue(result["shutdown"])
        self.assertFalse(self.backend.active)
        with self.assertRaises(RuntimeError):
            self.recorder.start("late", channel="L2")

    def test_actual_local_socket_protocol(self):
        sock = Path(self.temp.name) / "commands.sock"
        with LocalServer(str(sock), CommandHandler) as server:
            server.recorder = self.recorder
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01})
            thread.start()
            try:
                self.assertTrue(send_command(sock, {"command": "status"})["ok"])
                self.assertFalse(send_command(sock, {"command": "stop"})["ok"])
                missing = send_command(sock, {"command": "start", "label": "sql1", "save_csv": True})
                self.assertFalse(missing["ok"])
                self.assertIn("channel is required", missing["error"])
                self.assertEqual(self.backend.begin_count, 0)
                self.assertEqual(list(Path(self.temp.name).glob("*.csv")), [])
                self.assertTrue(send_command(sock, {"command": "start", "channel": "L2", "label": "sql1", "save_csv": False})["ok"])
                self.backend.emit()
                response = send_command(sock, {"command": "stop"})
                self.assertTrue(response["ok"])
                self.assertEqual(response["result"]["L2"]["energy_J"], 20)
                self.assertTrue(send_command(sock, {"command": "shutdown"})["shutdown"])
                thread.join(2)
                self.assertFalse(thread.is_alive())
            finally:
                if thread.is_alive():
                    server.shutdown()
                    thread.join(2)


class CliTests(unittest.TestCase):
    def test_start_without_channel_fails_before_sending_command(self):
        stderr = io.StringIO()
        with patch("quarch_capture.recorder.send_command") as send, redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                main(["start", "--label", "sql_001", "--csv", "off"])
            self.assertEqual(raised.exception.code, 2)
            send.assert_not_called()
        self.assertIn("--channel", stderr.getvalue())


class BackendFailureTests(unittest.TestCase):
    def test_failed_hardware_stop_closes_reader_and_keeps_retry_flag(self):
        backend = QisBackend("USB::test", "50ms")
        backend.reader = reader = object()
        backend.started_by_us = True
        backend.thread = Mock()
        backend.thread.is_alive.return_value = False
        backend._command = Mock(side_effect=RuntimeError("USB disconnected"))
        with patch.object(backend, "_close_interface") as close:
            with self.assertRaisesRegex(RuntimeError, "USB disconnected"):
                backend.end()
            close.assert_called_once_with(reader)
        self.assertIsNone(backend.reader)
        self.assertTrue(backend.started_by_us)


if __name__ == "__main__":
    unittest.main()
