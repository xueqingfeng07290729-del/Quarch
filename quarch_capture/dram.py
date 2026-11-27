"""Capture R1/R5 on one DIMM1 DDR4 riser through QTL2628; retain raw dV."""

import argparse
from contextlib import ExitStack
import csv
from datetime import datetime, timezone
import math
from pathlib import Path
import sys

from .capture import capture_to_csv
from .common import device_lock, interval_seconds, termination_guard, utc_now, write_json_atomic
from .shunts import configure_shunts


# User-provided physical mapping; electrical rail names are deliberately unknown.
MAPPING = {
    "CH1": {"instrument_channel": "POWER_1", "slot": "DIMM1", "riser": 1, "resistor": "R1", "rail": None},
    "CH2": {"instrument_channel": "POWER_2", "slot": "DIMM1", "riser": 1, "resistor": "R5", "rail": None},
}
FIELDS = ("measured_voltage_V", "shunt_voltage_V", "shunt_resistance_ohm",
          "current_A", "qis_current_A", "qis_power_W")


def measurement_column(headers, name, group, scales):
    found = [(i, scales[h.rsplit(" ", 2)[2]]) for i, h in enumerate(headers)
             if len(h.rsplit(" ", 2)) == 3 and h.rsplit(" ", 2)[:2] == [name, group]
             and h.rsplit(" ", 2)[2] in scales]
    if len(found) != 1:
        raise ValueError(f"Missing or ambiguous {name} {group} column")
    return found[0]


def export_connected_raw_csv(source, target):
    """Keep original values for connected inputs; exclude unused CH3/CH4 and digitals."""
    names = {info["instrument_channel"] for info in MAPPING.values()} | {"Tot"}
    with Path(source).open(encoding="utf-8-sig", newline="") as src, Path(target).open("x", encoding="utf-8", newline="") as dst:
        reader, writer = csv.reader(src), csv.writer(dst)
        headers = next(reader)
        indices = [i for i, h in enumerate(headers) if h.lower().startswith("time ") or h.rsplit(" ", 2)[0] in names]
        writer.writerow([headers[i] for i in indices])
        for row in reader:
            if not row:
                continue
            if len(row) != len(headers):
                raise ValueError("Malformed raw sample row")
            writer.writerow([row[i] for i in indices])


