#!/usr/bin/env python3
"""Query instrument identity via native USB or headless QIS; do not record data."""

import argparse
from contextlib import suppress
import os
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=["usb", "qis"], default="usb")
    parser.add_argument("--serial", default="QTL2843-03-002")
    args = parser.parse_args()
    from quarchpy.device import quarchDevice

    device, qis, owns_qis = None, None, False
    print("Running UID:", os.geteuid(), "Backend:", args.backend, flush=True)
    try:
        if args.backend == "qis":
            from quarchpy.qis import isQisRunning, startLocalQis
            owns_qis = not isQisRunning()
            qis = startLocalQis(headless=True, timeout=30)
            if qis is None:
                raise RuntimeError("QIS failed to start")
            print("QIS:", qis.send_command("$version"), flush=True)
            print("Discovered:", qis.qis_scan_devices(), flush=True)
            device = quarchDevice("USB::" + args.serial, ConType="QIS", timeout="15", qis_instance=qis)
        else:
            device = quarchDevice("USB:" + args.serial)
        print(device.send_command("*IDN?"), flush=True)
        print("Recording state:", device.send_command("rec?"), flush=True)
        print("Connection check passed; no recording or output-switching command was issued.", flush=True)
    finally:
        if device is not None:
            with suppress(Exception):
                device.close_connection()
        if qis is not None:
            with suppress(Exception):
                qis.streamSock.close()
            with suppress(Exception):
                qis.disconnect()
        if owns_qis:
            from quarchpy.qis import closeQis
            with suppress(Exception):
                closeQis()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
