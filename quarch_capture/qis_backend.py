"""PAM communication, fixture channels and buffered reading through QIS."""

from contextlib import suppress
from pathlib import Path
import socket
import threading
import time

from .common import interval_seconds, utc_now
from .channels import (available_power_channels, fixture_analog_groups, instrument_model,
                       selected_synthetic_functions)


READ_BATCH_INTERVAL_S = .1


def bounded_qis(interface):
    """Use socket timeouts instead of the vendor's repeated 10-second waits."""
    for name in ("sock", "streamSock"):
        getattr(interface, name).settimeout(5)

    def receive(sock):
        data = sock.recv(interface.maxRxBytes)
        if not data:
            raise ConnectionError("QIS connection closed while awaiting a response")
        return data

    interface.rx_bytes = receive
    return interface


def usb_connection_key(target, root=Path("/sys/bus/usb/devices")):
    """Identify this local USB enumeration; None means caching is unsafe."""
    transport, _, serial = target.partition(":")
    if transport.upper() != "USB":
        return None
    serial = serial.lstrip(":").casefold()
    for node in root.glob("*"):
        try:
            if ((node / "idVendor").read_text().strip() != "16d0"
                    or (node / "idProduct").read_text().strip() != "0449"
                    or (node / "serial").read_text().strip().casefold() != serial):
                continue
            stat = node.stat()
            return (str(node.resolve()), (node / "busnum").read_text().strip(),
                    (node / "devnum").read_text().strip(), stat.st_ino, stat.st_ctime_ns)
        except OSError:
            continue
    return None


