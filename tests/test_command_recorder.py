import csv
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from quarch_capture.commands import CommandHandler, DEFAULT_SOCKET, LocalServer, main, send_command
from quarch_capture.measurement import Recorder
from quarch_capture.qis_backend import (
    QisBackend, read_stream_block, selected_synthetic_functions, usb_connection_key,
)
from quarch_capture.stream_data import EnergySink


HEADER = "Time uS,L1_PInst VA mVA,L2_PInst VA mVA,L3_PInst VA mVA,Tot_PInst VA mVA"


class UsbConnectionKeyTests(unittest.TestCase):
    def make_device(self, root, name="1-4", vendor="16d0"):
        node = root / name
        node.mkdir()
        for name, value in {"idVendor": vendor, "idProduct": "0449", "serial": "QTL2843-03-002",
                            "busnum": "1", "devnum": "8"}.items():
            (node / name).write_text(value)
        return node

    def test_key_is_stable_until_matching_device_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.make_device(root, "unrelated", vendor="0000")
            node = self.make_device(root)
            key = usb_connection_key("USB::QTL2843-03-002", root)
            self.assertIsNotNone(key)
            self.assertEqual(key, usb_connection_key("usb:qtl2843-03-002", root))
            (node / "devnum").write_text("9")
            self.assertNotEqual(key, usb_connection_key("USB::QTL2843-03-002", root))
            self.assertIsNone(usb_connection_key("USB::other", root))
            self.assertIsNone(usb_connection_key("TCP::192.168.1.99", root))
            (node / "serial").unlink()
            self.assertIsNone(usb_connection_key("USB::QTL2843-03-002", root))

    def test_new_node_invalidates_key_even_if_usb_address_is_reused(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            node = self.make_device(root)
            before = usb_connection_key("USB::QTL2843-03-002", root)
            for file in node.iterdir():
                file.unlink()
            # Keep the old inode allocated: some /tmp filesystems reuse it
            # within the same timestamp tick, unlike the intended new node.
            node.rename(root / "detached")
            self.make_device(root)
            self.assertNotEqual(before, usb_connection_key("USB::QTL2843-03-002", root))


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

    def test_repeated_profile_is_reused_and_csv_switch_reconfigures(self):
        backend = QisBackend("USB::test", "10ms")
        backend.synthetic_definitions = self.definitions
        backend._command = Mock()
        first = backend.configure_channels("L2", False)
        self.assertEqual(first["synthetic_channels"], 1)
        backend._command.reset_mock()
        repeated = backend.configure_channels("L2", False)
        self.assertFalse(repeated["channels_rebuilt"])
        backend._command.assert_not_called()
        detailed = backend.configure_channels("L2", True)
        self.assertEqual(detailed["synthetic_channels"], 4)
        backend._command.assert_any_call("stream created channels clear")
        backend.configure_channels("L1", False)
        backend._command.assert_called_with("stream create channel " + self.functions[0])

    def test_full_blocks_are_drained_without_a_buffer_end_wait(self):
        reader = Mock()
        reader.send_command.return_value = "0 100000\r\n10000 200000\r\n"
        status, text, buffer_end = read_stream_block(reader, "USB::test")
        self.assertEqual(status, "running")
        self.assertFalse(buffer_end)
        self.assertEqual(text, "0 100000\r\n10000 200000\r\n")
        reader.send_command.assert_called_once_with("stream text all", device="USB::test")

    def test_failed_channel_creation_is_retried_from_clear(self):
        backend = QisBackend("USB::test", "10ms")
        backend.synthetic_definitions = self.definitions
        backend._command = Mock(side_effect=[None, RuntimeError("Create failed")])
        with self.assertRaisesRegex(RuntimeError, "Create failed"):
            backend.configure_channels("L2", False)
        self.assertIsNone(backend.active_profile)
        backend._command.reset_mock(side_effect=True)
        backend.configure_channels("L2", False)
        backend._command.assert_any_call("stream created channels clear")
        backend._command.assert_called_with("stream create channel " + self.functions[2])

    def test_start_reuses_settings_until_usb_reconnect_or_identity_becomes_unknown(self):
        backend = QisBackend("USB::test", "20ms")
        backend.synthetic_definitions = self.definitions
        backend.qis = SimpleNamespace(host="127.0.0.1", port=9722)
        state = {"recording": False, "functions": [], "period": "20ms", "trigger": "manual", "pre": "0"}
        commands, resamples = [], []

        def hardware_command(command):
            commands.append(command)
            if command == "rec?":
                return "ON" if state["recording"] else "OFF"
            if command == "stream created channels clear":
                state["functions"].clear()
            elif command.startswith("stream create channel "):
                state["functions"].append(command.removeprefix("stream create channel "))
            elif command.startswith("rec:trigger:mode "):
                state["trigger"] = command.split()[-1]
            elif command.startswith("rec:trigger:pre "):
                state["pre"] = command.split()[-1]
            elif command == "rec stream":
                self.assertEqual(state["trigger"], "manual")
                self.assertEqual(state["pre"], "0")
                state["recording"] = True
            elif command == "rec stop":
                state["recording"] = False
            return "OK"

        def resample(period):
            resamples.append(period)
            state["period"] = period
            return "OK"

        def make_reader(**kwargs):
            reader = Mock()
            delivered = False

            def header(device):
                if self.functions[2] in state["functions"]:
                    return "Time uS,L2_PInst VA mVA"
                return "Time uS,L2 voltage mV"

            def read(command, device):
                nonlocal delivered
                if command == "stream?":
                    return "Running" if state["recording"] else "Stopped"
                if not delivered:
                    delivered = True
                    period_us = 20000 if state["period"] == "20ms" else 125
                    return f"0 100000\r\n{period_us} 200000\r\neof"
                return "eof"

            reader.sendAndReceiveText.return_value = "Header ready"
            reader.stream_header_format.side_effect = header
            reader.send_command.side_effect = read
            return reader

        backend.device = SimpleNamespace(ConString="USB::test", send_command=hardware_command)
        backend.power = SimpleNamespace(stream_resample_mode=resample)
        module = SimpleNamespace(QisInterface=make_reader)
        generations = [("usb", 8), ("usb", 8), ("usb", 9), None, None]
        with patch.dict(sys.modules, {"quarchpy.connection_specific.connection_QIS": module}), \
                patch("quarch_capture.qis_backend.usb_connection_key", side_effect=generations):
            for index in range(len(generations)):
                if index >= 2:
                    # The Python backend survives, but QIS/device defaults return.
                    state.update(functions=[], period="125us", trigger="external", pre="50")
                sink = EnergySink("L2", .02)
                try:
                    started = backend.begin(sink)
                    backend.end()
                    self.assertEqual(sink.snapshot()["samples"], 2)
                    self.assertEqual(sink.snapshot()["L2"]["energy_J"], 6)
                    self.assertEqual(backend.record_state(), "OFF")
                    self.assertEqual(started["channels_rebuilt"], index != 1)
                    self.assertEqual(started["configuration_restored"], index != 1)
                    self.assertEqual(commands.count("stream created channels clear"), max(1, index))
                    self.assertEqual(len(resamples), max(1, index))
                finally:
                    if backend.reader is not None:
                        backend.end()
                    sink.close()

    def test_stream_error_invalidates_a_previously_cached_configuration(self):
        backend = QisBackend("USB::test", "10ms")
        backend.synthetic_definitions = self.definitions
        backend.active_profile, backend.configured_connection = ("L2", False), ("usb", 8)
        backend.device = SimpleNamespace(ConString="USB::test", send_command=Mock(return_value="OFF"))
        backend.qis = SimpleNamespace(host="127.0.0.1", port=9722)
        backend._command = Mock()
        reader = Mock()
        reader.sendAndReceiveText.return_value = "Header ready"
        reader.stream_header_format.return_value = "Time uS,L2 voltage mV"
        with patch("quarch_capture.qis_backend.usb_connection_key", return_value=("usb", 8)), \
                patch.dict(sys.modules, {"quarchpy.connection_specific.connection_QIS": SimpleNamespace(QisInterface=Mock(return_value=reader))}):
            with self.assertRaisesRegex(RuntimeError, "selected power channel"):
                backend.begin(EnergySink("L2", .01))
        self.assertIsNone(backend.active_profile)
        self.assertIsNone(backend.configured_connection)

    def test_unavailable_device_discards_configuration_cache(self):
        backend = QisBackend("USB::test", "10ms")
        backend.active_profile, backend.configured_connection = ("L2", False), ("usb", 8)
        backend.device = SimpleNamespace(send_command=Mock(return_value="Device USB::test Invalid or No Longer Valid"))
        backend.record_state()
        self.assertIsNone(backend.active_profile)
        self.assertIsNone(backend.configured_connection)

    def test_start_does_not_reconfigure_recording_or_disconnected_devices(self):
        for state in ["ON", "Device USB::test Invalid or No Longer Valid"]:
            with self.subTest(state=state):
                backend = QisBackend("USB::test", "10ms")
                backend.device = SimpleNamespace(send_command=Mock(return_value=state))
                backend._command = Mock()
                interface = Mock()
                with patch.dict(sys.modules, {"quarchpy.connection_specific.connection_QIS": SimpleNamespace(QisInterface=interface)}):
                    with self.assertRaisesRegex(RuntimeError, "not idle"):
                        backend.begin(EnergySink("L2", .01))
                backend._command.assert_not_called()
                interface.assert_not_called()

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
        with patch("quarch_capture.commands.send_command") as send, redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                main(["start", "--label", "sql_001", "--csv", "off"])
            self.assertEqual(raised.exception.code, 2)
            send.assert_not_called()
        self.assertIn("--channel", stderr.getvalue())

    def test_start_sends_channel_label_and_csv_switch(self):
        with patch("quarch_capture.commands.send_command", return_value={"ok": True}) as send, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(main(["start", "--channel", "l2", "--label", "sql_001", "--csv", "off"]), 0)
        send.assert_called_once_with(DEFAULT_SOCKET, {
            "command": "start", "label": "sql_001", "channel": "L2", "save_csv": False,
        }, 45)


class CompatibilityTests(unittest.TestCase):
    def test_existing_imports_use_the_split_implementations(self):
        from quarch_capture import recorder
        for name, implementation in {
            "EnergySink": EnergySink, "QisBackend": QisBackend, "Recorder": Recorder,
            "CommandHandler": CommandHandler, "LocalServer": LocalServer,
            "main": main, "send_command": send_command,
            "read_stream_block": read_stream_block,
            "selected_synthetic_functions": selected_synthetic_functions,
        }.items():
            with self.subTest(name=name):
                self.assertIs(getattr(recorder, name), implementation)
        self.assertEqual(recorder.DEFAULT_SOCKET, DEFAULT_SOCKET)

    def test_module_entry_points_still_require_channel_before_device_access(self):
        root = Path(__file__).resolve().parents[1]
        for module in ["quarch_capture", "quarch_capture.commands", "quarch_capture.recorder"]:
            with self.subTest(module=module):
                result = subprocess.run([sys.executable, "-m", module, "start", "--label", "sql_001"],
                                        capture_output=True, text=True, timeout=5, cwd=root)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("--channel", result.stderr)
                self.assertNotIn("RuntimeWarning", result.stderr)


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
