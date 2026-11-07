"""Energy integration for the AC CSV exported by python -m quarch_capture.

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
import statistics


CHANNELS = ("L1", "L2", "L3")


def energy_from_csv(csv_path, *, channel, expected_duration_s=None):
    """Return the explicitly selected output's net energy in J/Wh.

    Uses timestamp spacing, validates missing/non-finite power samples and
    integrates every output bin, including the final bin. Negative power
    is preserved. The reported duration covers the CSV's complete bins;
    requested_duration_matches reports whether this equals a supplied duration.
    """
    channel = channel.upper()
    if channel not in CHANNELS:
        raise ValueError("channel must be L1, L2 or L3")
    csv_path = Path(csv_path)
    divisors = {"ns": 1_000_000_000, "us": 1_000_000, "µs": 1_000_000,
                "μs": 1_000_000, "ms": 1000, "s": 1}
    time_values = []
    powers = {}
    with csv_path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        headers = reader.fieldnames or []
        time_headers = [c for c in headers if c.lower().startswith("time ")]
        if len(time_headers) != 1:
            raise ValueError("Expected one timestamp column named 'Time <unit>'")
        time_column = time_headers[0]
        unit = time_column.split()[-1].lower()
        if unit not in divisors:
            raise ValueError(f"Unsupported timestamp unit: {unit}")
        column = f"{channel}_PInst VA mVA"
        if column not in headers:
            raise ValueError(f"Missing {column}: the CSV does not contain selected output {channel}")
        powers[channel] = (column, [])
        for line_number, row in enumerate(reader, 2):
            try:
                # Keep native timestamp units until after differencing to avoid
                # cancellation error when deriving a small interval from large times.
                timestamp = float(row[time_column])
                values = {name: float(row[column]) / 1000 for name, (column, _) in powers.items()}
            except (ValueError, TypeError) as exc:
                raise ValueError(f"Missing or invalid numeric value on CSV line {line_number}") from exc
            if not math.isfinite(timestamp) or not all(math.isfinite(v) for v in values.values()):
                raise ValueError(f"Non-finite sample on CSV line {line_number}")
            time_values.append(timestamp)
            for name, value in values.items():
                powers[name][1].append(value)
    if len(time_values) < 2:
        raise ValueError("At least two timestamped output intervals are required")
    intervals = [b - a for a, b in zip(time_values, time_values[1:])]
    native_period = statistics.median(intervals)
    if native_period <= 0 or not all(math.isclose(d, native_period, rel_tol=1e-7, abs_tol=1e-12) for d in intervals):
        raise ValueError("Non-uniform or non-increasing timestamps: refusing to silently integrate missing bins")
    period = native_period / divisors[unit]
    duration = len(time_values) * period
    result = {
        "source_csv": str(csv_path.resolve()),
        "selected_channel": channel,
        "method": "Sum of QIS interval-mean instantaneous power multiplied by each interval duration",
        "power_source": "PInst computed from full-rate voltage/current before QIS output resampling",
        "samples": len(time_values), "interval_s": period, "output_rate_Hz": 1 / period,
        "duration_s": duration, "start_time_s": time_values[0] / divisors[unit],
        "end_time_exclusive_s": (time_values[-1] + native_period) / divisors[unit],
        "negative_power_policy": "Preserved; reported energy is net energy",
    }
    if expected_duration_s is not None:
        result["requested_duration_s"] = expected_duration_s
        result["requested_duration_matches"] = math.isclose(duration, expected_duration_s, rel_tol=1e-7, abs_tol=1e-9)
    for name, (_, values) in powers.items():
        energy = math.fsum(values) * period
        result[name] = {
            "mean_power_W": energy / duration, "energy_J": energy, "energy_Wh": energy / 3600,
            "min_interval_mean_W": min(values), "max_interval_mean_W": max(values),
        }
    return result


def main():
    parser = argparse.ArgumentParser(prog="python -m quarch_capture.energy", description=__doc__)
    parser.add_argument("csv", type=Path)
    parser.add_argument("--channel", type=str.upper, choices=CHANNELS, required=True,
                        help="Output whose power is integrated (required)")
    parser.add_argument("--seconds", type=float, help="Expected recording duration for a completeness check")
    args = parser.parse_args()
    report = energy_from_csv(args.csv, channel=args.channel, expected_duration_s=args.seconds)
    target = args.csv.with_suffix(f".{args.channel}.energy.json")
    target.write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False))
    print("Saved:", target.resolve())


if __name__ == "__main__":
    main()
