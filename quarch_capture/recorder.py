"""Local start/stop recorder. QIS stays connected; hardware recording is idle between sessions."""

import argparse
from contextlib import suppress
import csv
from datetime import datetime, timezone
import errno
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import socket
import socketserver
import stat
import sys
import threading
import time
import uuid


CHANNELS = ("L1", "L2", "L3")
DEFAULT_SOCKET = Path.home() / ".cache/quarch/recorder.sock"
READ_BATCH_INTERVAL_S = .1


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def interval_seconds(value):
    match = re.fullmatch(r"([1-9]\d*)(us|ms)", value.lower())
    if not match:
        raise argparse.ArgumentTypeError("Use a positive integer interval such as 125us or 10ms")
    amount, unit = match.groups()
    result = int(amount) / (1_000_000 if unit == "us" else 1000)
    if result < .000125:
        raise argparse.ArgumentTypeError("QTL2843 output intervals must be at least 125us")
    return result


def selected_synthetic_functions(definitions, channel, include_details):
    """Keep the chosen output's vendor functions, in dependency order."""
    if channel not in CHANNELS:
        raise ValueError("channel must be L1, L2 or L3")
    functions = []
    power_found = False
    for definition in definitions:
        function = definition.function
        destination = re.match(r"\s*chan\(\s*([^,\s]+)\s*,\s*([^\s)]+)\s*\)", function)
        if destination is None:
            continue
        name, group = destination.groups()
        is_power = name == channel + "_PInst" and group == "VA"
        is_selected = name.startswith((channel + "_", channel + "V_"))
        if is_power or (include_details and is_selected):
            functions.append(function)
            power_found |= is_power
    if not power_found:
        raise ValueError(f"Instrument fixture has no instantaneous-power definition for {channel}")
    return functions


def read_stream_block(reader, device):
    """Read a bounded QIS text block and expose EOF so callers can batch safely.

    QIS 'eof' marks the end of currently buffered data, not the end of recording.
    Full blocks must be drained immediately rather than delayed between reads.
    """
    text = reader.send_command("stream text all", device=device).rstrip("\r\n")
    at_buffer_end = text.endswith("eof")
    if at_buffer_end:
        text = text[:-3]
    if text and not text.endswith("\n"):
        text += "\r\n"
    status = "running"
    if at_buffer_end or not text.strip():
        current = reader.send_command("stream?", device=device).lower()
        if "overrun" in current or "out of buffer" in current:
            status = "overrun"
        elif "stopped" in current:
            status = "stopped"
        elif "running" not in current:
            raise RuntimeError(f"Unexpected QIS streaming state: {current}")
    return status, text, at_buffer_end or not text.strip()


