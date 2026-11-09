#!/usr/bin/env python3
"""Discover a QTL2312/QTL2843 PAM or capture CSV through headless QIS.

API reference: Quarch AN-031 and the adjacent QuarchPy checkout.
Run without --device to discover devices without starting a measurement.
Use --device USB::... --inspect to list the actual fixture power channels.
Captures require --channel. Channel selection does not switch device power.
Command-controlled mode: serve, start, stop, status and shutdown.
"""

import argparse
import csv
from contextlib import nullcontext, suppress
from datetime import datetime
import math
import json
import os
from pathlib import Path
import sys

from .common import device_lock, interval_seconds, termination_guard, write_json_atomic
from .channels import (available_power_channels, channel_name, fixture_analog_groups,
                       instrument_model, power_column, selected_columns)


def export_channel_csv(source, target, channel):
    """Export time and the selected output; retain the complete-channel source."""
    channel = channel_name(channel)
    with Path(source).open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        headers = reader.fieldnames or []
        time_columns = [c for c in headers if c.lower().startswith("time ")]
        if len(time_columns) != 1:
            raise ValueError("Expected one timestamp column")
        power_column(headers, channel)
        columns = time_columns + [headers[i] for i in selected_columns(headers, channel)]
        with Path(target).open("x", newline="", encoding="utf-8") as result:
            writer = csv.DictWriter(result, fieldnames=columns)
            writer.writeheader()
            for row in reader:
                if None in row or any(row.get(c) is None for c in columns):
                    raise ValueError("Malformed capture row")
                writer.writerow({c: row[c] for c in columns})


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "watch":
        from .watch import main as watch_main
        return watch_main(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] in ("serve", "start", "stop", "status", "recover", "shutdown"):
        from .commands import main as commands_main
        return commands_main(sys.argv[1:])
    parser = argparse.ArgumentParser(prog="python -m quarch_capture", description=__doc__)
    parser.add_argument("--device", help="Exact USB::... or TCP::... target printed by discovery")
    parser.add_argument("--channel", type=channel_name,
                        help="Power channel to measure; use --inspect to list available channels")
    parser.add_argument("--inspect", action="store_true", help="Read instrument/fixture identity and channels without recording")
    parser.add_argument("--ip", help="Known instrument IPv4 address for targeted discovery")
    parser.add_argument("--seconds", type=float, default=10, help="Capture duration (default: 10 seconds)")
    parser.add_argument("--resample", default="10ms", help="QIS software resampling interval (default: 10ms / 100 Hz)")
    parser.add_argument("--output", type=Path, help="Selected-output CSV; complete channels go to <stem>.all-channels.csv")
    args = parser.parse_args()
    if not math.isfinite(args.seconds) or args.seconds <= 0:
        parser.error("--seconds must be finite and positive")
    if args.inspect and not args.device:
        parser.error("--inspect requires --device")
    if args.device and args.channel is None and not args.inspect:
        parser.error("--channel is required with --device; use --inspect to list channels")
    interval_seconds(args.resample)

    with device_lock(args.device) if args.device else nullcontext():
        return _capture(args, parser)


def _capture(args, parser):
    output = (args.output or Path(datetime.now().strftime(f"quarch_{args.channel}_%Y%m%d_%H%M%S.csv"))).resolve()
    all_channels = output.with_name(output.stem + ".all-channels.csv")
    energy_file = output.with_suffix(".energy.json")
    if args.device and not args.inspect:
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
            print("Run again with --device '<one exact target above>' --inspect to list fixture channels.")
            return 0

        device = quarchDevice(args.device, ConType="QIS", timeout="15", qis_instance=qis)
        identity = device.send_command("*IDN?")
        print("Instrument:\n", identity)
        model = instrument_model(identity)
        if args.inspect:
            power = quarchPPM(device, skipDefaultSyntheticChannels=True)
            power.fixture_definition = device.send_command("fix:chan:xml?")
            print(json.dumps({"device": args.device, "model": model,
                              "fixture_identity": device.send_command("fix:identity?").strip(),
                              "available_channels": available_power_channels(power.parse_synthetic_channels_from_instrument()),
                              "analog_base_rates_Hz": fixture_analog_groups(power.fixture_definition) if model == "QTL2312" else {},
                              "device_record_state": device.send_command("rec?").strip()}, ensure_ascii=False, indent=2))
            return 0
        if device.send_command("rec?").strip().upper() != "OFF":
            raise RuntimeError("Instrument is already recording; its configuration was not changed")

        def configure(command):
            reply = device.send_command(command)
            if not reply.strip().upper().startswith("OK"):
                raise RuntimeError(f"{command}: {reply}")

        power = quarchPPM(device, skipDefaultSyntheticChannels=True)
        power.fixture_definition = device.send_command("fix:chan:xml?")
        definitions = power.parse_synthetic_channels_from_instrument()
        available = available_power_channels(definitions)
        if args.channel not in available:
            raise ValueError(f"Channel {args.channel} is unavailable; choose from {', '.join(available)}")
        groups = fixture_analog_groups(power.fixture_definition) if model == "QTL2312" else {}
        minimum = max(1 / rate for rate in groups.values()) if groups else .000125
        if interval_seconds(args.resample) < minimum:
            raise ValueError(f"{model} output interval must be at least {minimum * 1e6:g}us")
        fixture_identity = device.send_command("fix:identity?").strip()
        configure("stream created channels clear")
        power.send_synthetic_channels(definitions)
        configure("rec:trigger:mode manual")
        configure("rec:trigger:pre 0")
        reply = power.stream_resample_mode(args.resample)
        if "fail" in reply.lower() or "invalid" in reply.lower():
            raise RuntimeError(f"Software resampling failed: {reply}")
        # QIS resample changes hardware averaging; restore full-rate analogs
        # afterwards so instantaneous power is calculated before averaging.
        if groups:
            for group in groups:
                configure(f"rec:averaging:group {group} 1")
        else:
            configure("rec averaging 1")
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
        energy.update(device=args.device, instrument_identity=identity, fixture_identity=fixture_identity,
                      analog_base_rates_Hz=groups, hardware_averaging_samples=1)
        write_json_atomic(energy_file, energy)
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


@termination_guard()
def cli():
    try:
        return main()
    except KeyboardInterrupt as exc:
        print("Capture interrupted.", file=sys.stderr)
        for note in getattr(exc, "__notes__", []):
            print(note, file=sys.stderr)
        return 130
    except BrokenPipeError:
        with open(os.devnull, "w") as null:
            os.dup2(null.fileno(), sys.stdout.fileno())
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        for note in getattr(exc, "__notes__", []):
            print(note, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(cli())
