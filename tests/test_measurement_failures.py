"""Regression cases for lost replies, concurrent control and result persistence."""

from contextlib import contextmanager
import json
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
import uuid

from quarch_capture.commands import CommandHandler, LocalServer, send_command
from quarch_capture import client
from quarch_capture.common import write_json_atomic
from quarch_capture.measurement import Recorder
from tools.benchmark import tpch_energy


class SimulatedBackend:
    def __init__(self):
        self.active = self.reader_active = False
        self.error = None
        self.fail_stop = False
        self.begin_count = self.end_count = 0
        self.resample, self.read_batch_interval_s = "10ms", .1

    def set_resample(self, resample):
        if self.active:
            raise RuntimeError("Cannot change interval while recording")
        self.resample = resample

    def begin(self, sink):
        self.begin_count += 1
        self.sink = sink
        sink.set_header("Time uS,L2_PInst VA mVA")
        self.active = self.reader_active = True
        return {"record_start_ack_monotonic": time.monotonic()}

    def emit(self):
        self.sink.feed("0,100000\n10000,100000\n")

    def end(self):
        self.end_count += 1
        self.active = self.reader_active = False
        if self.fail_stop:
            raise RuntimeError("injected stream failure")
        return {"device_record_state": "OFF"}

    def record_state(self):
        return "ON" if self.active else "OFF"

    def close(self):
        self.active = self.reader_active = False


@contextmanager
def simulated_service(backend=None):
    with tempfile.TemporaryDirectory() as folder:
        output = Path(folder)
        backend = backend or SimulatedBackend()
        recorder = Recorder(backend, "L2", .01, output)
        socket_path = output / "test.sock"
        with LocalServer(str(socket_path), CommandHandler) as server:
            server.recorder = recorder
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01})
            thread.start()
            try:
                yield output, backend, recorder, socket_path
            finally:
                server.shutdown()
                thread.join(timeout=2)
                recorder.close()


def sql_timing():
    return {"elapsed_s": .8, "driver_wall_s": .81, "statement_count": 1}


