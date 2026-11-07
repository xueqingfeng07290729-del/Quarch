#!/usr/bin/env python3
"""Summarize a complete-channel Quarch AC capture and export analysis files."""

import argparse
import hashlib
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


def stats(values, times):
    valid = np.isfinite(values)
    v, t = np.asarray(values)[valid], np.asarray(times)[valid]
    if not len(v):
        return None
    return {
        "count": int(len(v)), "mean": float(v.mean()),
        "median": float(np.median(v)), "std": float(v.std()),
        "min": float(v.min()), "min_time_s": float(t[v.argmin()]),
        "max": float(v.max()), "max_time_s": float(t[v.argmax()]),
        "p95": float(np.quantile(v, .95)), "p99": float(np.quantile(v, .99)),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", nargs="?", type=Path, default=Path("measure.csv"))
    parser.add_argument("--outdir", type=Path, default=Path("measure_analysis"))
    parser.add_argument("--after", type=float, default=2, help="Start of the later comparison interval, in seconds")
    parser.add_argument("--baseline", type=Path, help="Optional previous capture for startup comparison")
    args = parser.parse_args()
    df = pd.read_csv(args.input)
    t = df["Time uS"].to_numpy(dtype=float) / 1e6
    delta = np.diff(df["Time uS"].to_numpy(dtype=float))
    if len(delta) == 0 or np.any(delta <= 0):
        raise ValueError("Timestamps must be strictly increasing")
    dt = float(np.median(delta)) / 1e6
    if not np.all(delta == delta[0]):
        raise ValueError("Uneven timestamps: investigate missing samples before calculating energy")
    raw_columns = [f"{rail} {kind} {unit}" for rail in ["L1", "L2", "L3"]
                   for kind, unit in [("current", "mA"), ("voltage", "mV")]]
    raw_columns += ["Neutral current mA", "Tot_PInst VA mVA"]
    if not np.isfinite(df[raw_columns].to_numpy()).all():
        raise ValueError("Missing or non-finite raw samples")
    late = t >= args.after
    if not late.any():
        raise ValueError("The selected later interval is outside the capture")

    with args.input.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    summary = {
        "source": str(args.input.resolve()), "sha256": digest,
        "samples": len(df), "columns": len(df.columns),
        "sample_interval_us": dt * 1e6, "sample_rate_Hz": 1 / dt,
        "recorded_duration_s": len(df) * dt,
        "last_timestamp_s": float(t[-1]), "continuous_timestamps": True,
        "comparison_interval_start_s": args.after,
        "power_window_ms": 100,
        "power_window_evidence": "QIS setup log uses pActive(100mS, ...) and rms(100ms, ...)",
        "missing_values": {c: int(n) for c, n in df.isna().sum().items() if n},
        "first_valid_s": {}, "internal_missing_after_first_valid": {}, "channels": {},
    }
    for column in df:
        first = df[column].first_valid_index()
        if first is not None:
            summary["first_valid_s"][column] = float(t[first])
            holes = int(df.loc[first:, column].isna().sum())
            if holes:
                summary["internal_missing_after_first_valid"][column] = holes

    for rail in ["L1", "L2", "L3", "Tot"]:
        active = df[f"{rail}_PAct power mW"].to_numpy() / 1000
        instant = df[f"{rail}_PInst VA mVA"].to_numpy() / 1000
        item = {
            "active_100ms_W": stats(active, t),
            "active_100ms_after_interval_W": stats(active[late], t[late]),
            "instantaneous_W": stats(instant, t),
            "whole_record_mean_W": float(instant.mean()),
            "whole_record_energy_Wh": float(instant.sum() * dt / 3600),
        }
        if rail != "Tot":
            for suffix, output_name, scale in [
                ("RMS voltage mV", "voltage_rms_V", 1000),
                ("RMS current mA", "current_rms_A", 1000),
                ("PF PF% %", "power_factor", 100),
            ]:
                a = df[f"{rail}_{suffix}"].to_numpy() / scale
                item[output_name] = stats(a, t)
                item[output_name + "_after_interval"] = stats(a[late], t[late])
            item["frequency_Hz"] = stats(df[f"{rail}V_Freq Freq mHz"].to_numpy() / 1000, t)
            raw_current = df[f"{rail} current mA"].to_numpy() / 1000
            peak = int(np.argmax(np.abs(raw_current)))
            item["raw_current_abs_peak_A"] = float(abs(raw_current[peak]))
            item["raw_current_peak_signed_A"] = float(raw_current[peak])
            item["raw_current_peak_time_s"] = float(t[peak])
        summary["channels"][rail] = item

    residual = df["Tot_PAct power mW"] - sum(df[f"{r}_PAct power mW"] for r in ["L1", "L2", "L3"])
    summary["max_total_vs_channel_sum_difference_W"] = float(residual.abs().max() / 1000)
    if args.baseline:
        base = pd.read_csv(args.baseline)
        bt = base["Time uS"].to_numpy() / 1e6
        summary["previous_capture"] = {
            "source": str(args.baseline.resolve()),
            "active_100ms_W": stats(base["Tot_PAct power mW"].to_numpy() / 1000, bt),
            "instantaneous_W": stats(base["Tot_PInst VA mVA"].to_numpy() / 1000, bt),
        }

    # One-second energy averages use the complete instantaneous-power series.
    # The 100 ms derived series starts only after its initial calculation window.
    table = pd.DataFrame({
        "second_start_s": np.floor(t).astype(int),
        "total_mean_W": df["Tot_PInst VA mVA"] / 1000,
        "total_100ms_peak_W": df["Tot_PAct power mW"] / 1000,
        "L2_mean_W": df["L2_PInst VA mVA"] / 1000,
        "L2_voltage_rms_V": df["L2_RMS voltage mV"] / 1000,
        "L2_current_rms_A": df["L2_RMS current mA"] / 1000,
        "L2_power_factor": df["L2_PF PF% %"] / 100,
    }).groupby("second_start_s").agg({
        "total_mean_W": "mean", "total_100ms_peak_W": "max", "L2_mean_W": "mean",
        "L2_voltage_rms_V": "mean", "L2_current_rms_A": "mean", "L2_power_factor": "mean",
    })
    args.outdir.mkdir(parents=True, exist_ok=True)
    table.to_csv(args.outdir / "one_second_summary.csv", float_format="%.6f")
    (args.outdir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False, allow_nan=False) + "\n")

    plt.rcParams.update({"font.size": 10, "axes.spines.top": False, "axes.spines.right": False})
    fig, axes = plt.subplots(2, 2, figsize=(14, 8.5), layout="constrained")
    blue, orange, gray = "#176B9A", "#C77810", "#657383"
    power = df["Tot_PAct power mW"].to_numpy() / 1000
    stride = max(1, round(.001 / dt))
    thin = np.arange(0, len(t), stride)
    peak = int(np.nanargmax(power))
    mean_late = summary["channels"]["Tot"]["active_100ms_after_interval_W"]["mean"]

    ax = axes[0, 0]
    ax.axvspan(0, args.after, color=orange, alpha=.10, label="Initial interval")
    ax.plot(t[thin], power[thin], color=blue, lw=1, label="Total active power (100 ms)")
    ax.hlines(mean_late, args.after, t[-1], color=orange, ls="--", lw=1.2,
              label=f"After {args.after:g} s mean: {mean_late:.2f} W")
    ax.scatter([t[peak]], [power[peak]], color=orange, s=25, zorder=4)
    ax.annotate(f"{power[peak]:.1f} W at {t[peak]:.3f} s", (t[peak], power[peak]),
                xytext=(5, power[peak] * .82), arrowprops={"arrowstyle": "->", "color": orange}, color=orange)
    ax.set(xlim=(0, len(df)*dt), ylim=(0, power[peak]*1.13), title="A  Full capture", ylabel="Active power (W)")
    ax.legend(loc="upper right", frameon=False, fontsize=9)

    ax = axes[0, 1]
    selected = thin[t[thin] >= args.after]
    ax.plot(t[selected], power[selected], color=blue, lw=1)
    ax.axhline(mean_late, color=orange, ls="--", lw=1)
    ax.set(xlim=(args.after, len(df)*dt), title=f"B  Detail after {args.after:g} s", ylabel="Active power (W)")

    ax = axes[1, 0]
    initial = t < min(1, t[-1])
    current = df["L2 current mA"].to_numpy() / 1000
    ax.plot(t[initial], current[initial], color=gray, lw=.65, label=f"L2 current ({dt * 1e6:g} us output interval)")
    ax.plot(t[initial], df.loc[initial, "L2_RMS current mA"] / 1000, color=blue, lw=1.5, label="L2 RMS current (100 ms)")
    ci = int(np.argmax(np.abs(current)))
    ax.scatter([t[ci]], [current[ci]], color=orange, s=25, zorder=4)
    ax.annotate(f"{current[ci]:.2f} A at {t[ci]:.3f} s", (t[ci], current[ci]),
                xytext=(.55, current[ci]*.70), arrowprops={"arrowstyle": "->", "color": orange}, color=orange)
    ax.set(xlim=(0, min(1, t[-1])), title="C  Initial current transient on L2", ylabel="Current (A)")
    ax.legend(frameon=False, loc="upper right", fontsize=9)

    ax = axes[1, 1]
    ax.plot(t[thin], df["L2_RMS voltage mV"].to_numpy()[thin] / 1000, color=blue, lw=1)
    ax.set(xlim=(0, len(df)*dt), title="D  L2 supply voltage", ylabel="RMS voltage (V)")
    for ax in axes.flat:
        ax.set_xlabel("Time since recording started (s)")
        ax.grid(alpha=.2)
    fig.suptitle(f"QTL2843 | {args.input.name} | {len(df)*dt:g} s, {1/dt:,.0f} samples/s\n"
                 f"Output interval: {dt * 1e6:g} us. RMS and active-power channels: 100 ms calculation window.", fontsize=13)
    fig.savefig(args.outdir / "power_analysis.png", dpi=170)
    plt.close(fig)
    print(json.dumps({
        "samples": summary["samples"], "duration_s": summary["recorded_duration_s"],
        "total_mean_W": summary["channels"]["Tot"]["whole_record_mean_W"],
        "comparison_interval_start_s": args.after,
        "total_mean_after_interval_W": mean_late,
        "total_100ms_peak_W": summary["channels"]["Tot"]["active_100ms_W"]["max"],
        "energy_Wh": summary["channels"]["Tot"]["whole_record_energy_Wh"],
        "outputs": str(args.outdir.resolve()),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