class EnergySink:
    """Bounded-memory interval integration, with optional selected-channel CSV."""

    def __init__(self, channel, period, csv_path=None):
        self.channel, self.period = channel, period
        self.csv_path = Path(csv_path) if csv_path else None
        self.handle = self.csv_path.open("x", newline="", encoding="utf-8") if self.csv_path else None
        self.writer = csv.writer(self.handle) if self.handle else None
        self.lock = threading.RLock()
        self.headers = None
        self.pending = ""
        self.count = 0
        self.sum_w = self.compensation = 0.0
        self.minimum = self.maximum = None
        self.first_t = self.last_t = None

    def set_header(self, header):
        with self.lock:
            headers = next(csv.reader([header]))
            timestamps = [i for i, c in enumerate(headers) if c.lower().startswith("time ")]
            if len(timestamps) != 1:
                raise ValueError("QIS returned an invalid timestamp header")
            self.time_index = timestamps[0]
            units = {"ns": 1_000_000_000, "us": 1_000_000, "ms": 1000, "s": 1}
            self.time_divisor = units[headers[self.time_index].split()[-1].lower()]
            power_name = f"{self.channel}_PInst VA mVA"
            if power_name not in headers:
                raise ValueError(f"QIS did not provide the selected power channel: {power_name}")
            self.power_index = headers.index(power_name)
            self.selected_indices = [self.time_index] + [
                i for i, c in enumerate(headers)
                if c.startswith((self.channel + " ", self.channel + "_", self.channel + "V_"))
            ]
            self.headers = headers
            if self.writer:
                self.writer.writerow([headers[i] for i in self.selected_indices])
                self.handle.flush()

    def feed(self, text):
        with self.lock:
            self.pending += text
            lines = self.pending.split("\n")
            self.pending = lines.pop()
            if len(self.pending) > 65536:
                raise ValueError("QIS returned an overlong incomplete row")
            for line in lines:
                if not line.strip():
                    continue
                row = next(csv.reader([line.rstrip("\r")]))
                if self.headers is None or len(row) != len(self.headers):
                    raise ValueError("Malformed QIS sample row")
                timestamp = float(row[self.time_index])
                power_w = float(row[self.power_index]) / 1000
                if not math.isfinite(timestamp) or not math.isfinite(power_w):
                    raise ValueError("Non-finite timestamp or selected-channel power")
                if self.last_t is not None and not math.isclose(
                    timestamp - self.last_t, self.period * self.time_divisor,
                    rel_tol=1e-6, abs_tol=1e-7,
                ):
                    raise ValueError("Missing, duplicate or out-of-order QIS samples")
                if self.first_t is None:
                    self.first_t = timestamp
                self.last_t = timestamp
                corrected = power_w - self.compensation
                combined = self.sum_w + corrected
                self.compensation = (combined - self.sum_w) - corrected
                self.sum_w = combined
                self.count += 1
                self.minimum = power_w if self.minimum is None else min(self.minimum, power_w)
                self.maximum = power_w if self.maximum is None else max(self.maximum, power_w)
                if self.writer:
                    self.writer.writerow([row[i] for i in self.selected_indices])

    def snapshot(self):
        with self.lock:
            duration = self.count * self.period
            joules = self.sum_w * self.period if self.count else None
            return {
                "samples": self.count, "interval_s": self.period, "duration_s": duration,
                "sample_start_s": self.first_t / self.time_divisor if self.first_t is not None else None,
                "sample_end_exclusive_s": self.last_t / self.time_divisor + self.period if self.last_t is not None else None,
                "selected_channel": self.channel,
                self.channel: {
                    "mean_power_W": self.sum_w / self.count if self.count else None,
                    "energy_J": joules, "energy_Wh": joules / 3600 if joules is not None else None,
                    "min_interval_mean_W": self.minimum, "max_interval_mean_W": self.maximum,
                },
            }

    def close(self):
        with self.lock:
            try:
                if self.pending.strip():
                    raise ValueError("Capture ended with an incomplete sample row")
            finally:
                if self.handle:
                    self.handle.close()


