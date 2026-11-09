"""Live power samples over the existing recorder connection."""

import argparse
import json
from pathlib import Path
import queue
import re
import select
import socket
import sys
import time
import uuid

from .client import DEFAULT_SOCKET, _cancel, _stop, require_idle
from .common import CHANNELS, interval_seconds, termination_guard
from .channels import channel_name


def stream_watch(handler, request):
    """Serve one scoped stream; disconnects stop only this watch session."""
    recorder = handler.server.recorder
    samples = queue.Queue(maxsize=max(2048, int(2 / interval_seconds(request.get("resample", "10ms")))))

    def emit(timestamp, power):
        try:
            samples.put_nowait((timestamp, power))
        except queue.Full:
            raise RuntimeError("Live output cannot keep up; measurement was stopped") from None

    def send(frame):
        handler.wfile.write((json.dumps(frame, allow_nan=False) + "\n").encode())
        handler.wfile.flush()

    started = recorder.start("watch", request.get("channel"), False,
                             request_id=request.get("request_id"), start_deadline=request.get("start_deadline"),
                             resample=request.get("resample", "10ms"), on_sample=emit)
    session_id = started["session_id"]
    disconnected, error = False, None
    try:
        send({"event": "ready", "ok": True, **started})
        while True:
            if select.select([handler.connection], [], [], 0)[0]:
                if not handler.connection.recv(1, socket.MSG_PEEK):
                    disconnected = True
                    break
                raise RuntimeError("Use a separate control connection for stop commands")
            with recorder.lock:
                if not recorder.session or recorder.session["session_id"] != session_id:
                    break
                backend_error = getattr(recorder.backend, "error", None)
            if backend_error:
                raise RuntimeError(backend_error)
            try:
                timestamp, power = samples.get(timeout=.1)
            except queue.Empty:
                continue
            send({"event": "sample", "time_s": timestamp, "power_W": power})
    except (BrokenPipeError, ConnectionResetError, TimeoutError):
        disconnected = True
    except Exception as exc:
        error = str(exc)
    finally:
        with recorder.lock:
            if recorder.session and recorder.session["session_id"] == session_id:
                stopped = recorder.stop(session_id=session_id)
            elif recorder.last_result and recorder.last_result["session_id"] == session_id:
                stopped = recorder.stop(session_id=session_id)
            else:
                stopped = {"state": "error", "error": "Watch session was replaced"}
        if not disconnected:
            try:
                send({"event": "stopped", "ok": error is None and stopped["state"] != "error",
                      **stopped, **({"error": error} if error else {})})
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass


def parse_args(argv=None):
    parser = argparse.ArgumentParser(prog="Quarch watch", description="Print every selected-channel power sample until Ctrl+C.")
    channels = parser.add_mutually_exclusive_group(required=True)
    for channel in CHANNELS:
        channels.add_argument("--" + channel, dest="channel", action="store_const", const=channel)
    channels.add_argument("--channel", type=channel_name, help="Power channel, e.g. 3.3V, VIO_1V8 or Tot")
    parser.add_argument("--interval", default="10ms", help="Output interval, e.g. 10ms, 100ms or 125us (default: 10ms)")
    parser.add_argument("--socket", type=Path, default=DEFAULT_SOCKET)
    expanded = []
    for value in (sys.argv[1:] if argv is None else argv):
        shorthand = re.fullmatch(r"--(\d+(?:us|ms))", value, re.IGNORECASE)
        expanded.extend(["--interval", shorthand.group(1)] if shorthand else [value])
    args = parser.parse_args(expanded)
    args.socket = args.socket.expanduser()
    try:
        period = interval_seconds(args.interval)
        if args.channel in CHANNELS and period < .000125:
            parser.error("QTL2843 output intervals must be at least 125us")
    except argparse.ArgumentTypeError as exc:
        parser.error(str(exc))
    return args


def read_frame(stream):
    line = stream.readline(1_048_577)
    if len(line) > 1_048_576 or not line.endswith(b"\n"):
        raise RuntimeError("Watch connection closed or returned an invalid frame")
    return json.loads(line)


@termination_guard()
def main(argv=None):
    args = parse_args(argv)
    try:
        status = require_idle(args.socket)
    except (FileNotFoundError, ConnectionRefusedError) as exc:
        raise RuntimeError("Recorder is not running; start python -m quarch_capture serve first") from exc
    if not status.get("live_watch"):
        raise RuntimeError("Restart quarch_capture serve once to enable the watch command")
    request = {"request_id": uuid.uuid4().hex, "start_deadline": time.time() + 45}
    started = primary = None
    finished = False
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(max(45, 2 * interval_seconds(args.interval)))
        connection.connect(str(args.socket))
        with connection.makefile("rb") as stream:
            try:
                connection.sendall((json.dumps({"command": "watch", "channel": args.channel,
                                               "resample": args.interval, **request}) + "\n").encode())
                frame = read_frame(stream)
                if (not frame.get("ok") or frame.get("event") != "ready" or not frame.get("session_id")
                        or frame.get("request_id") != request["request_id"]):
                    raise RuntimeError(f"Could not start watch: {frame}")
                started = frame
                print(f"Watching {args.channel}, interval {args.interval}; Ctrl+C stops recording.", file=sys.stderr, flush=True)
                print(f"time_s,{args.channel}_power_W", flush=True)
                while True:
                    frame = read_frame(stream)
                    if frame.get("event") == "sample":
                        print(f"{frame['time_s']:.6f},{frame['power_W']:.6f}", flush=True)
                    elif frame.get("event") == "stopped":
                        finished = True
                        if not frame.get("ok"):
                            raise RuntimeError(f"Watch stopped with an error: {frame}")
                        break
                    else:
                        raise RuntimeError(f"Invalid watch response: {frame}")
            except BaseException as exc:
                primary = exc
                raise
            finally:
                if not finished:
                    try:
                        if started is None:
                            _cancel(args.socket, request, None)
                        else:
                            _stop(args.socket, started, request, None, allow_already_stopped=True)
                    except BaseException as cleanup:
                        if primary is None:
                            raise
                        primary.add_note(f"Watch cleanup also failed: {cleanup}")
    return 0
