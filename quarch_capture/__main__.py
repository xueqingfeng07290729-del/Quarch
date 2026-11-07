"""Command entry point: python -m quarch_capture."""

from .capture import cli


if __name__ == "__main__":
    raise SystemExit(cli())