class QisBackend:
    """Only begin() issues rec stream; end() issues rec stop and drains QIS."""

    def __init__(self, device_target, resample):
        self.target, self.resample = device_target, resample
        self.qis = self.device = self.reader = self.thread = None
        self.owns_qis = False
        self.started_by_us = False
        self.error = None
        self.stop_requested = threading.Event()
        self.active_profile = None

    def initialize(self):
        from quarchpy.device import quarchDevice, quarchPPM
        from quarchpy.qis import isQisRunning, startLocalQis
        self.owns_qis = not isQisRunning()
        try:
            self.qis = startLocalQis(headless=True)
            if self.qis is None:
                raise RuntimeError("Could not start headless QIS")
            self.version = self.qis.send_command("$version")
            self.qis.qis_scan_devices()
            self.device = quarchDevice(self.target, ConType="QIS", timeout="15", qis_instance=self.qis)
            self.identity = self.device.send_command("*IDN?")
            if "2843" not in self.identity:
                raise RuntimeError("Selected device is not a QTL2843")
            if self.record_state().upper() != "OFF":
                raise RuntimeError("Instrument is already recording; existing recording was not stopped")
            for command in ["rec averaging 1", "rec:trigger:mode manual", "rec:trigger:pre 0"]:
                self._command(command)
            # Create functions per measurement, rather than computing all three
            # outputs' RMS/frequency/power channels for every energy-only run.
            self.power = quarchPPM(self.device, skipDefaultSyntheticChannels=True)
            self.power.fixture_definition = self.device.send_command("fix:chan:xml?")
            self.synthetic_definitions = self.power.parse_synthetic_channels_from_instrument()
            response = self.power.stream_resample_mode(self.resample)
            if "fail" in response.lower() or "invalid" in response.lower():
                raise RuntimeError(response)
        except Exception:
            self.close()
            raise

    def _command(self, command):
        response = self.device.send_command(command)
        if not response.strip().upper().startswith("OK"):
            raise RuntimeError(f"{command}: {response}")

    def record_state(self):
        return self.device.send_command("rec?").strip() if self.device else "DISCONNECTED"

    @property
    def reader_active(self):
        return bool(self.thread and self.thread.is_alive())

    def configure_channels(self, channel, include_details):
        profile = (channel, include_details)
        functions = selected_synthetic_functions(self.synthetic_definitions, channel, include_details)
        if profile != self.active_profile:
            self.active_profile = None
            self._command("stream created channels clear")
            for function in functions:
                self._command("stream create channel " + function)
            self.active_profile = profile
        return {
            "processing_profile": "selected_channel_details" if include_details else "energy_only",
            "synthetic_channels": len(functions),
            "read_batch_interval_s": READ_BATCH_INTERVAL_S,
        }

    def begin(self, sink):
        from quarchpy.connection_specific.connection_QIS import QisInterface
        if self.reader_active or self.record_state().upper() != "OFF":
            raise RuntimeError("Instrument is not idle")
        profile = self.configure_channels(sink.channel, include_details=sink.csv_path is not None)
        self.reader = QisInterface(host=self.qis.host, port=self.qis.port, connectionMessage=False)
        self.error = None
        self.stop_requested.clear()
        ready = threading.Event()
        self.started_by_us = True
        try:
            self._command("rec stream")
        except Exception:
            with suppress(Exception):
                self._command("rec stop")
            self._close_interface(self.reader)
            self.reader = None
            raise
        started = {"record_start_ack_at": utc_now(), "record_start_ack_monotonic": time.monotonic(), **profile}
        reader = self.reader

        def consume():
            try:
                deadline = time.monotonic() + 10
                while True:
                    header_state = reader.sendAndReceiveText(reader.sock, "stream text header", self.device.ConString)
                    if "Header Not Available" not in header_state:
                        header = reader.stream_header_format(self.device.ConString)
                        break
                    if time.monotonic() > deadline:
                        raise TimeoutError("QIS stream header was not available")
                    time.sleep(.01)
                sink.set_header(header)
                ready.set()
                while True:
                    status, stripes, at_buffer_end = read_stream_block(reader, self.device.ConString)
                    if stripes:
                        sink.feed(stripes.replace(" ", ","))
                    if "overrun" in status:
                        raise RuntimeError("QIS/device buffer overrun")
                    if status == "stopped" and at_buffer_end:
                        if not self.stop_requested.is_set():
                            raise RuntimeError("Instrument stopped before a stop command was requested")
                        break
                    if at_buffer_end:
                        # This batches delivery, not sampling. The instrument
                        # still outputs every 10ms (or the requested interval).
                        # A stop command wakes this wait immediately for drain.
                        if self.stop_requested.is_set():
                            # rec stop can still be awaiting the hardware ACK;
                            # do not busy-poll while that command completes.
                            time.sleep(.005)
                        else:
                            self.stop_requested.wait(READ_BATCH_INTERVAL_S)
            except Exception as exc:
                self.error = str(exc)
                if not self.stop_requested.is_set():
                    with suppress(Exception):
                        self._command("rec stop")
            finally:
                ready.set()

        self.thread = threading.Thread(target=consume, name="quarch-command-stream", daemon=True)
        self.thread.start()
        if not ready.wait(12) or self.error:
            with suppress(Exception):
                self.end()
            raise RuntimeError(self.error or "Timed out starting the QIS reader")
        return started

    def end(self):
        self.stop_requested.set()
        try:
            self._command("rec stop")
            metadata = {"record_stop_ack_at": utc_now()}
            if self.thread:
                self.thread.join(20)
            if self.reader_active:
                raise TimeoutError("QIS reader did not drain within 20 seconds")
            if self.error:
                raise RuntimeError(self.error)
            state = self.record_state()
            if state.upper() != "OFF":
                raise RuntimeError(f"Instrument did not stop: {state}")
            metadata["device_record_state"] = state
            self.started_by_us = False
            return metadata
        finally:
            self._close_interface(self.reader)
            if self.thread and self.thread.is_alive():
                self.thread.join(1)
            self.reader = None

    @staticmethod
    def _close_interface(interface):
        if interface:
            for name in ["streamSock", "sock"]:
                sock = getattr(interface, name, None)
                if sock:
                    with suppress(Exception):
                        sock.shutdown(socket.SHUT_RDWR)
                    with suppress(Exception):
                        sock.close()

    def close(self):
        if self.reader is not None:
            with suppress(Exception):
                self.end()
        if self.device and self.started_by_us:
            with suppress(Exception):
                self._command("rec stop")
        if self.device:
            with suppress(Exception):
                self.device.close_connection()
            self.device = None
        self._close_interface(self.qis)
        self.qis = None
        if self.owns_qis:
            from quarchpy.qis import closeQis
            with suppress(Exception):
                closeQis()
            self.owns_qis = False


