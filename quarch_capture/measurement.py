"""Manage individual measurement sessions, their state and energy summaries."""

from contextlib import suppress
from datetime import datetime, timezone
import math
import os
from pathlib import Path
import re
import threading
import time
import uuid

from .common import CHANNELS, interval_seconds, utc_now, write_json_atomic
from .stream_data import EnergySink


class Recorder:
    def __init__(self, backend, channel, period, output_dir, save_csv=False):
        self.backend, self.channel, self.period = backend, channel, period
        self.output_dir, self.save_csv = Path(output_dir).resolve(), save_csv
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.session = self.last_result = None
        self.fault = None
        self.closing = False
        self.persistence_error = None
        self.cancelled_starts = {}
        self.output_override = None

    @staticmethod
    def _validate_request(request_id, start_deadline):
        if not isinstance(request_id, str) or not re.fullmatch(r"[0-9a-f]{32}", request_id):
            raise ValueError("request_id must be a 32-character lowercase hexadecimal token")
        if (isinstance(start_deadline, bool) or not isinstance(start_deadline, (int, float))
                or not math.isfinite(start_deadline) or start_deadline > time.time() + 120):
            raise ValueError("start_deadline must be a finite Unix timestamp no more than 120s ahead")

    def start(self, label="measurement", channel=None, save_csv=None, *, request_id=None, start_deadline=None,
              resample=None, on_sample=None):
        with self.lock:
            self.cancelled_starts = {key: deadline for key, deadline in self.cancelled_starts.items()
                                     if deadline > time.time()}
            if request_id is not None or start_deadline is not None:
                self._validate_request(request_id, start_deadline)
                if start_deadline <= time.time() or request_id in self.cancelled_starts:
                    raise RuntimeError("Start request expired or was cancelled")
            if self.closing or self.fault:
                raise RuntimeError(self.fault or "Recorder is shutting down")
            if self.persistence_error:
                raise RuntimeError("Previous summary is not saved; retry stop before starting another measurement")
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
            period = interval_seconds(resample) if resample is not None else self.period
            name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ") + f"_{channel}_{uuid.uuid4().hex[:8]}"
            csv_path = self.output_dir / (name + ".csv") if save_csv else None
            sink = EnergySink(channel, period, csv_path, on_sample=on_sample)
            session = {
                "session_id": name, "label": label, "selected_channel": channel,
                "csv_enabled": save_csv, "csv_path": str(csv_path) if csv_path else None,
                "summary_path": str(self.output_dir / (name + ".energy.json")),
                "start_requested_at": utc_now(), "sink": sink,
                "request_id": request_id,
            }
            try:
                if resample is not None:
                    self.output_override = (self.backend.resample, self.backend.read_batch_interval_s)
                    self.backend.set_resample(resample)
                    self.backend.read_batch_interval_s = min(period, self.output_override[1])
                session.update(self.backend.begin(sink))
            except Exception:
                with suppress(Exception):
                    sink.close()
                with suppress(Exception):
                    self._restore_output()
                try:
                    if self.backend.record_state().upper() != "OFF" or self.backend.reader_active or self.output_override:
                        self.fault = "Start failed and the instrument could not be confirmed idle; shut down and inspect the device"
                except Exception as exc:
                    self.fault = f"Start failed; recording state is unknown: {exc}"
                raise
            self.session = session
            if start_deadline is not None and start_deadline <= time.time():
                self.stop(session_id=session["session_id"])
                raise RuntimeError("Start request expired while starting; stop was requested")
            return {"state": "recording", **{k: v for k, v in session.items() if k not in ["sink", "record_start_ack_monotonic"]}}

    def _restore_output(self):
        if self.output_override is not None:
            resample, batch = self.output_override
            self.backend.set_resample(resample)
            self.backend.read_batch_interval_s = batch
            self.output_override = None

    def cancel_start(self, request_id, start_deadline):
        """Cancel by client token, including when cancellation arrives before start."""
        self._validate_request(request_id, start_deadline)
        with self.lock:
            self.cancelled_starts = {key: deadline for key, deadline in self.cancelled_starts.items()
                                     if deadline > time.time()}
            if start_deadline > time.time():
                self.cancelled_starts[request_id] = start_deadline
            if self.session and self.session.get("request_id") == request_id:
                response = self.stop(session_id=self.session["session_id"])
            elif self.last_result and self.last_result.get("request_id") == request_id:
                if not self.last_result.get("summary_saved"):
                    self._save_last_result()
                response = {"already_stopped": True, "result": self.last_result}
            else:
                response = {}
            # Cancellation acknowledges the actual state, including failed starts
            # that never reached self.session. It must not hide an ON/unknown device.
            status = self.status()
            return {**response, "state": status["state"], "device_record_state": status["device_record_state"],
                    "cancelled": status["state"] != "error", "error": status.get("fault") or status.get("persistence_error")}

    def _save_last_result(self):
        result = self.last_result
        result["summary_saved"] = True
        result.pop("summary_save_error", None)
        try:
            write_json_atomic(result["summary_path"], result)
        except Exception as exc:
            self.persistence_error = str(exc)
            result.update(summary_saved=False, summary_save_error=str(exc))
        else:
            self.persistence_error = None

    def stop(self, session_id=None):
        with self.lock:
            current = self.session or self.last_result
            if session_id is not None and (not current or current["session_id"] != session_id):
                raise RuntimeError("Session mismatch; no other measurement was stopped")
            if self.session is None:
                if self.last_result is not None:
                    if not self.last_result.get("summary_saved"):
                        self._save_last_result()
                    return {"state": "error" if self.fault or self.persistence_error or not self.last_result["complete"] else "idle",
                            "already_stopped": True, "result": self.last_result}
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
            try:
                self._restore_output()
            except Exception as exc:
                self.fault = f"Could not restore the configured output interval: {exc}"
                errors.append(self.fault)
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
            self._save_last_result()
            return {"state": "error" if errors or self.persistence_error else "idle", "result": result}

    def status(self):
        with self.lock:
            device_state = self.backend.record_state()
            reader_active = self.backend.reader_active
            state = "error" if self.fault or self.persistence_error else "recording" if self.session else "idle"
            if self.session is None and (device_state.upper() != "OFF" or reader_active):
                state = "error"
            error = getattr(self.backend, "error", None)
            if error and self.session:
                state = "error"
            result = {
                "state": state, "pid": os.getpid(), "configured_channel": self.channel,
                "start_channel_required": True,
                "default_csv_enabled": self.save_csv,
                "interval_s": self.session["sink"].period if self.session else self.period,
                "device_record_state": device_state,
                "reader_active": reader_active,
                "protocol_version": 2,
                "live_watch": True,
            }
            if self.session:
                result["active_session"] = {
                    "session_id": self.session["session_id"], "label": self.session["label"],
                    "request_id": self.session.get("request_id"),
                    "csv_enabled": self.session["csv_enabled"],
                    **self.session["sink"].snapshot(),
                }
            if self.last_result:
                result["last_result"] = self.last_result
            if error:
                result["stream_error"] = error
            if self.fault:
                result["fault"] = self.fault
            if self.persistence_error:
                result["persistence_error"] = self.persistence_error
            return result

    def request_shutdown(self, force=False):
        if not isinstance(force, bool):
            raise ValueError("force must be true or false")
        with self.lock:
            response = self.stop() if self.session or self.persistence_error else {"state": "error" if self.fault else "idle"}
            if self.persistence_error and not force:
                return {**response, "shutdown": False, "error": "Summary is not saved; retry stop before shutdown, or use --force"}
            self.closing = True
            response["shutdown"] = True
            return response

    def close(self):
        with self.lock:
            self.closing = True
            if self.session or self.persistence_error:
                with suppress(Exception):
                    self.stop()
            self.backend.close()
