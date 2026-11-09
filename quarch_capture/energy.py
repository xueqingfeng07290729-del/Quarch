"""Energy integration for the AC/DC CSV exported by python -m quarch_capture.

This is a local Python helper, not a Quarch vendor API. Each QIS-resampled
pInstantaneous value is a mean over an output interval, so energy is the
sum of mean power times interval duration. RMS voltage times RMS current
is apparent power and must not be substituted for active energy.
"""

import argparse
import csv
import json
import math
from pathlib import Path

from .common import write_json_atomic
from .channels import channel_name
from .stream_data import EnergySink


def energy_from_csv(csv_path, *, channel, expected_duration_s=None):
    """Infer the period from two rows, then integrate in bounded memory."""
    channel = channel_name(channel)
    csv_path = Path(csv_path)
    with csv_path.open(encoding="utf-8-sig") as handle:
        header = handle.readline().rstrip("\r\n")
        sink = EnergySink(channel, 1)
        sink.set_header(header)
        first = []
        for line in handle:
            if line.strip():
                first.append(line)
                if len(first) == 2:
                    break
        if len(first) < 2:
            raise ValueError("At least two timestamped output intervals are required")
        rows = list(csv.reader(first))
        if any(len(row) != len(sink.headers) for row in rows):
            raise ValueError("Malformed CSV sample row")
        period = (float(rows[1][sink.time_index]) - float(rows[0][sink.time_index])) / sink.time_divisor
        if not math.isfinite(period) or period <= 0:
            raise ValueError("Non-uniform or non-increasing timestamps")
        sink.period = period
        sink.feed("".join(first))
        for block in iter(lambda: handle.read(65536), ""):
            sink.feed(block)
        sink.feed("\n")  # A valid CSV need not end with a newline.
        sink.close()
    result = sink.snapshot()
    result.update(
        source_csv=str(csv_path.resolve()), output_rate_Hz=1 / period,
        start_time_s=result.pop("sample_start_s"), end_time_exclusive_s=result.pop("sample_end_exclusive_s"),
        method="Sum of QIS interval-mean instantaneous power multiplied by each interval duration",
        power_source="QIS power channel; full-rate V/I is enforced by this project's capture backend",
        negative_power_policy="Preserved; reported energy is net energy",
    )
    if expected_duration_s is not None:
        result["requested_duration_s"] = expected_duration_s
        result["requested_duration_matches"] = math.isclose(result["duration_s"], expected_duration_s, rel_tol=1e-7, abs_tol=1e-9)
    return result


def main():
    parser = argparse.ArgumentParser(prog="python -m quarch_capture.energy", description=__doc__)
    parser.add_argument("csv", type=Path)
    parser.add_argument("--channel", type=channel_name, required=True,
                        help="Output whose power is integrated (required)")
    parser.add_argument("--seconds", type=float, help="Expected recording duration for a completeness check")
    args = parser.parse_args()
    report = energy_from_csv(args.csv, channel=args.channel, expected_duration_s=args.seconds)
    target = args.csv.with_suffix(f".{args.channel}.energy.json")
    write_json_atomic(target, report)
    print(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False))
    print("Saved:", target.resolve())


if __name__ == "__main__":
    main()
