"""Local start/stop command interface. Recording stays OFF between measurements."""

import argparse
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import socket
import socketserver
import stat
import sys
import threading

from .common import CHANNELS
from .measurement import Recorder
from .qis_backend import QisBackend


DEFAULT_SOCKET = Path.home() / ".cache/quarch/recorder.sock"


def interval_seconds(value):
    match = re.fullmatch(r"([1-9]\d*)(us|ms)", value.lower())
    if not match:
        raise argparse.ArgumentTypeError("Use a positive integer interval such as 125us or 10ms")
    amount, unit = match.groups()
    result = int(amount) / (1_000_000 if unit == "us" else 1000)
    if result < .000125:
        raise argparse.ArgumentTypeError("QTL2843 output intervals must be at least 125us")
    return result


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
                payload = recorder.start(request.get("label", "measurement"), request.get("channel"), request.get("save_csv"),
                                         request_id=request.get("request_id"), start_deadline=request.get("start_deadline"))
            elif command == "stop":
                payload = recorder.stop(session_id=request.get("session_id"))
            elif command == "cancel_start":
                payload = recorder.cancel_start(request.get("request_id"), request.get("start_deadline"))
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
        if command == "stop":
            sub.add_argument("--session-id", help="Stop only this session; reject a different active measurement")
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
    elif args.command == "stop" and args.session_id is not None:
        request["session_id"] = args.session_id
    response = send_command(args.socket.expanduser(), request, args.timeout)
    print(json.dumps(response, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if response.get("ok") else 1


def cli():
    try:
        return main()
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(cli())
