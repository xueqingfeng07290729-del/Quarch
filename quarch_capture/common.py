"""Shared output names and UTC timestamps for command-controlled capture."""

from datetime import datetime, timezone
from contextlib import contextmanager, suppress
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import tempfile
import threading

from .channels import AC_CHANNELS


CHANNELS = AC_CHANNELS  # Legacy short options; PAM channels are discovered dynamically.
RUNTIME_DIR = Path.home() / ".cache/quarch"


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def interval_seconds(value):
    match = re.fullmatch(r"([1-9]\d*)(us|ms)", value.lower())
    if not match:
        raise argparse.ArgumentTypeError("Use a positive integer interval such as 125us or 10ms")
    amount, unit = match.groups()
    result = int(amount) / (1_000_000 if unit == "us" else 1000)
    if result < .000004:
        raise argparse.ArgumentTypeError("Output intervals must be at least 4us; the device may impose a higher minimum")
    return result


@contextmanager
def atomic_output(path, *, newline=None):
    """Replace a report only after a complete write; keep the old file on failure."""
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".quarch-", suffix=".tmp", newline=newline, delete=False) as handle:
            temporary = Path(handle.name)
            yield handle
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)


def write_json_atomic(path, content):
    with atomic_output(path) as handle:
        json.dump(content, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")


@contextmanager
def device_lock(target):
    """Share ownership between fixed-duration and command-controlled captures."""
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = hashlib.sha256(target.lower().encode()).hexdigest()[:16]
    with (RUNTIME_DIR / ("device-" + key + ".lock")).open("a+") as handle:
        os.chmod(handle.name, 0o600)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("A capture already owns this device") from None
        yield


@contextmanager
def termination_guard():
    """Turn SIGTERM into cooperative cancellation so Python cleanup can run."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    def interrupted(signum, frame):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise KeyboardInterrupt("SIGTERM received")
    previous = signal.signal(signal.SIGTERM, interrupted)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def finalize_run(metadata, primary, *steps):
    """Attempt every finalizer, retaining the original failure and cleanup notes."""
    error = primary
    for step in steps:
        try:
            step()
        except BaseException as exc:
            if error is None:
                error = exc
            elif exc is not error:
                error.add_note(f"Finalization also failed: {type(exc).__name__}: {exc}")
            metadata.update(status="failed", error=f"{type(error).__name__}: {error}",
                            error_notes=getattr(error, "__notes__", []))
    if primary is None and error is not None:
        raise error