class Recorder:
    def __init__(self, backend, channel, period, output_dir, save_csv=False):
        self.backend, self.channel, self.period = backend, channel, period
        self.output_dir, self.save_csv = Path(output_dir).resolve(), save_csv
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.session = self.last_result = None
        self.fault = None
        self.closing = False

    def start(self, label="measurement", channel=None, save_csv=None):
        with self.lock:
            if self.closing or self.fault:
                raise RuntimeError(self.fault or "Recorder is shutting down")
            if self.session is not None:
                raise RuntimeError("A measurement is already active; stop it first")
            if not isinstance(channel, str) or channel.upper() not in CHANNELS:
                raise ValueError("channel is required for every start; choose L1, L2 or L3")
            channel = channel.upper()
            if not isinstance(label, str) or not re.fullmatch(r"[\w.-]{1,80}", label) or label in [".", ".."]:
                raise ValueError("label must contain 1-80 letters, digits, underscores, dots or hyphens")
            if save_csv is not None and not isinstance(save_csv, bool):
                raise ValueError("save_csv must be true or false")
            save_csv = self.save_csv if save_csv is None else save_csv
            name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ") + f"_{label}_{channel}_{uuid.uuid4().hex[:6]}"
            csv_path = self.output_dir / (name + ".csv") if save_csv else None
            sink = EnergySink(channel, self.period, csv_path)
            session = {
                "session_id": name, "label": label, "selected_channel": channel,
                "csv_enabled": save_csv, "csv_path": str(csv_path) if csv_path else None,
                "summary_path": str(self.output_dir / (name + ".energy.json")),
                "start_requested_at": utc_now(), "sink": sink,
            }
            try:
                session.update(self.backend.begin(sink))
            except Exception:
                with suppress(Exception):
                    sink.close()
                try:
                    if self.backend.record_state().upper() != "OFF" or self.backend.reader_active:
                        self.fault = "Start failed and the instrument could not be confirmed idle; shut down and inspect the device"
                except Exception as exc:
                    self.fault = f"Start failed; recording state is unknown: {exc}"
                raise
            self.session = session
            return {"state": "recording", **{k: v for k, v in session.items() if k not in ["sink", "record_start_ack_monotonic"]}}

    def stop(self):
        with self.lock:
            if self.session is None:
                if self.last_result is not None:
                    return {"state": "error" if self.fault or not self.last_result["complete"] else "idle", "already_stopped": True, "result": self.last_result}
                raise RuntimeError("No active measurement")
            session = self.session
            requested_mono = time.monotonic()
            result = {k: v for k, v in session.items() if k not in ["sink", "record_start_ack_monotonic"]}
            result["stop_requested_at"] = utc_now()
            result["command_window_s"] = max(0, requested_mono - session["record_start_ack_monotonic"])
            errors = []
            try:
                result.update(self.backend.end())
            except Exception as exc:
                errors.append(str(exc))
            try:
                result["device_record_state"] = self.backend.record_state()
                if result["device_record_state"].upper() != "OFF" or self.backend.reader_active:
                    self.fault = "Instrument/reader could not be confirmed idle; shut down and check the device before restarting"
                    errors.append(self.fault)
            except Exception as exc:
                self.fault = f"Cannot confirm recording is OFF: {exc}"
                errors.append(self.fault)
            try:
                session["sink"].close()
            except Exception as exc:
                errors.append(str(exc))
            result.update(session["sink"].snapshot())
            if not result["samples"]:
                errors.append("No output samples were received; the interval may have been too short")
            result.update({
                "complete": not errors, "errors": errors,
                "method": "Sum of selected-channel interval-mean instantaneous power times output interval",
                "negative_power_policy": "Preserved; net energy",
                "boundary_note": "Energy duration is the number of device output bins times their interval; command latency and interval quantization can differ from wall time.",
            })
            self.session = None
            self.last_result = result
            Path(result["summary_path"]).write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
            return {"state": "idle" if not errors else "error", "result": result}

    def status(self):
        with self.lock:
            device_state = self.backend.record_state()
            reader_active = self.backend.reader_active
            state = "error" if self.fault else "recording" if self.session else "idle"
            if self.session is None and (device_state.upper() != "OFF" or reader_active):
                state = "error"
            error = getattr(self.backend, "error", None)
            if error and self.session:
                state = "error"
            result = {
                "state": state, "pid": os.getpid(), "configured_channel": self.channel,
                "start_channel_required": True,
                "default_csv_enabled": self.save_csv, "interval_s": self.period,
                "device_record_state": device_state,
                "reader_active": reader_active,
            }
            if self.session:
                result["active_session"] = {
                    "session_id": self.session["session_id"], "label": self.session["label"],
                    "csv_enabled": self.session["csv_enabled"],
                    **self.session["sink"].snapshot(),
                }
            if self.last_result:
                result["last_result"] = self.last_result
            if error:
                result["stream_error"] = error
            if self.fault:
                result["fault"] = self.fault
            return result

    def request_shutdown(self):
        with self.lock:
            self.closing = True
            response = self.stop() if self.session else {"state": "error" if self.fault else "idle"}
            response["shutdown"] = True
            return response

    def close(self):
        with self.lock:
            self.closing = True
            if self.session:
                with suppress(Exception):
                    self.stop()
            self.backend.close()


class LocalServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


class CommandHandler(socketserver.StreamRequestHandler):
    def handle(self):
        shutdown = False
        try:
            self.connection.settimeout(10)
            raw = self.rfile.readline(65537)
            if len(raw) > 65536 or not raw.endswith(b"\n"):
                raise ValueError("Invalid command frame")
            request = json.loads(raw)
            command = request.get("command")
            recorder = self.server.recorder
            if command == "start":
                payload = recorder.start(request.get("label", "measurement"), request.get("channel"), request.get("save_csv"))
            elif command == "stop":
                payload = recorder.stop()
            elif command == "status":
                payload = recorder.status()
            elif command == "shutdown":
                payload = recorder.request_shutdown()
                shutdown = True
            else:
                raise ValueError("Unknown command")
            response = {"ok": payload.get("state") != "error", **payload}
        except Exception as exc:
            response = {"ok": False, "error": str(exc)}
        try:
            self.wfile.write((json.dumps(response, ensure_ascii=False, allow_nan=False) + "\n").encode())
        finally:
            if shutdown:
                threading.Thread(target=self.server.shutdown, daemon=True).start()


def send_command(socket_path, request, timeout=45):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(str(socket_path))
        client.sendall((json.dumps(request, ensure_ascii=False) + "\n").encode())
        with client.makefile("rb") as stream:
            response = stream.readline(1_048_577)
        if len(response) > 1_048_576 or not response.endswith(b"\n"):
            raise RuntimeError("Invalid recorder response")
        return json.loads(response)


