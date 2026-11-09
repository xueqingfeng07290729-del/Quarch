#!/usr/bin/env python3
"""Verify published SSD measurements offline, without QIS or a database."""

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from quarch_capture.energy import energy_from_csv


def require(condition, message):
    if not condition:
        raise ValueError(message)


def close(a, b):
    return math.isclose(float(a), float(b), rel_tol=1e-10, abs_tol=1e-10)


def local_path(root, relative):
    path = (root / relative).resolve()
    require(path.is_relative_to(root.resolve()), f"Path leaves archive: {relative}")
    return path


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def read_csv(path):
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def verify_run(folder, queries):
    metadata = read_json(folder / "run.json")
    rows, summary = read_csv(folder / "runs.csv"), read_csv(folder / "summary.csv")
    require(metadata["status"] == "complete" and metadata["database_file_unchanged"], "Incomplete or modified database run")
    require(metadata["queries"] == queries and metadata["runs_per_query"] == 3, "Unexpected run configuration")
    require(len(rows) == len(queries) * 3 and len(summary) == len(queries), "Unexpected result count")
    require({int(row["query_id"]) for row in rows} == set(queries), "Query set mismatch")
    sessions = set()
    for row in rows:
        capture_path = local_path(folder, row["capture_json"])
        capture = read_json(capture_path)
        require(capture["complete"] and capture["summary_saved"] and not capture["errors"], f"Incomplete: {capture_path}")
        require(capture["device_record_state"] == "OFF" and capture["selected_channel"] == "3.3V", "Capture state/channel mismatch")
        require(capture["session_id"] not in sessions, "Duplicate capture session")
        sessions.add(capture["session_id"])
        require(row["capture_session_id"] == capture["session_id"], "CSV/capture session mismatch")
        require(local_path(capture_path.parent, capture["summary_path"]) == capture_path, "Broken summary path")
        require(capture["samples"] > 0 and close(capture["interval_s"], .01), "Invalid sampling interval")
        power = capture["3.3V"]
        require(close(power["energy_J"], row["energy_total_J"]), "CSV/capture energy mismatch")
        require(close(power["energy_J"], power["energy_Wh"] * 3600), "J/Wh mismatch")
        require(close(power["energy_J"], power["mean_power_W"] * capture["duration_s"]), "Power integral mismatch")
        require(close(capture["samples"] * .01, row["recorded_duration_s"]), "Duration/sample mismatch")
        require(int(row["executions"]) == 1 and float(row["sql_total_s"]) > 0, "Invalid SQL execution")
        require(float(row["cached_fraction_after"]) <= .001 and int(row["process_read_bytes"]) > 0, "Cold-cache/I/O check failed")
    for row in summary:
        group = [r for r in rows if r["query_id"] == row["query_id"]]
        require({int(r["batch"]) for r in group} == {1, 2, 3}, "Missing/duplicate repetition")
        energy = [float(r["energy_total_J"]) for r in group]
        sql = [float(r["sql_total_s"]) for r in group]
        require(close(statistics.median(energy), row["energy_median_J"]), "Energy median mismatch")
        require(close(statistics.mean(energy), row["energy_mean_J"]), "Energy mean mismatch")
        require(close(statistics.median(sql), row["sql_median_s"]), "SQL median mismatch")
    return rows, summary


def verify(folder):
    listed = set()
    for line in (folder / "SHA256SUMS").read_text().splitlines():
        expected, relative = line.split("  ", 1)
        path = local_path(folder, relative)
        require(hashlib.sha256(path.read_bytes()).hexdigest() == expected, f"Checksum mismatch: {relative}")
        require(relative not in listed, f"Duplicate checksum: {relative}")
        listed.add(relative)
    actual = {str(p.relative_to(folder)) for p in folder.rglob("*") if p.is_file() and p.name != "SHA256SUMS"}
    require(actual == listed, "Checksum inventory does not match archive files")
    manifest = read_json(folder / "manifest.json")
    for item in manifest["files"]:
        require(hashlib.sha256(local_path(folder, item["path"]).read_bytes()).hexdigest() == item["published_sha256"],
                f"Manifest mismatch: {item['path']}")
    rows, summary = verify_run(folder / "full-run", list(range(1, 23)))
    repeat, repeat_summary = verify_run(folder / "q19-cooldown", [19])
    review = read_csv(folder / "full-run/review.csv")
    require(len(review) == 22, "Review query count mismatch")
    for row, original in zip(review, summary):
        require(row["query_id"] == original["query_id"] and close(row["energy_median_J"], original["energy_median_J"]),
                "Review replaced original measurements")
        if row["query_id"] == "19":
            require(close(row["cooldown30s_energy_median_J"], repeat_summary[0]["energy_median_J"]), "Q19 repeat mismatch")
    hardware = folder / "hardware-check"
    integrated = energy_from_csv(hardware / "total-1ms.csv", channel="Tot", expected_duration_s=3)
    saved = read_json(hardware / "total-1ms.energy.json")
    require(integrated["samples"] == 3000 and integrated["requested_duration_matches"], "Hardware sample count mismatch")
    require(close(integrated["Tot"]["energy_J"], saved["Tot"]["energy_J"]), "Hardware CSV integral mismatch")
    unplug = read_json(hardware / "physical-usb-replug.json")
    samples = read_csv(hardware / "physical-usb-replug.csv")
    times = [float(row["time_s"]) for row in samples]
    require(len(samples) == unplug["samples"] == 308 and times[0] == 0, "USB replug sample count mismatch")
    require(all(close(b - a, .01) for a, b in zip(times, times[1:])), "USB replug timestamp gap")
    return {"checksums_verified": len(listed), "complete_full_run_queries": len(summary),
            "complete_full_run_captures": len(rows), "complete_q19_repeat_captures": len(repeat),
            "hardware_csv_samples": integrated["samples"], "physical_usb_replug_samples": len(samples),
            "q19_repeat_energy_median_J": float(repeat_summary[0]["energy_median_J"])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path, nargs="?", default=ROOT / "experiments/2026-11-09-tpch-sf30-ssd")
    args = parser.parse_args()
    print(json.dumps(verify(args.archive.resolve()), indent=2))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError) as exc:
        print(f"Verification failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
