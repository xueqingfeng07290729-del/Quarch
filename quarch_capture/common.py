"""Shared output names and UTC timestamps for command-controlled capture."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile


CHANNELS = ("L1", "L2", "L3")


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def write_json_atomic(path, content):
    """Replace a summary only after the complete temporary file has been flushed."""
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(content, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
