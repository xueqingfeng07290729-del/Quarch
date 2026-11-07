"""Offline integration and report durability, without instrument access."""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import tracemalloc
import unittest
from unittest.mock import patch

from quarch_capture import energy


class OfflineEnergyTests(unittest.TestCase):
    def test_known_energy_negative_power_and_missing_final_newline(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "power.csv"
            path.write_text("Time µs,L2_PInst VA mVA\n0,100000\n125,-200000\n250,300000")
            result = energy.energy_from_csv(path, channel="L2")
            self.assertAlmostEqual(result["L2"]["energy_J"], .025)
            self.assertEqual(result["samples"], 3)
            self.assertEqual(result["interval_s"], .000125)

    def test_nonuniform_and_invalid_samples_are_rejected(self):
        for data in ["0,1\n10000,2\n30000,3", "0,nan\n10000,2", "0\n10000,2", "0,1\n0,2"]:
            with self.subTest(data=data), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "bad.csv"
                path.write_text("Time uS,L2_PInst VA mVA\n" + data)
                with self.assertRaises(ValueError):
                    energy.energy_from_csv(path, channel="L2")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "unit.csv"
            path.write_text("Time unknown,L2_PInst VA mVA\n0,1\n1,2\n")
            with self.assertRaisesRegex(ValueError, "Unsupported timestamp unit"):
                energy.energy_from_csv(path, channel="L2")

    def test_cr_only_csv_remains_supported(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "mac.csv"
            path.write_bytes(b"Time uS,L2_PInst VA mVA\r0,100000\r10000,100000\r")
            self.assertEqual(energy.energy_from_csv(path, channel="L2")["L2"]["energy_J"], 2)

    def test_report_survives_failed_replacement(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "power.csv"
            path.write_text("Time uS,L2_PInst VA mVA\n0,100000\n10000,100000\n")
            with patch.object(sys, "argv", ["energy", str(path), "--channel", "L2"]), redirect_stdout(io.StringIO()):
                energy.main()
                report = path.with_suffix(".L2.energy.json")
                original = report.read_bytes()
                with patch("quarch_capture.common.os.replace", side_effect=OSError("disk failure")):
                    with self.assertRaises(OSError):
                        energy.main()
            self.assertEqual(report.read_bytes(), original)
            self.assertEqual(json.loads(original)["L2"]["energy_J"], 2)

    def test_large_csv_uses_bounded_memory(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "large.csv"
            with path.open("w") as handle:
                handle.write("Time uS,L2_PInst VA mVA\n")
                for i in range(100000):
                    handle.write(f"{i * 125},100000\n")
            tracemalloc.start()
            try:
                result = energy.energy_from_csv(path, channel="L2")
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            self.assertEqual(result["samples"], 100000)
            self.assertAlmostEqual(result["L2"]["energy_J"], 1250)
            self.assertLess(peak, 4 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
