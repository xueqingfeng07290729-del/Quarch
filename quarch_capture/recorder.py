"""Compatibility imports and entry point for the split capture service.

New code should import stream_data, qis_backend, measurement or commands
according to its responsibility. Existing recorder imports remain supported.
"""

from .common import CHANNELS, utc_now
from .commands import (
    DEFAULT_SOCKET, CommandHandler, LocalServer, cli, interval_seconds, main,
    send_command, serve,
)
from .measurement import Recorder
from .qis_backend import (
    READ_BATCH_INTERVAL_S, QisBackend, read_stream_block, selected_synthetic_functions,
)
from .stream_data import EnergySink


__all__ = [
    "CHANNELS", "DEFAULT_SOCKET", "READ_BATCH_INTERVAL_S", "utc_now",
    "interval_seconds", "selected_synthetic_functions", "read_stream_block",
    "EnergySink", "QisBackend", "Recorder", "LocalServer", "CommandHandler",
    "send_command", "serve", "main", "cli",
]


if __name__ == "__main__":
    raise SystemExit(cli())