def convert_raw_csv(source, output_dir, shunts, period):
    """Stream raw QIS CSV to SI-unit wide/per-shunt CSV, without changing the source."""
    count, first, previous = 0, None, None
    with ExitStack() as stack:
        reader = csv.reader(stack.enter_context(Path(source).open(encoding="utf-8-sig", newline="")))
        headers = next(reader)
        times = [(i, h.split()[-1]) for i, h in enumerate(headers) if h.lower().startswith("time ")]
        time_scales = {"uS": 1e-6, "us": 1e-6, "ms": 1e-3, "s": 1, "ns": 1e-9}
        if len(times) != 1 or times[0][1] not in time_scales:
            raise ValueError("Missing or unsupported timestamp column")
        ti, unit = times[0]
        columns, writers = {}, {}
        for ch, info in MAPPING.items():
            name = info["instrument_channel"]
            if not math.isfinite(shunts[name]) or shunts[name] <= 0:
                raise ValueError(f"Invalid resistance for {name}")
            columns[ch] = [measurement_column(headers, name, group, scales) for group, scales in (
                ("voltage", {"V": 1, "mV": 1e-3, "uV": 1e-6}),
                ("dV", {"dV": 1, "mdV": 1e-3, "udV": 1e-6}),
                ("current", {"A": 1, "mA": 1e-3, "uA": 1e-6}),
                ("power", {"W": 1, "mW": 1e-3, "uW": 1e-6}))]
            path = Path(output_dir) / f"{ch}_riser{info['riser']}_{info['resistor']}.csv"
            writers[ch] = csv.writer(stack.enter_context(path.open("x", newline="", encoding="utf-8")))
            writers[ch].writerow(("time_s", *FIELDS))
        wide = csv.writer(stack.enter_context((Path(output_dir) / "dram.csv").open("x", newline="", encoding="utf-8")))
        wide.writerow(("time_s", *(f"{ch}_{field}" for ch in MAPPING for field in FIELDS)))
        for row in reader:
            if not row:
                continue
            if len(row) != len(headers):
                raise ValueError("Malformed raw sample row")
            timestamp = float(row[ti]) * time_scales[unit]
            if not math.isfinite(timestamp) or timestamp < 0:
                raise ValueError("Invalid timestamp")
            if previous is not None and not math.isclose(timestamp - previous, period, rel_tol=1e-7, abs_tol=1e-10):
                raise ValueError("Missing, duplicate or out-of-order raw samples")
            combined = [timestamp]
            for ch, info in MAPPING.items():
                voltage, dv, qis_current, power = (float(row[i]) * scale for i, scale in columns[ch])
                resistance = shunts[info["instrument_channel"]]
                values = (voltage, dv, resistance, dv / resistance, qis_current, power)
                if not all(math.isfinite(v) for v in values):
                    raise ValueError(f"Non-finite raw measurement in {ch}")
                writers[ch].writerow((timestamp, *values))
                combined.extend(values)
            wide.writerow(combined)
            first = timestamp if first is None else first
            previous = timestamp
            count += 1
        if not count:
            raise ValueError("No raw samples captured")
    return {"samples": count, "interval_s": period, "duration_s": count * period,
            "first_timestamp_s": first, "last_timestamp_s": previous}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", required=True, help="QTL2312 target, e.g. USB::QTL2312-01-578")
    parser.add_argument("--channel", required=True, choices=["Tot", "POWER_1", "POWER_2"],
                        help="Energy summary: Tot sums only connected CH1/R1 and CH2/R5")
    parser.add_argument("--seconds", type=float, default=30)
    parser.add_argument("--resample", default="10ms", help="QIS output interval (default 10ms); native analog interval is 4us")
    parser.add_argument("--shunt-mohm", type=float, nargs=2, default=[5, 5],
                        metavar=("CH1_R1", "CH2_R5"), help="CH1/R1 and CH2/R5 resistances in milliohms")
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args(argv)
    if not math.isfinite(args.seconds) or args.seconds <= 0:
        parser.error("seconds must be finite and positive")
    if any(not math.isfinite(v) or v <= 0 for v in args.shunt_mohm):
        parser.error("Both active shunt resistances must be finite and positive")
    period = interval_seconds(args.resample)
    output = (args.output_dir or Path("results/dram") / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")).resolve()
    output.mkdir(parents=True, exist_ok=False)
    requested = {info["instrument_channel"]: value * .001 for info, value in zip(MAPPING.values(), args.shunt_mohm)}
    report = {"started_at": utc_now(), "status": "running", "device": args.device,
              "channel_mapping": MAPPING, "rail_mapping_verified": False,
              "unused_channels": ["POWER_3", "POWER_4"],
              "requested_shunts_ohm": requested, "requested_duration_s": args.seconds,
              "requested_interval_s": period, "current_method": "current_A = shunt_voltage_V / shunt_resistance_ohm",
              "sample_mean_note": "Resampled values are interval means, not full-rate raw ADC samples",
              "power_note": "Use QIS mean power or rescale it by old_R/new_R; do not multiply mean voltage by mean current"}
    write_json_atomic(output / "run.json", report)

    def prepare(device):
        identity = device.send_command("*IDN?")
        fixture = device.send_command("FIX:IDN?")
        if "QTL2312" not in identity or "QTL2628" not in fixture:
            raise RuntimeError("DRAM capture requires QTL2312 with QTL2628; configuration not changed")
        report.update(instrument_identity=identity, fixture_identity=fixture,
                      shunt_resistances_ohm=configure_shunts(device, requested))
        write_json_atomic(output / "run.json", report)

    args.output, args.inspect, args.ip = output / "selected.csv", False, None
    args.active_channels = list(requested)
    try:
        with device_lock(args.device):
            if capture_to_csv(args, parser, prepare) != 0:
                raise RuntimeError("QIS capture did not complete")
        diagnostic = output / "selected.all-channels.csv"
        source = output / "raw.csv"
        export_connected_raw_csv(diagnostic, source)
        report.update(convert_raw_csv(source, output, report["shunt_resistances_ohm"], period))
        if not math.isclose(report["duration_s"], args.seconds, rel_tol=1e-7, abs_tol=1e-9):
            raise RuntimeError("Captured samples do not cover the requested duration")
        report.update(status="complete", raw_csv=str(source), derived_csv=str(output / "dram.csv"),
                      diagnostic_csv=str(diagnostic), active_shunt_channels=args.active_channels)
        print("Raw measurements:", source)
        print("DIMM1 R1/R5 SI measurements:", output / "dram.csv")
        print("Independent channel CSVs: CH1_riser1_R1.csv, CH2_riser1_R5.csv")
        return 0
    except BaseException as exc:
        report.update(status="interrupted" if isinstance(exc, KeyboardInterrupt) else "failed",
                      error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        report["finished_at"] = utc_now()
        write_json_atomic(output / "run.json", report)


@termination_guard()
def cli():
    try:
        return main()
    except KeyboardInterrupt:
        print("Interrupted; raw data retained, run is not marked complete.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(cli())
