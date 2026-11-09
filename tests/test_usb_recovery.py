"""USB reconnection must recover new measurements without hiding partial ones."""

from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from quarch_capture.client import require_idle
from quarch_capture.commands import send_command
from quarch_capture.qis_backend import QisBackend, bounded_qis, read_stream_block
from test_measurement_failures import SimulatedBackend, simulated_service


class ReplugBackend(SimulatedBackend):
    def __init__(self):
        super().__init__()
        self.reconnect_needed = False
        self.connected = True
        self.reconnect_count = 0

    def record_state(self):
        if not self.connected:
            self.reconnect_needed = True
            return "DISCONNECTED"
        return super().record_state()

    def set_resample(self, resample):
        if not self.connected:
            raise ConnectionError("USB disconnected")
        super().set_resample(resample)

    def reconnect(self, resample):
        if not self.connected:
            raise ConnectionError("USB disconnected")
        self.reconnect_count += 1
        self.reconnect_needed = False
        self.error = None
        self.active = False
        self.set_resample(resample)


class UsbRecoveryTests(unittest.TestCase):
    def test_recovered_averaging_retries_only_transient_idle_state(self):
        for state, error in [("OFF", "FAIL: 0x41 -Failed to change state of action"),
                             ("ON", "FAIL: 0x41 -Failed to change state of action"),
                             ("OFF", "FAIL: 0x11 -Bad Command")]:
            with self.subTest(state=state, error=error):
                backend = QisBackend("USB::test", "10ms")
                backend.record_state = Mock(return_value=state)
                backend.device = Mock()
                backend.device.send_command.side_effect = [error, "OK"]
                with patch("quarch_capture.qis_backend.time.sleep"):
                    if state == "OFF" and "0x41" in error:
                        backend._command("rec:averaging:group 0 1")
                        self.assertEqual(backend.device.send_command.call_count, 2)
                    else:
                        with self.assertRaises(RuntimeError):
                            backend._command("rec:averaging:group 0 1")
                        self.assertEqual(backend.device.send_command.call_count, 1)

    def test_socket_timeout_and_eof_end_reads_without_vendor_retries(self):
        interface = Mock(maxRxBytes=4096)
        bounded_qis(interface)
        interface.sock.settimeout.assert_called_once_with(5)
        interface.sock.recv.side_effect = TimeoutError("USB/QIS stalled")
        with self.assertRaises(TimeoutError):
            interface.rx_bytes(interface.sock)
        interface.sock.recv.assert_called_once_with(4096)
        interface.sock.recv.side_effect = None
        interface.sock.recv.return_value = b""
        with self.assertRaisesRegex(ConnectionError, "closed"):
            interface.rx_bytes(interface.sock)

    def test_watch_preflight_recovers_error_and_preserves_failed_result(self):
        with simulated_service(ReplugBackend()) as (_, backend, recorder, path):
            recorder.start(channel="L2", resample="100ms")
            backend.connected = False
            result = recorder.stop()["result"]
            self.assertFalse(result["complete"])
            saved = Path(result["summary_path"]).read_bytes()
            self.assertIsNotNone(recorder.output_override)
            backend.connected = True
            status = require_idle(path)
            self.assertEqual(status["state"], "idle")
            self.assertTrue(status["reconnected"])
            self.assertEqual(backend.reconnect_count, 1)
            self.assertEqual(backend.resample, "10ms")
            self.assertIsNone(recorder.output_override)
            self.assertIsNone(recorder.fault)
            self.assertFalse(status["last_result"]["complete"])
            self.assertEqual(Path(result["summary_path"]).read_bytes(), saved)
            recorder.start(channel="L2")
            backend.emit()
            self.assertTrue(recorder.stop()["result"]["complete"])

    def test_direct_start_recovers_a_replug_missed_by_status(self):
        with simulated_service(ReplugBackend()) as (_, backend, recorder, _):
            backend.reconnect_needed = True
            recorder.start(channel="L2")
            self.assertEqual(backend.reconnect_count, 1)
            backend.emit()
            self.assertTrue(recorder.stop()["result"]["complete"])

    def test_missing_usb_can_be_retried_without_restarting_service(self):
        with simulated_service(ReplugBackend()) as (_, backend, recorder, path):
            backend.connected = False
            with self.assertRaisesRegex(RuntimeError, "disconnected"):
                require_idle(path)
            backend.connected = True
            self.assertEqual(require_idle(path)["state"], "idle")

    def test_recovery_does_not_interrupt_active_or_unsaved_measurements(self):
        with simulated_service(ReplugBackend()) as (_, backend, recorder, path):
            recorder.start(channel="L2")
            backend.reconnect_needed = True
            self.assertFalse(send_command(path, {"command": "recover"})["ok"])
            self.assertEqual(backend.reconnect_count, 0)
            backend.emit()
            with patch("quarch_capture.measurement.write_json_atomic", side_effect=OSError("disk full")):
                recorder.stop()
            self.assertFalse(send_command(path, {"command": "status"})["recovery_available"])
            self.assertFalse(send_command(path, {"command": "recover"})["ok"])
            self.assertEqual(backend.reconnect_count, 0)
            recorder.stop()  # Save before the fixture's teardown.

    def test_unrelated_fault_is_not_cleared_by_recovery(self):
        with simulated_service(ReplugBackend()) as (_, backend, recorder, path):
            recorder.fault = "unrelated failure"
            self.assertFalse(send_command(path, {"command": "recover"})["ok"])
            self.assertEqual(recorder.fault, "unrelated failure")
            self.assertEqual(backend.reconnect_count, 0)

    def test_usb_generation_change_triggers_even_with_off_reply(self):
        backend = QisBackend("USB::test", "10ms")
        backend.attached_connection = ("old",)
        backend.device = SimpleNamespace(send_command=Mock(return_value="OFF"))
        with patch("quarch_capture.qis_backend.usb_connection_key", return_value=("new",)):
            self.assertEqual(backend.record_state(), "OFF")
            self.assertTrue(backend.reconnect_needed)

    def test_disconnect_reply_is_not_parsed_as_a_sample_row(self):
        reader = Mock()
        reader.send_command.return_value = "Device USB::test Invalid or No Longer Valid"
        with self.assertRaisesRegex(ConnectionError, "disconnected"):
            read_stream_block(reader, "USB::test")

    def test_non_streaming_cache_refreshes_only_our_idle_single_device_qis(self):
        for owned, extra, state in [(True, False, "OFF"), (False, False, "OFF"),
                                    (True, True, "OFF"), (True, False, "ON")]:
            with self.subTest(owned=owned, extra=extra, state=state):
                backend = QisBackend("USB::test", "10ms")
                backend.owns_qis = owned
                old = backend.qis = Mock(host="127.0.0.1", port=9722)
                details = "1) USB::test Stream:No Name:null"
                if extra:
                    details += "\n2) USB::other Stream:Yes"
                old.send_command.return_value = details
                backend.record_state = Mock(return_value=state)
                new = Mock()
                launch = Mock(return_value=new)
                device = Mock()
                device.send_command.return_value = "QTL2312-01"
                modules = {"quarchpy.qis": SimpleNamespace(startLocalQis=launch),
                           "quarchpy.device": SimpleNamespace(quarchDevice=Mock(return_value=device))}
                with patch.dict("sys.modules", modules), \
                        patch("quarch_capture.qis_backend.socket.create_connection", side_effect=ConnectionRefusedError):
                    if owned and not extra and state == "OFF":
                        backend._refresh_stream_support()
                        self.assertIs(backend.qis, new)
                        old.send_command.assert_any_call("$shutdown")
                        launch.assert_called_once()
                    else:
                        with self.assertRaisesRegex(RuntimeError, "non-streaming"):
                            backend._refresh_stream_support()
                        launch.assert_not_called()
                        self.assertNotIn(("$shutdown",), [call.args for call in old.send_command.call_args_list])

    def test_empty_scanner_cache_restarts_only_owned_qis(self):
        for owned in (True, False):
            with self.subTest(owned=owned):
                backend = QisBackend("USB::test", "10ms")
                backend.owns_qis = owned
                backend.qis = Mock(host="127.0.0.1", port=9722)
                interface = Mock()
                interface.qis_scan_devices.return_value = []
                backend._restart_qis = Mock()
                backend._refresh_stream_support = Mock()
                backend._load_fixture = Mock()
                backend.set_resample = Mock()
                backend.record_state = Mock(return_value="OFF")
                device = Mock()
                device.send_command.return_value = "QTL2312-01"
                modules = {"quarchpy.connection_specific.connection_QIS": SimpleNamespace(QisInterface=Mock(return_value=interface)),
                           "quarchpy.device": SimpleNamespace(quarchDevice=Mock(return_value=device))}
                with patch.dict("sys.modules", modules), \
                        patch("quarch_capture.qis_backend.usb_connection_key", return_value=("new",)):
                    backend.reconnect("10ms")
                self.assertEqual(backend._restart_qis.call_count, int(owned))

    def test_exited_qis_is_relaunched_only_when_owned(self):
        for owned in (True, False):
            with self.subTest(owned=owned):
                backend = QisBackend("USB::test", "10ms")
                backend.owns_qis = owned
                interface = Mock()
                interface.qis_scan_devices.return_value = ["USB::test"]
                backend._refresh_stream_support = Mock()
                backend._load_fixture = Mock()
                backend.set_resample = Mock()
                backend.record_state = Mock(return_value="OFF")
                device = Mock()
                device.send_command.return_value = "QTL2312-01"
                launch = Mock(return_value=interface)
                modules = {"quarchpy.connection_specific.connection_QIS": SimpleNamespace(QisInterface=Mock(side_effect=ConnectionRefusedError)),
                           "quarchpy.device": SimpleNamespace(quarchDevice=Mock(return_value=device)),
                           "quarchpy.qis": SimpleNamespace(startLocalQis=launch)}
                with patch.dict("sys.modules", modules), \
                        patch("quarch_capture.qis_backend.usb_connection_key", return_value=("new",)):
                    if owned:
                        backend.reconnect("10ms")
                        self.assertFalse(backend.connection_lost)
                        launch.assert_called_once()
                    else:
                        with self.assertRaises(ConnectionRefusedError):
                            backend.reconnect("10ms")
                        launch.assert_not_called()

    def test_reconnect_stops_only_a_recording_owned_by_this_backend(self):
        for owned in (True, False):
            with self.subTest(owned=owned):
                backend = QisBackend("USB::test", "10ms")
                backend.connection_lost = True
                backend.started_by_us = owned
                backend.qis = SimpleNamespace(host="localhost", port=9722)
                hardware = {"state": "ON"}
                sent = []

                def send(command):
                    sent.append(command)
                    if command == "*IDN?": return "QTL2312-01"
                    if command == "rec stop": hardware["state"] = "OFF"; return "OK"
                    return hardware["state"]

                device = SimpleNamespace(send_command=send)
                backend._load_fixture = Mock()
                backend.set_resample = Mock()
                qis = Mock(host="localhost", port=9722)
                qis.send_command.return_value = "1) USB::test Stream:Yes"
                module = SimpleNamespace(QisInterface=Mock(return_value=qis))
                with patch.dict("sys.modules", {"quarchpy.connection_specific.connection_QIS": module,
                                                "quarchpy.device": SimpleNamespace(quarchDevice=Mock(return_value=device))}), \
                        patch("quarch_capture.qis_backend.usb_connection_key", return_value=("new",)):
                    if owned:
                        backend.reconnect("10ms")
                        self.assertFalse(backend.connection_lost)
                        self.assertIn("rec stop", sent)
                        backend._load_fixture.assert_called_once()
                    else:
                        with self.assertRaisesRegex(RuntimeError, "unowned"):
                            backend.reconnect("10ms")
                        self.assertNotIn("rec stop", sent)
                        backend._load_fixture.assert_not_called()


if __name__ == "__main__":
    unittest.main()
