"""A scoped measurement window shared by automated database workloads."""

from contextlib import contextmanager
import time
import uuid

from .commands import DEFAULT_SOCKET, send_command
from .common import termination_guard, write_json_atomic


def command(socket_path, request):
    response = send_command(socket_path, request)
    if not response.get("ok"):
        raise RuntimeError(f"Recorder {request['command']} failed: {response}")
    return response


def require_idle(socket_path):
    status = command(socket_path, {"command": "status"})
    if status.get("protocol_version", 0) < 2:
        raise RuntimeError("Recorder protocol 2 is required; restart quarch_capture serve to load the updated service")
    if status.get("state") != "idle" or status.get("reader_active") or status.get("device_record_state") != "OFF":
        raise RuntimeError("Recorder must already be idle with recording OFF; active sessions are not interrupted")
    return status


def preserve_response(path, response):
    capture = response.get("result")
    if capture is None:
        capture = {"complete": False, "errors": [response.get("error") or "No capture result returned"],
                   "recorder_response": response}
    write_json_atomic(path, capture)
    return capture


def _exchange(socket_path, request):
    failure = None
    for _ in range(2):
        try:
            return send_command(socket_path, request), failure
        except BaseException as exc:
            if failure is None:
                failure = exc
    raise failure


def _cancel(socket_path, request, path):
    response, failure = _exchange(socket_path, {"command": "cancel_start", **request})
    preserve_response(path, response)
    if not response.get("ok") or not response.get("cancelled"):
        raise RuntimeError(f"Recording state could not be confirmed: {response}")
    if failure is not None and not isinstance(failure, Exception):
        raise failure


def _stop(socket_path, started, request, path):
    stop = {"command": "stop", "session_id": started["session_id"]}
    try:
        response, failure = _exchange(socket_path, stop)
    except BaseException as failure:
        try:
            _cancel(socket_path, request, path)
        except BaseException as cleanup:
            failure.add_note(f"Recording state could not be confirmed: {cleanup}")
        else:
            failure.add_note("Own recording was cancelled; the measurement was discarded")
        raise
    capture = preserve_response(path, response)
    if failure is not None:
        if not response.get("ok"):
            failure.add_note(f"Recorder recovery returned an error: {response}")
        failure.add_note("Stop reply required recovery; capture evidence was saved, but the measurement was discarded")
        raise failure
    if not response.get("ok"):
        raise RuntimeError(f"Recorder stop failed: {response}")
    if response.get("already_stopped"):
        raise RuntimeError("Measurement stopped before the SQL batch finished; partial energy was rejected")
    if capture.get("session_id") != started["session_id"] or capture.get("request_id") != request["request_id"]:
        raise RuntimeError("Recorder session changed; energy cannot be attributed to this batch")
    return capture


@contextmanager
def capture_window(*, socket_path=DEFAULT_SOCKET, channel, label, save_csv=False, capture_path):
    """Always clean up our own request; preserve workload errors and partial data."""
    with termination_guard():
        require_idle(socket_path)
        request = {"request_id": uuid.uuid4().hex, "start_deadline": time.time() + 45}
        started = primary = None
        capture = {}
        try:
            response = command(socket_path, {"command": "start", "channel": channel,
                                            "label": label, "save_csv": save_csv, **request})
            if not response.get("session_id") or response.get("request_id") != request["request_id"]:
                raise RuntimeError("Invalid start response; request ownership could not be confirmed")
            started = response
            yield capture
        except BaseException as exc:
            primary = exc
            raise
        finally:
            try:
                if started is None:
                    _cancel(socket_path, request, capture_path)
                else:
                    capture.update(_stop(socket_path, started, request, capture_path))
            except BaseException as cleanup:
                if primary is None:
                    raise
                phase = "Start cancellation could not be confirmed" if started is None else "Capture cleanup also failed"
                primary.add_note(f"{phase}: {cleanup}")
