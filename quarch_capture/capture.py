#!/usr/bin/env python3
"""Discover an AC PAM or capture CSV through a headless local QIS server.

API reference: Quarch AN-031 and the adjacent QuarchPy checkout.
Run without --device to discover devices without starting a measurement.
Captures require --channel L1, L2 or L3. This selects exported data and
energy statistics; it does not switch the instrument's AC power outputs.
Command-controlled mode: serve, start, stop, status and shutdown.
"""

import argparse
import csv
from contextlib import suppress
from datetime import datetime
import json
import math
from pathlib import Path
import sys


def export_channel_csv(source, target, channel):
    """Export time and the selected output; retain the complete-channel source."""
    if channel not in ("L1", "L2", "L3"):
        raise ValueError("channel must be L1, L2 or L3")
    with Path(source).open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        headers = reader.fieldnames or []
        time_columns = [c for c in headers if c.lower().startswith("time ")]
        if len(time_columns) != 1:
            raise ValueError("Expected one timestamp column")
        columns = time_columns + [c for c in headers if c.startswith((channel + " ", channel + "_", channel + "V_"))]
        if f"{channel}_PInst VA mVA" not in columns:
            raise ValueError(f"The capture is missing output {channel}")
        with Path(target).open("x", newline="", encoding="utf-8") as result:
            writer = csv.DictWriter(result, fieldnames=columns)
            writer.writeheader()
            for row in reader:
                if None in row or any(row.get(c) is None for c in columns):
                    raise ValueError("Malformed capture row")
                writer.writerow({c: row[c] for c in columns})


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ("serve", "start", "stop", "status", "shutdown"):
        from .commands import main as commands_main
        return commands_main(sys.argv[1:])
    parser = argparse.ArgumentParser(prog="python -m quarch_capture", description=__doc__)
    parser.add_argument("--device", help="Exact USB::... or TCP::... target printed by discovery")
    parser.add_argument("--channel", type=str.upper, choices=("L1", "L2", "L3"),
                        help="Output to export and measure; required with --device; does not switch AC outputs")
    parser.add_argument("--ip", help="Known instrument IPv4 address for targeted discovery")
    parser.add_argument("--seconds", type=float, default=10, help="Capture duration (default: 10 seconds)")
    parser.add_argument("--resample", default="10ms", help="QIS software resampling interval (default: 10ms / 100 Hz)")
    parser.add_argument("--output", type=Path, help="Selected-output CSV; complete channels go to <stem>.all-channels.csv")
    args = parser.parse_args()
    if not math.isfinite(args.seconds) or args.seconds <= 0:
        parser.error("--seconds must be finite and positive")
    if args.device and args.channel is None:
        parser.error("--channel is required with --device; choose L1, L2 or L3")

    output = (args.output or Path(datetime.now().strftime(f"qtl2843_{args.channel}_%Y%m%d_%H%M%S.csv"))).resolve()
    all_channels = output.with_name(output.stem + ".all-channels.csv")
    energy_file = output.with_suffix(".energy.json")
    if args.device:
        for path in (output, all_channels, energy_file):
            if path.exists():
                parser.error(f"Output already exists: {path}")
        if not output.parent.is_dir():
            parser.error(f"Output directory does not exist: {output.parent}")

    try:
        from quarchpy.device import quarchDevice, quarchPPM
        from quarchpy.qis import closeQis, isQisRunning, startLocalQis
    except ImportError as exc:
        print(f"Missing Python dependency: {exc}. Install ./QuarchPy into a virtual environment.", file=sys.stderr)
        return 1

    owns_qis = not isQisRunning()
    qis = None
    device = None
    power = None
    streaming = False
    try:
        qis = startLocalQis(headless=True)
        if qis is None:
            raise RuntimeError("Could not start QIS. Check ~/.quarchpy/quarchpy.log.")
        print("QIS:", qis.send_command("$version"))
        targets = qis.qis_scan_devices(preferred_connection_only=False, ip_address=args.ip)
        print("Discovered targets:")
        for target in targets:
            print(" ", target)
        if not args.device:
            if not targets:
                print("No devices found. Check DATA IN / USB, network addressing and USB permissions.")
                return 2
            print("Run again with --device '<one exact target above>' --channel L1|L2|L3 to capture.")
            return 0

        device = quarchDevice(args.device, ConType="QIS", timeout="15", qis_instance=qis)
        identity = device.send_command("*IDN?")
        print("Instrument:\n", identity)
        if "2843" not in identity:
            raise RuntimeError("The instrument did not identify itself as QTL2843; capture was not started.")

        def configure(command):
            reply = device.send_command(command)
            if not reply.strip().upper().startswith("OK"):
                raise RuntimeError(f"{command}: {reply}")

        # AC RMS/power calculations need full-rate hardware samples.
        configure("rec averaging 1")
        configure("rec:trigger:mode manual")
        power = quarchPPM(device)  # Creates the default AC synthetic channels.
        reply = power.stream_resample_mode(args.resample)
        if "fail" in reply.lower() or "invalid" in reply.lower():
            raise RuntimeError(f"Software resampling failed: {reply}")
        # The caller owns this exclusive file handle, so existing data cannot be overwritten.
        with all_channels.open("x", encoding="utf-8", newline="") as handle:
            print(f"Selected output: {args.channel}; capturing {args.seconds:g} seconds")
            print("Complete-channel CSV:", all_channels)
            streaming = True
            try:
                power.start_stream(
                    file_name=str(all_channels), file_max_mb=256,
                    stream_duration=args.seconds, output_file_handle=handle,
                )
                power.wait_stop()
                streaming = False
            finally:
                if streaming:
                    with suppress(Exception):
                        power.stop_stream()
                    streaming = False
        status = power.stream_running_status()
        print("Stream status:", status)
        state = qis.deviceDict.get(power.ConString)
        if state and state[0]:
            raise RuntimeError(f"Capture reported an error: {state}")
        export_channel_csv(all_channels, output, args.channel)
        with output.open(encoding="utf-8") as handle:
            header = handle.readline().strip()
            first_sample = handle.readline().strip()
        if not header or not first_sample:
            raise RuntimeError("No measurement samples were captured.")
        print("CSV header:", header)
        print("Saved:", output)
        from .energy import energy_from_csv
        energy = energy_from_csv(output, channel=args.channel, expected_duration_s=args.seconds)
        energy["all_channels_csv"] = str(all_channels)
        energy_file.write_text(json.dumps(energy, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
        print(f"Output interval: {energy['interval_s'] * 1000:g} ms; covered duration: {energy['duration_s']:g} s")
        if not energy["requested_duration_matches"]:
            print("Note: complete output bins cover a different duration from the request; energy refers to those bins.")
        selected = energy[args.channel]
        print(f"{args.channel} mean power: {selected['mean_power_W']:.6f} W")
        print(f"{args.channel} energy: {selected['energy_J']:.6f} J = {selected['energy_Wh']:.9f} Wh")
        print("Energy summary:", energy_file)
        return 0
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
            with suppress(Exception):
                closeQis()


def cli():
    try:
        return main()
    except KeyboardInterrupt:
        print("Capture interrupted.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(cli())