def serve(args):
    path = args.socket.expanduser().resolve()
    if len(os.fsencode(path)) > 100:
        raise ValueError("Unix socket path is too long")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # A separate device lock prevents two instances using different sockets
    # from recording the same instrument through this controller.
    key = hashlib.sha256(args.device.lower().encode()).hexdigest()[:16]
    DEFAULT_SOCKET.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    device_lock = DEFAULT_SOCKET.parent / ("device-" + key + ".lock")
    with device_lock.open("a+") as lock:
        os.chmod(device_lock, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("A recorder already owns this device")
        if path.exists():
            if not stat.S_ISSOCK(path.lstat().st_mode) or path.lstat().st_uid != os.getuid():
                raise RuntimeError("Refusing to replace a non-owned socket path")
            with socket.socket(socket.AF_UNIX) as existing:
                outcome = existing.connect_ex(str(path))
                if outcome == 0:
                    raise RuntimeError("A recorder is already listening on this socket")
                if outcome not in [errno.ECONNREFUSED, errno.ENOENT]:
                    raise RuntimeError("Could not confirm that the existing socket is stale")
            path.unlink()
        backend = QisBackend(args.device, args.resample)
        recorder = None
        socket_inode = None
        try:
            backend.initialize()
            recorder = Recorder(backend, args.channel, interval_seconds(args.resample), args.output_dir, args.csv == "on")
            with LocalServer(str(path), CommandHandler) as server:
                os.chmod(path, 0o600)
                socket_inode = path.stat().st_ino
                server.recorder = recorder
                previous = {}
                def interrupted(signum, frame):
                    threading.Thread(target=server.shutdown, daemon=True).start()
                for signum in [signal.SIGTERM, signal.SIGINT]:
                    previous[signum] = signal.signal(signum, interrupted)
                try:
                    print(json.dumps({"ready": True, "socket": str(path), "qis_version": backend.version, **recorder.status()}, ensure_ascii=False), flush=True)
                    server.serve_forever(poll_interval=.1)
                finally:
                    for signum, handler in previous.items():
                        signal.signal(signum, handler)
        finally:
            if recorder:
                recorder.close()
            else:
                backend.close()
            if socket_inode is not None and path.exists() and path.lstat().st_ino == socket_inode:
                path.unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m quarch_capture", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    service = commands.add_parser("serve", help="Keep QIS connected, with recording OFF until start")
    service.add_argument("--device", required=True)
    service.add_argument("--channel", type=str.upper, choices=CHANNELS, required=True)
    service.add_argument("--resample", default="10ms", help="QIS output interval (default: 10ms / 100 Hz)")
    service.add_argument("--csv", choices=["on", "off"], default="off", help="Default CSV switch (default: off)")
    service.add_argument("--output-dir", type=Path, default=Path("results/sessions"))
    service.add_argument("--socket", type=Path, default=DEFAULT_SOCKET)
    for command in ["start", "stop", "status", "shutdown"]:
        sub = commands.add_parser(command)
        sub.add_argument("--socket", type=Path, default=DEFAULT_SOCKET)
        sub.add_argument("--timeout", type=float, default=45)
        if command == "start":
            sub.add_argument("--label", default="measurement")
            sub.add_argument("--channel", type=str.upper, choices=CHANNELS, required=True,
                             help="Output for this measurement; must be specified for every start")
            sub.add_argument("--csv", choices=["on", "off"], default=None, help="Override the server CSV switch for this measurement")
    args = parser.parse_args(argv)
    if args.command == "serve":
        interval_seconds(args.resample)
        serve(args)
        return 0
    request = {"command": args.command}
    if args.command == "start":
        request.update(label=args.label, channel=args.channel, save_csv=None if args.csv is None else args.csv == "on")
    response = send_command(args.socket.expanduser(), request, args.timeout)
    print(json.dumps(response, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if response.get("ok") else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
