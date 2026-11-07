"""Parse streamed samples, integrate instantaneous power and optionally write CSV."""

import csv
import math
from pathlib import Path
import threading


class EnergySink:
    """Bounded-memory interval integration, with optional selected-channel CSV."""

    def __init__(self, channel, period, csv_path=None):
        self.channel, self.period = channel, period
        self.csv_path = Path(csv_path) if csv_path else None
        self.handle = self.csv_path.open("x", newline="", encoding="utf-8") if self.csv_path else None
        self.writer = csv.writer(self.handle) if self.handle else None
        self.lock = threading.RLock()
        self.headers = None
        self.pending = ""
        self.count = 0
        self.sum_w = self.compensation = 0.0
        self.minimum = self.maximum = None
        self.first_t = self.last_t = None

    def set_header(self, header):
        with self.lock:
            headers = next(csv.reader([header]))
            timestamps = [i for i, c in enumerate(headers) if c.lower().startswith("time ")]
            if len(timestamps) != 1:
                raise ValueError("QIS returned an invalid timestamp header")
            self.time_index = timestamps[0]
            units = {"ns": 1_000_000_000, "us": 1_000_000, "µs": 1_000_000, "μs": 1_000_000, "ms": 1000, "s": 1}
            unit = headers[self.time_index].split()[-1].lower()
            if unit not in units:
                raise ValueError(f"Unsupported timestamp unit: {unit}")
            self.time_divisor = units[unit]
            power_name = f"{self.channel}_PInst VA mVA"
            if power_name not in headers:
                raise ValueError(f"QIS did not provide the selected power channel: {power_name}")
            self.power_index = headers.index(power_name)
            self.selected_indices = [self.time_index] + [
                i for i, c in enumerate(headers)
                if c.startswith((self.channel + " ", self.channel + "_", self.channel + "V_"))
            ]
            self.headers = headers
            if self.writer:
                self.writer.writerow([headers[i] for i in self.selected_indices])
                self.handle.flush()

    def feed(self, text):
        with self.lock:
            self.pending += text
            lines = self.pending.split("\n")
            self.pending = lines.pop()
            if len(self.pending) > 65536:
                raise ValueError("QIS returned an overlong incomplete row")
            for line in lines:
                if not line.strip():
                    continue
                row = next(csv.reader([line.rstrip("\r")]))
                if self.headers is None or len(row) != len(self.headers):
                    raise ValueError("Malformed QIS sample row")
                timestamp = float(row[self.time_index])
                power_w = float(row[self.power_index]) / 1000
                if not math.isfinite(timestamp) or not math.isfinite(power_w):
                    raise ValueError("Non-finite timestamp or selected-channel power")
                if self.last_t is not None and not math.isclose(
                    timestamp - self.last_t, self.period * self.time_divisor,
                    rel_tol=1e-6, abs_tol=1e-7,
                ):
                    raise ValueError("Missing, duplicate or out-of-order QIS samples")
                if self.first_t is None:
                    self.first_t = timestamp
                self.last_t = timestamp
                corrected = power_w - self.compensation
                combined = self.sum_w + corrected
                self.compensation = (combined - self.sum_w) - corrected
                self.sum_w = combined
                self.count += 1
                self.minimum = power_w if self.minimum is None else min(self.minimum, power_w)
                self.maximum = power_w if self.maximum is None else max(self.maximum, power_w)
                if self.writer:
                    self.writer.writerow([row[i] for i in self.selected_indices])

    def snapshot(self):
        with self.lock:
            duration = self.count * self.period
            joules = self.sum_w * self.period if self.count else None
            return {
                "samples": self.count, "interval_s": self.period, "duration_s": duration,
                "sample_start_s": self.first_t / self.time_divisor if self.first_t is not None else None,
                "sample_end_exclusive_s": self.last_t / self.time_divisor + self.period if self.last_t is not None else None,
                "selected_channel": self.channel,
                self.channel: {
                    "mean_power_W": self.sum_w / self.count if self.count else None,
                    "energy_J": joules, "energy_Wh": joules / 3600 if joules is not None else None,
                    "min_interval_mean_W": self.minimum, "max_interval_mean_W": self.maximum,
                },
            }

    def close(self):
        with self.lock:
            try:
                if self.pending.strip():
                    raise ValueError("Capture ended with an incomplete sample row")
            finally:
                if self.handle:
                    self.handle.close()
