"""QTL2843 communication, stream configuration and buffered reading through QIS."""

from contextlib import suppress
import re
import socket
import threading
import time

from .common import CHANNELS, utc_now


READ_BATCH_INTERVAL_S = .1


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