def read_stream_block(reader, device):
    """Read a bounded QIS text block and expose EOF so callers can batch safely.

    QIS 'eof' marks the end of currently buffered data, not the end of recording.
    Full blocks must be drained immediately rather than delayed between reads.
    """
    text = reader.send_command("stream text all", device=device).rstrip("\r\n")
    if "invalid or no longer valid" in text.lower() or text.lstrip().upper().startswith("FAIL"):
        raise ConnectionError(f"Instrument stream disconnected: {text}")
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
        self.read_batch_interval_s = READ_BATCH_INTERVAL_S
        self.active_profile = self.configured_connection = None
        self.model = None
        self.analog_groups = {}
        self.minimum_interval_s = .000125
        self.attached_connection = None
        self.connection_lost = False
        self.qis_endpoint = ("127.0.0.1", 9722)

    def initialize(self):
        self.active_profile = self.configured_connection = None
        from quarchpy.device import quarchDevice
        from quarchpy.qis import isQisRunning, startLocalQis
        self.owns_qis = not isQisRunning()
        try:
            self.qis = startLocalQis(headless=True)
            if self.qis is None:
                raise RuntimeError("Could not start headless QIS")
            bounded_qis(self.qis)
            self.qis_endpoint = (self.qis.host, self.qis.port)
            self.version = self.qis.send_command("$version")
            self.qis.qis_scan_devices()
            self.device = quarchDevice(self.target, ConType="QIS", timeout="15", qis_instance=self.qis)
            self.identity = self.device.send_command("*IDN?")
            self.model = instrument_model(self.identity)
            if self.record_state().upper() != "OFF":
                raise RuntimeError("Instrument is already recording; existing recording was not stopped")
            self._load_fixture()
            self.set_resample(self.resample)
            self.attached_connection = usb_connection_key(self.target)
            self.connection_lost = False
        except Exception:
            self.close()
            raise

    def _load_fixture(self):
        from quarchpy.device import quarchPPM
        self.power = quarchPPM(self.device, skipDefaultSyntheticChannels=True)
        self.power.fixture_definition = self.device.send_command("fix:chan:xml?")
        self.synthetic_definitions = self.power.parse_synthetic_channels_from_instrument()
        self.available_channels = available_power_channels(self.synthetic_definitions)
        self.fixture_identity = self.device.send_command("fix:identity?").strip()
        self.analog_groups = {}
        self.minimum_interval_s = .000125
        if self.model == "QTL2312":
            self.analog_groups = fixture_analog_groups(self.power.fixture_definition)
            self.minimum_interval_s = max(1 / rate for rate in self.analog_groups.values())

    @property
    def reconnect_needed(self):
        changed = (self.attached_connection is not None
                   and usb_connection_key(self.target) != self.attached_connection)
        return self.connection_lost or changed

    def reconnect(self, resample):
        """Reopen this device after a disconnect; never resume a partial stream."""
        from quarchpy.connection_specific.connection_QIS import QisInterface
        from quarchpy.device import quarchDevice
        if self.reader_active:
            raise RuntimeError("Cannot reconnect while the previous reader is still active")
        self.connection_lost = True
        self.active_profile = self.configured_connection = None
        connection = usb_connection_key(self.target)
        if self.target.upper().startswith("USB:") and connection is None:
            raise ConnectionError(f"{self.target} is disconnected; reconnect USB and retry")
        host, port = (self.qis.host, self.qis.port) if self.qis else self.qis_endpoint
        self.qis_endpoint = (host, port)
        self._close_interface(self.reader)
        self.reader = None
        self._close_interface(self.qis)
        self.device = None
        try:
            try:
                self.qis = QisInterface(host=host, port=port, connectionMessage=False)
            except ConnectionRefusedError:
                if not self.owns_qis:
                    raise
                from quarchpy.qis import startLocalQis
                self.qis = startLocalQis(headless=True, host=host, port=port)
                if self.qis is None:
                    raise ConnectionError("Could not restart the recorder's QIS server")
            bounded_qis(self.qis)
            found = self.qis.qis_scan_devices()
            if (not found and self.owns_qis and self.target.upper().startswith("USB:")
                    and usb_connection_key(self.target) is not None):
                # The USB scanner can retain an empty list after a disconnect.
                # An owned QIS with no devices has no other device streams to stop.
                self._restart_qis()
            self.device = quarchDevice(self.target, ConType="QIS", timeout="5", qis_instance=self.qis)
            self.identity = self.device.send_command("*IDN?")
            self.model = instrument_model(self.identity)
            state = self.record_state().upper()
            if state == "ON" and self.started_by_us:
                # Only finish the orphaned recording owned by this backend.
                self._command("rec stop")
                state = self.record_state().upper()
            if state != "OFF":
                raise RuntimeError(f"Reconnect requires recording OFF; unowned recordings are not stopped: {state}")
            self.started_by_us = False
            self._refresh_stream_support()
            self._load_fixture()
            self.set_resample(resample)
            if connection != usb_connection_key(self.target):
                raise ConnectionError("USB changed again during reconnection; retry after the connection is stable")
            self.attached_connection = connection
            self.error = None
            self.connection_lost = False
        except Exception as exc:
            self.error = f"Reconnect failed: {exc}"
            raise

    def _refresh_stream_support(self):
        """Discard QIS's non-streaming device cache after an interrupted probe."""
        details = self.qis.send_command("$list details")
        lines = [line for line in details.splitlines() if ")" in line]
        target_lines = [line for line in lines if line.split(")", 1)[1].strip().split()[0].casefold()
                        == self.target.casefold()]
        if not any("stream:no" in line.casefold() for line in target_lines):
            return
        # QIS has no documented command to rebuild this cached device type.
        # Restart only our local server, with this sole device confirmed idle.
        if not self.owns_qis or len(lines) != 1 or self.record_state().upper() != "OFF":
            raise RuntimeError("QIS cached this PAM as non-streaming; restart its QIS server when all devices are idle")
        from quarchpy.device import quarchDevice
        self._restart_qis()
        self.device = quarchDevice(self.target, ConType="QIS", timeout="5", qis_instance=self.qis)
        self.identity = self.device.send_command("*IDN?")
        self.model = instrument_model(self.identity)
        if self.record_state().upper() != "OFF":
            raise RuntimeError("Instrument is not idle after restarting QIS")

    def _restart_qis(self):
        """Restart only after callers establish ownership and no other streams."""
        from quarchpy.qis import startLocalQis
        host, port = self.qis.host, self.qis.port
        with suppress(ConnectionError):  # QIS may close TCP before replying.
            self.qis.send_command("$shutdown")
        self._close_interface(self.qis)
        deadline = time.monotonic() + 5
        while True:
            try:
                with socket.create_connection((host, port), timeout=.2):
                    pass
            except OSError:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("QIS did not shut down; retry recovery after it exits")
            time.sleep(.1)
        self.qis = startLocalQis(headless=True, host=host, port=port)
        if self.qis is None:
            raise ConnectionError("Could not restart QIS after USB reconnection")
        bounded_qis(self.qis)
        self.version = self.qis.send_command("$version")
        self.qis.qis_scan_devices()

    def _command(self, command):
        for attempt in range(6):
            response = self.device.send_command(command)
            if response.strip().upper().startswith("OK"):
                return
            # Immediately after USB/QIS recovery, resample can return before
            # the firmware's averaging state transition has settled. Retry
            # only this idempotent setting, while recording is still OFF.
            if (attempt < 5 and command.startswith("rec:averaging:group ")
                    and "FAIL: 0x41" in response and self.record_state().upper() == "OFF"):
                time.sleep(.05)
                continue
            break
        raise RuntimeError(f"{command}: {response}")

    def record_state(self):
        try:
            state = self.device.send_command("rec?").strip() if self.device else "DISCONNECTED"
        except Exception as exc:
            state = f"DISCONNECTED: {exc}"
        if state.upper() not in ("OFF", "ON"):
            self.active_profile = self.configured_connection = None
            self.connection_lost = True
        return state

    @property
    def reader_active(self):
        return bool(self.thread and self.thread.is_alive())

    def set_resample(self, resample):
        if self.reader_active or self.record_state().upper() != "OFF":
            raise RuntimeError("Cannot change output interval while recording")
        if interval_seconds(resample) < self.minimum_interval_s:
            raise ValueError(f"{self.model or 'QTL2843'} output interval must be at least {self.minimum_interval_s * 1e6:g}us")
        response = self.power.stream_resample_mode(resample)
        if "fail" in response.lower() or "invalid" in response.lower():
            raise RuntimeError(response)
        # QIS resampling automatically chooses hardware averaging on DC PAMs.
        # Override it AFTER setting resample: calculate V*I at the analog base
        # rate, then average power in QIS, rather than multiplying averaged V/I.
        for group in self.analog_groups:
            self._command(f"rec:averaging:group {group} 1")
        self.resample = resample

    def configure_channels(self, channel, include_details):
        profile = (channel, include_details)
        functions = selected_synthetic_functions(self.synthetic_definitions, channel, include_details)
        rebuilt = profile != self.active_profile
        if rebuilt:
            self.active_profile = None
            self._command("stream created channels clear")
            for function in functions:
                self._command("stream create channel " + function)
            self.active_profile = profile
        return {
            "processing_profile": "selected_channel_details" if include_details else "energy_only",
            "synthetic_channels": len(functions),
            "read_batch_interval_s": self.read_batch_interval_s,
            "channels_rebuilt": rebuilt,
        }

    def begin(self, sink):
        from quarchpy.connection_specific.connection_QIS import QisInterface
        if self.reader_active or self.record_state().upper() != "OFF":
            raise RuntimeError("Instrument is not idle")
        connection = usb_connection_key(self.target)
        restore = connection is None or connection != self.configured_connection
        if restore:
            self.active_profile = None
            for command in ["rec averaging 1", "rec:trigger:mode manual", "rec:trigger:pre 0"]:
                self._command(command)
        profile = self.configure_channels(sink.channel, include_details=sink.csv_path is not None)
        if restore:
            self.set_resample(self.resample)
            self.configured_connection = connection
        profile["configuration_restored"] = restore
        self.reader = bounded_qis(QisInterface(host=self.qis.host, port=self.qis.port, connectionMessage=False))
        self.error = None
        self.stop_requested.clear()
        ready = threading.Event()
        self.started_by_us = True
        try:
            self._command("rec stream")
        except Exception:
            self.active_profile = self.configured_connection = None
            with suppress(Exception):
                self._command("rec stop")
            self._close_interface(self.reader)
            self.reader = None
            raise
        started = {"record_start_ack_at": utc_now(), "record_start_ack_monotonic": time.monotonic(), **profile}
        if self.model:
            started.update(device=self.target, instrument_identity=self.identity,
                           fixture_identity=self.fixture_identity, analog_base_rates_Hz=self.analog_groups,
                           hardware_averaging_samples=1)
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
                            self.stop_requested.wait(self.read_batch_interval_s)
            except Exception as exc:
                self.error = str(exc)
                if isinstance(exc, (ConnectionError, OSError)) or self.reconnect_needed:
                    self.connection_lost = True
                self.active_profile = self.configured_connection = None
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
