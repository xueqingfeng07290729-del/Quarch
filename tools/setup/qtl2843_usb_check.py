#!/usr/bin/env python3
"""Read device identity/status using QuarchPy's low-level USB transport."""

import argparse
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", default="QTL2843-03-002")
    args = parser.parse_args()
    transport_path = Path(__file__).resolve().parents[2] / "QuarchPy/quarchpy/connection_specific"
    sys.path.insert(0, str(transport_path))
    from connection_USB import USB

    device = USB(args.serial)
    if device is None:
        raise RuntimeError("USB device not found")
    try:
        if not device.IsPortOpen():
            raise RuntimeError(device.GetLastError())
        for command in ("hello?", "*IDN?", "*SERIAL?", "rec?",
                        "config:ethernet:ip?", "config:ethernet:dhcp?"):
            reply = device.RunCommand(command)
            print(f"{command}\n{reply.strip()}\n", flush=True)
            if not reply.strip():
                raise RuntimeError(f"No reply to {command}: {device.GetLastError()}")
    finally:
        device.ClosePort()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