class MeasurementFailureTests(unittest.TestCase):
    def measure(self, session, path, output):
        return tpch_energy.measure_batch(session, "SELECT 1", 1, path, "L2", "test", False, output / "capture.json")

    def test_lost_start_reply_cancels_only_its_request(self):
        with simulated_service() as (output, backend, recorder, path):
            def transport(sock, request):
                response = send_command(sock, request)
                if request["command"] == "start":
                    self.assertTrue(response["ok"])
                    raise TimeoutError("lost start reply")
                return response

            session = Mock()
            with patch.object(client, "send_command", side_effect=transport):
                with self.assertRaisesRegex(TimeoutError, "lost start reply"):
                    self.measure(session, path, output)
            self.assertFalse(backend.active)
            self.assertEqual(backend.end_count, 1)
            session.run.assert_not_called()
            self.assertFalse(json.loads((output / "capture.json").read_text())["complete"])

    def test_cancel_before_late_start_prevents_hardware_recording(self):
        with simulated_service() as (output, backend, recorder, path):
            request_id, deadline = uuid.uuid4().hex, time.time() + 45
            cancelled = send_command(path, {"command": "cancel_start", "request_id": request_id, "start_deadline": deadline})
            self.assertTrue(cancelled["ok"])
            response = send_command(path, {"command": "start", "channel": "L2", "request_id": request_id, "start_deadline": deadline})
            self.assertFalse(response["ok"])
            self.assertEqual(backend.begin_count, 0)

    def test_expired_start_never_records(self):
        with simulated_service() as (output, backend, recorder, path):
            with self.assertRaisesRegex(RuntimeError, "expired"):
                recorder.start(channel="L2", request_id=uuid.uuid4().hex, start_deadline=time.time() - 1)
            self.assertEqual(backend.begin_count, 0)

    def test_start_expiring_during_backend_setup_is_stopped(self):
        with simulated_service() as (output, backend, recorder, path):
            original_begin = backend.begin

            def delayed_begin(sink):
                values = original_begin(sink)
                time.sleep(.03)
                return values

            with patch.object(backend, "begin", side_effect=delayed_begin):
                with self.assertRaisesRegex(RuntimeError, "expired while starting"):
                    recorder.start(channel="L2", request_id=uuid.uuid4().hex, start_deadline=time.time() + .01)
            self.assertFalse(backend.active)
            self.assertEqual(backend.end_count, 1)

    def test_unreachable_cancellation_keeps_start_error_and_reports_uncertain_cleanup(self):
        replies = [{"ok": True, "state": "idle", "reader_active": False,
                    "device_record_state": "OFF", "protocol_version": 2},
                   TimeoutError("start reply lost"), ConnectionError("server unreachable"), ConnectionError("server unreachable")]
        with tempfile.TemporaryDirectory() as folder, patch.object(client, "send_command", side_effect=replies):
            with self.assertRaisesRegex(TimeoutError, "start reply lost") as raised:
                self.measure(Mock(), "fake", Path(folder))
        self.assertTrue(any("cancellation could not be confirmed" in note for note in raised.exception.__notes__))

    def test_early_external_stop_is_rejected_and_capture_is_preserved(self):
        with simulated_service() as (output, backend, recorder, path):
            def execute(_):
                backend.emit()
                self.assertTrue(send_command(path, {"command": "stop"})["ok"])
                return sql_timing()

            with self.assertRaisesRegex(RuntimeError, "before the SQL batch finished"):
                self.measure(Mock(run=execute), path, output)
            self.assertTrue((output / "capture.json").exists())
            self.assertEqual(backend.end_count, 1)

    def test_other_session_is_not_stopped_by_batch_cleanup(self):
        with simulated_service() as (output, backend, recorder, path):
            other_id = None

            def execute(_):
                nonlocal other_id
                backend.emit()
                self.assertTrue(send_command(path, {"command": "stop"})["ok"])
                other = send_command(path, {"command": "start", "channel": "L2", "label": "other"})
                other_id = other["session_id"]
                return sql_timing()

            with self.assertRaisesRegex(RuntimeError, "Session mismatch"):
                self.measure(Mock(run=execute), path, output)
            self.assertTrue(backend.active)
            self.assertEqual(recorder.session["session_id"], other_id)
            self.assertEqual(backend.end_count, 1)

    def test_sql_error_survives_stop_error_and_partial_result_is_saved(self):
        with simulated_service() as (output, backend, recorder, path):
            backend.fail_stop = True

            def execute(_):
                backend.emit()
                raise ValueError("original SQL failure")

            with self.assertRaisesRegex(ValueError, "original SQL failure") as raised:
                self.measure(Mock(run=execute), path, output)
            self.assertTrue(any("injected stream failure" in note for note in raised.exception.__notes__))
            capture = json.loads((output / "capture.json").read_text())
            self.assertFalse(capture["complete"])
            self.assertEqual(capture["L2"]["energy_J"], 2)
            self.assertFalse(backend.active)

    def test_cancel_after_lost_reply_does_not_stop_a_new_foreign_session(self):
        with simulated_service() as (output, backend, recorder, path):
            def transport(sock, request):
                response = send_command(sock, request)
                if request["command"] == "start":
                    backend.emit()
                    send_command(path, {"command": "stop"})
                    send_command(path, {"command": "start", "channel": "L2", "label": "other"})
                    raise TimeoutError("lost start reply")
                return response

            with patch.object(client, "send_command", side_effect=transport):
                with self.assertRaises(TimeoutError):
                    self.measure(Mock(), path, output)
            self.assertTrue(backend.active)
            self.assertEqual(recorder.session["label"], "other")
            self.assertEqual(backend.end_count, 1)

    def test_old_server_is_rejected_before_any_start(self):
        with patch.object(client, "send_command", return_value={"ok": True, "state": "idle"}) as send:
            with self.assertRaisesRegex(RuntimeError, "restart"):
                tpch_energy.require_idle("fake")
            self.assertEqual(send.call_count, 1)

    def test_failed_start_cannot_be_cancelled_as_idle_while_device_is_on(self):
        with simulated_service() as (output, backend, recorder, path):
            begin = backend.begin
            def fail_after_start(sink):
                begin(sink)
                raise RuntimeError("startup acknowledgement failed")
            token, deadline = uuid.uuid4().hex, time.time() + 45
            with patch.object(backend, "begin", side_effect=fail_after_start):
                response = send_command(path, {"command": "start", "channel": "L2",
                                               "request_id": token, "start_deadline": deadline})
            self.assertFalse(response["ok"])
            cancelled = send_command(path, {"command": "cancel_start", "request_id": token, "start_deadline": deadline})
            self.assertFalse(cancelled["ok"])
            self.assertFalse(cancelled["cancelled"])
            self.assertEqual(cancelled["device_record_state"], "ON")

    def test_stop_transport_failures_recover_own_recording_but_reject_the_window(self):
        for delivered, failures in [(False, 1), (True, 1), (False, 2)]:
            with self.subTest(delivered=delivered, failures=failures), simulated_service() as (output, backend, recorder, path):
                attempts = 0
                def transport(sock, request):
                    nonlocal attempts
                    if request["command"] == "stop":
                        attempts += 1
                        if attempts <= failures:
                            if delivered:
                                send_command(sock, request)
                            raise TimeoutError("stop reply lost")
                    return send_command(sock, request)
                def execute(_):
                    backend.emit()
                    return sql_timing()
                with patch.object(client, "send_command", side_effect=transport):
                    with self.assertRaisesRegex(TimeoutError, "stop reply lost") as raised:
                        self.measure(Mock(run=execute), path, output)
                self.assertFalse(backend.active)
                self.assertEqual(backend.end_count, 1)
                self.assertTrue(raised.exception.__notes__)
                self.assertTrue((output / "capture.json").exists())

    def test_sigterm_cleans_up_the_owned_measurement(self):
        with simulated_service() as (output, backend, recorder, path):
            script = ("import sys,time\nfrom pathlib import Path\n"
                      "from quarch_capture.client import capture_window\n"
                      "try:\n"
                      " with capture_window(socket_path=Path(sys.argv[1]),channel='L2',label='signal',capture_path=Path(sys.argv[2])):\n"
                      "  time.sleep(60)\n"
                      "except KeyboardInterrupt: sys.exit(130)\n")
            child = subprocess.Popen([sys.executable, "-c", script, str(path), str(output / "capture.json")],
                                     cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 5
                while recorder.session is None:
                    if child.poll() is not None or time.monotonic() > deadline:
                        self.fail("Child did not start a measurement")
                    time.sleep(.01)
                child.send_signal(signal.SIGTERM)
                _, stderr = child.communicate(timeout=5)
                self.assertEqual(child.returncode, 130, stderr)
                self.assertFalse(backend.active)
                self.assertEqual(backend.end_count, 1)
                self.assertTrue((output / "capture.json").exists())
            finally:
                if child.poll() is None:
                    child.kill()
                    child.communicate()

    def test_first_interrupt_during_stop_still_recovers_the_recording(self):
        with simulated_service() as (output, backend, recorder, path):
            interrupted = False
            def transport(sock, request):
                nonlocal interrupted
                if request["command"] == "stop" and not interrupted:
                    interrupted = True
                    raise KeyboardInterrupt("SIGTERM during stop")
                return send_command(sock, request)
            def execute(_):
                backend.emit()
                return sql_timing()
            with patch.object(client, "send_command", side_effect=transport):
                with self.assertRaisesRegex(KeyboardInterrupt, "SIGTERM during stop"):
                    self.measure(Mock(run=execute), path, output)
            self.assertFalse(backend.active)
            self.assertTrue((output / "capture.json").exists())

    def test_unreachable_stop_reports_uncertainty_instead_of_claiming_cleanup(self):
        with simulated_service() as (output, backend, recorder, path):
            def transport(sock, request):
                if request["command"] in ["stop", "cancel_start"]:
                    raise TimeoutError("server unreachable")
                return send_command(sock, request)
            def execute(_):
                backend.emit()
                return sql_timing()
            with patch.object(client, "send_command", side_effect=transport):
                with self.assertRaises(TimeoutError) as raised:
                    self.measure(Mock(run=execute), path, output)
            self.assertTrue(any("could not be confirmed" in note for note in raised.exception.__notes__))
            self.assertTrue(backend.active)


class PersistenceTests(unittest.TestCase):
    def test_chinese_labels_do_not_become_filesystem_names(self):
        for save_csv in [False, True]:
            with self.subTest(csv=save_csv), simulated_service() as (output, backend, recorder, path):
                label = "测" * 80
                recorder.start(label, channel="L2", save_csv=save_csv)
                backend.emit()
                result = recorder.stop()["result"]
                self.assertTrue(result["complete"])
                self.assertTrue(result["summary_saved"])
                self.assertEqual(result["label"], label)
                self.assertLess(len(Path(result["summary_path"]).name.encode()), 100)
                self.assertEqual(json.loads(Path(result["summary_path"]).read_text())["label"], label)

    def test_shutdown_refuses_to_drop_an_unsaved_result_and_can_be_retried(self):
        with simulated_service() as (output, backend, recorder, path):
            recorder.start(channel="L2")
            backend.emit()
            with patch("quarch_capture.measurement.write_json_atomic", side_effect=OSError("disk full")):
                refused = send_command(path, {"command": "shutdown"})
                self.assertFalse(refused["ok"])
                self.assertFalse(refused["shutdown"])
                self.assertFalse(recorder.closing)
                self.assertTrue(send_command(path, {"command": "status"}).get("last_result"))
            self.assertTrue(send_command(path, {"command": "stop"})["ok"])
            self.assertTrue(send_command(path, {"command": "shutdown"})["shutdown"])

    def test_force_shutdown_is_an_explicit_override_for_failed_save(self):
        with simulated_service() as (output, backend, recorder, path):
            recorder.start(channel="L2")
            backend.emit()
            with patch("quarch_capture.measurement.write_json_atomic", side_effect=OSError("disk full")):
                response = send_command(path, {"command": "shutdown", "force": True})
                self.assertTrue(response["shutdown"])
                self.assertFalse(response["result"]["summary_saved"])
                self.assertTrue(recorder.closing)

    def test_failed_summary_save_is_reported_and_retried(self):
        with simulated_service() as (output, backend, recorder, path):
            started = recorder.start(channel="L2")
            backend.emit()
            with patch("quarch_capture.measurement.write_json_atomic", side_effect=OSError("disk full")):
                stopped = send_command(path, {"command": "stop", "session_id": started["session_id"]})
                self.assertFalse(stopped["ok"])
                self.assertFalse(stopped["result"]["summary_saved"])
                self.assertEqual(recorder.status()["state"], "error")
                with self.assertRaisesRegex(RuntimeError, "retry stop"):
                    recorder.start(channel="L2")
            retried = send_command(path, {"command": "stop", "session_id": started["session_id"]})
            self.assertTrue(retried["ok"])
            self.assertTrue(retried["result"]["summary_saved"])
            saved = json.loads(Path(retried["result"]["summary_path"]).read_text())
            self.assertEqual(saved, retried["result"])
            self.assertNotIn("summary_save_error", saved)

    def test_atomic_replace_failure_preserves_old_file_and_removes_temporary_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "summary.json"
            write_json_atomic(path, {"old": True})
            with patch("quarch_capture.common.os.replace", side_effect=OSError("disk failure")):
                with self.assertRaises(OSError):
                    write_json_atomic(path, {"new": True})
            self.assertEqual(json.loads(path.read_text()), {"old": True})
            self.assertEqual(list(Path(folder).glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
