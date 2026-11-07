"""Exercise live samples and process exits through a real local socket."""

from contextlib import redirect_stderr
import io
import json
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch
import uuid

from quarch_capture.stream_data import EnergySink
from quarch_capture.watch import parse_args, read_frame
from test_measurement_failures import SimulatedBackend, simulated_service


LAUNCHER = Path(__file__).resolve().parents[1] / "bin" / "Quarch"


class StreamingBackend(SimulatedBackend):
    def begin(self, sink):
        result = super().begin(sink)
        self.stopped = threading.Event()

        def produce():
            index = 0
            try:
                while not self.stopped.is_set():
                    sink.feed(f"{index * sink.period * 1_000_000:.3f},123456\n")
                    index += 1
                    self.stopped.wait(sink.period)
            except Exception as exc:
                self.error = str(exc)

        self.thread = threading.Thread(target=produce)
        self.thread.start()
        return result

    def end(self):
        self.stopped.set()
        self.thread.join(timeout=2)
        return super().end()


class WatchTests(unittest.TestCase):
    def launch(self, path, *args):
        child = subprocess.Popen([sys.executable, str(LAUNCHER), "watch", "--L2", "--socket", str(path), *args],
                                 cwd="/tmp", stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(child.stderr.close)
        self.addCleanup(child.stdout.close)
        self.addCleanup(lambda: child.poll() is None and child.kill())
        return child

    def wait_for(self, condition):
        deadline = time.monotonic() + 5
        while not condition():
            if time.monotonic() > deadline:
                self.fail("Timed out waiting for live recording state")
            time.sleep(.01)

    def test_required_channel_default_and_interval_validation(self):
        self.assertEqual(parse_args(["--L2"]).interval, "10ms")
        self.assertEqual(parse_args(["--L3", "--100ms"]).interval, "100ms")
        self.assertEqual(parse_args(["--L1", "--125us"]).channel, "L1")
        for args in [[], ["--L1", "--L2"], ["--L2", "--10us"], ["--L2", "--0ms"]]:
            with self.subTest(args=args), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parse_args(args)

    def test_callback_preserves_actual_timestamps_and_watts(self):
        received = []
        sink = EnergySink("L2", .01, on_sample=lambda *sample: received.append(sample))
        sink.set_header("Time uS,L2_PInst VA mVA")
        sink.feed("25000,123456\n35000,-")
        sink.feed("2000\n")
        sink.close()
        self.assertEqual(received, [(.025, 123.456), (.035, -2.0)])

    def test_interrupts_stop_and_restore_interval(self):
        for args, signum, period in [((), signal.SIGINT, .01), (("--10ms",), signal.SIGINT, .01),
                                     (("--20ms",), signal.SIGTERM, .02)]:
            with self.subTest(args=args, signal=signum), simulated_service(StreamingBackend()) as (_, backend, recorder, path):
                child = self.launch(path, *args)
                self.wait_for(lambda: recorder.session is not None and recorder.session["sink"].count >= 4)
                child.send_signal(signum)
                output, errors = child.communicate(timeout=5)
                self.assertEqual(child.returncode, 130, errors)
                lines = output.splitlines()
                self.assertEqual(lines[0], "time_s,L2_power_W")
                samples = [tuple(map(float, row.split(","))) for row in lines[1:]]
                self.assertGreaterEqual(len(samples), 3)
                for previous, current in zip(samples, samples[1:]):
                    self.assertAlmostEqual(current[0] - previous[0], period)
                self.assertTrue(all(power == 123.456 for _, power in samples))
                self.assertFalse(backend.active)
                self.assertIsNone(recorder.session)
                self.assertEqual((backend.resample, backend.read_batch_interval_s), ("10ms", .1))
                self.assertTrue(recorder.last_result["complete"])
                self.assertFalse(recorder.last_result["csv_enabled"])

    def test_connection_loss_stops_recording(self):
        with simulated_service(StreamingBackend()) as (_, backend, recorder, path):
            with socket.socket(socket.AF_UNIX) as connection:
                connection.connect(str(path))
                connection.sendall((json.dumps({"command": "watch", "channel": "L2", "resample": "100ms",
                                                "request_id": uuid.uuid4().hex, "start_deadline": time.time() + 45}) + "\n").encode())
                with connection.makefile("rb") as stream:
                    self.assertEqual(read_frame(stream)["event"], "ready")
                    self.assertEqual(read_frame(stream)["power_W"], 123.456)
            self.wait_for(lambda: recorder.session is None)
            self.assertFalse(backend.active)
            self.assertEqual(backend.resample, "10ms")

    def test_closed_output_pipe_stops_recording(self):
        with simulated_service(StreamingBackend()) as (_, backend, recorder, path):
            child = self.launch(path)
            self.wait_for(lambda: recorder.session is not None and recorder.session["sink"].count >= 3)
            child.stdout.close()
            child.wait(timeout=5)
            self.assertEqual(child.returncode, 0, child.stderr.read())
            self.assertFalse(backend.active)
            self.assertIsNone(recorder.session)

    def test_busy_measurement_is_not_interrupted(self):
        with simulated_service() as (_, backend, recorder, path):
            started = recorder.start(channel="L2", label="SQL")
            backend.emit()
            child = self.launch(path)
            _, errors = child.communicate(timeout=5)
            self.assertEqual(child.returncode, 1, errors)
            self.assertTrue(backend.active)
            self.assertEqual(recorder.session["session_id"], started["session_id"])

    def test_failed_watch_start_restores_previous_interval(self):
        with simulated_service() as (_, backend, recorder, _):
            with patch.object(backend, "begin", side_effect=RuntimeError("start failed")):
                with self.assertRaisesRegex(RuntimeError, "start failed"):
                    recorder.start(channel="L2", resample="100ms", on_sample=lambda *_: None)
            self.assertEqual((backend.resample, backend.read_batch_interval_s), ("10ms", .1))
            self.assertEqual(recorder.status()["state"], "idle")

    def test_reader_failure_stops_watch_and_reports_error(self):
        with simulated_service(StreamingBackend()) as (_, backend, recorder, path):
            child = self.launch(path)
            self.wait_for(lambda: recorder.session is not None and recorder.session["sink"].count >= 3)
            backend.error = "injected reader error"
            _, errors = child.communicate(timeout=5)
            self.assertEqual(child.returncode, 1, errors)
            self.assertIn("injected reader error", errors)
            self.assertFalse(backend.active)
            self.assertIsNone(recorder.session)

    def test_missing_service_gives_start_instruction(self):
        child = self.launch(Path("/tmp/nonexistent-quarch-watch.sock"))
        _, errors = child.communicate(timeout=5)
        self.assertEqual(child.returncode, 1)
        self.assertIn("serve first", errors)


if __name__ == "__main__":
    unittest.main()
