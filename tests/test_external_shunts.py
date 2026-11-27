"""External-shunt regression: missing current channels, SI units and raw retention."""

import csv
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from quarch_capture.channels import selected_synthetic_functions
from quarch_capture.dram import MAPPING, convert_raw_csv, export_connected_raw_csv
from quarch_capture.shunts import configure_shunts, resistance_ohm, synthetic_definitions


def fixture():
    return ("<XmlResponse><Features><Feature><Param><Name>ExternalShunt</Name><Value>1</Value>"
            "</Param></Feature></Features><Channels>" + "".join(
                f"<Channel><Param><Name>Name</Name><Value>POWER_{i}</Value></Param>"
                "<Param><Name>Type</Name><Value>dV</Value></Param></Channel>"
                for i in range(1, 5)) + "</Channels></XmlResponse>")


def power(resistances):
    functions = [SimpleNamespace(function="", number=0)]
    functions.extend(SimpleNamespace(number=i, function=(
        f"chan(POWER_{i},power) Multiplier(W, micro, chan(POWER_{i},voltage), chan(POWER_{i},current))"))
        for i in range(1, 5))
    functions.append(SimpleNamespace(number=5, function=(
        "chan(Tot,power) sum(" + ",".join(f"chan(POWER_{i},power)" for i in range(1, 5)) + ")")))
    return SimpleNamespace(fixture_definition=fixture(),
                           parse_synthetic_channels_from_instrument=lambda: functions,
                           send_command=lambda command: resistances[command.split(":")[1]])


class ExternalShuntTests(unittest.TestCase):
    def test_five_milliohm_builds_current_before_power_and_total(self):
        definitions, shunts = synthetic_definitions(power({f"POWER_{i}": "5 milliOhm" for i in range(1, 5)}))
        selected = selected_synthetic_functions(definitions, "Tot", False, shunts)
        self.assertEqual(len(selected), 9)
        for i in range(1, 5):
            self.assertIn(f"chan(POWER_{i},current) Multiplier(A, micro, 200, chan(POWER_{i},dV))", selected)
            self.assertLess(next(n for n, s in enumerate(selected) if s.startswith(f"chan(POWER_{i},current)")),
                            next(n for n, s in enumerate(selected) if s.startswith(f"chan(POWER_{i},power)")))
        self.assertTrue(selected[-1].startswith("chan(Tot,power)"))

    def test_unset_unused_inputs_do_not_block_a_configured_single_input(self):
        definitions, shunts = synthetic_definitions(power({"POWER_1": "5 milliOhm", **{
            f"POWER_{i}": "0 Ohm" for i in range(2, 5)}}))
        self.assertEqual(len(selected_synthetic_functions(definitions, "POWER_1", False, shunts)), 2)
        with self.assertRaisesRegex(ValueError, "POWER_2 is unset"):
            selected_synthetic_functions(definitions, "Tot", False, shunts)

    def test_resistance_units_and_invalid_values(self):
        for value in ("5 milliOhm", "5000 MicroOhm", "0.005 Ohm", "5e-6 KiloOhm"):
            self.assertAlmostEqual(resistance_ohm(value), .005)
        self.assertEqual(resistance_ohm("0 Ohm"), 0)
        for value in ("-5 MilliOhm", "nan Ohm", "inf Ohm", "5", "FAIL", "1e999 Ohm"):
            with self.assertRaises(ValueError):
                resistance_ohm(value)

    def test_existing_current_definitions_and_other_fixtures_are_preserved(self):
        p = power({f"POWER_{i}": "5 milliOhm" for i in range(1, 5)})
        original = p.parse_synthetic_channels_from_instrument()
        original.append(SimpleNamespace(number=9, function="chan(POWER_1,current) Multiplier(A,micro,200,chan(POWER_1,dV))"))
        definitions, _ = synthetic_definitions(p)
        self.assertEqual(sum(d.function.startswith("chan(POWER_1,current)") for d in definitions), 1)
        p.fixture_definition = "<XmlResponse/>"
        p.send_command = Mock(side_effect=AssertionError("No shunt query for a normal fixture"))
        _, shunts = synthetic_definitions(p)
        self.assertEqual(shunts, {})

    def test_vendor_ampere_group_resolves_current_dependency_without_duplicates(self):
        p = power({f"POWER_{i}": "5 milliOhm" for i in range(1, 5)})
        original = p.parse_synthetic_channels_from_instrument()
        original.extend(SimpleNamespace(number=i + 10, function=(
            f"chan(POWER_{i},A) Multiplier(A, auto, 200, chan(POWER_{i},dv))")) for i in range(1, 5))
        definitions, shunts = synthetic_definitions(p)
        self.assertEqual(len(definitions), 9)
        selected = selected_synthetic_functions(definitions, "Tot", False, shunts)
        self.assertEqual(len(selected), 9)
        self.assertTrue(selected[0].startswith("chan(POWER_1,A)"))
        self.assertTrue(selected[1].startswith("chan(POWER_1,power)"))
        self.assertFalse(any(f.startswith("chan(POWER_1,current)") for f in selected))

    def test_zero_shunt_is_rejected_even_with_vendor_current_definitions(self):
        p = power({"POWER_1": "5 milliOhm", "POWER_2": "0 Ohm"})
        original = p.parse_synthetic_channels_from_instrument()
        original.extend(SimpleNamespace(number=i + 10, function=(
            f"chan(POWER_{i},A) Multiplier(A, auto, 0, chan(POWER_{i},dv))")) for i in (1, 2))
        definitions, shunts = synthetic_definitions(p, ["POWER_1", "POWER_2"])
        self.assertEqual(len(selected_synthetic_functions(definitions, "POWER_1", False, shunts)), 2)
        for selected in ("POWER_2", "Tot"):
            with self.subTest(selected=selected), self.assertRaisesRegex(ValueError, "POWER_2 is unset"):
                selected_synthetic_functions(definitions, selected, False, shunts)

    def test_active_pair_excludes_disconnected_inputs_from_total_and_queries(self):
        p = power({"POWER_1": "5 milliOhm", "POWER_2": "5 milliOhm"})
        definitions, shunts = synthetic_definitions(p, ["POWER_1", "POWER_2"])
        self.assertEqual(set(shunts), {"POWER_1", "POWER_2"})
        selected = selected_synthetic_functions(definitions, "Tot", False, shunts)
        self.assertEqual(len(selected), 5)
        self.assertEqual(selected[-1], "chan(Tot,power) sum(chan(POWER_1,power),chan(POWER_2,power))")
        self.assertFalse(any("POWER_3" in f or "POWER_4" in f for f in selected))
        with self.assertRaisesRegex(ValueError, "no instantaneous-power definition"):
            selected_synthetic_functions(definitions, "POWER_3", False, shunts)
        for active in (["POWER_1", "POWER_1"], ["UNKNOWN"], []):
            with self.assertRaises(ValueError):
                synthetic_definitions(p, active)

    def test_configuration_readback_skips_unchanged_values_and_rejects_recording(self):
        device = Mock()
        def reply(command):
            if command == "REC?":
                return "OFF"
            if command == "FIX:CHAN:XML?":
                return fixture()
            return "5 milliOhm"
        device.send_command.side_effect = reply
        values = {f"POWER_{i}": .005 for i in range(1, 5)}
        self.assertEqual(configure_shunts(device, values), values)
        self.assertFalse(any(":SHUNT " in c.args[0] for c in device.send_command.call_args_list))
        device.reset_mock()
        pair = {"POWER_1": .005, "POWER_2": .005}
        self.assertEqual(configure_shunts(device, pair), pair)
        self.assertFalse(any("POWER_3" in c.args[0] or "POWER_4" in c.args[0]
                             for c in device.send_command.call_args_list))
        device.reset_mock()
        device.send_command.side_effect = lambda command: "ON"
        with self.assertRaisesRegex(RuntimeError, "Stop recording"):
            configure_shunts(device, values)
        self.assertEqual(device.send_command.call_count, 1)


class DramCsvTests(unittest.TestCase):
    def raw_csv(self, path, times=(0, 10000)):
        headers = ["Time uS"]
        for i in range(1, 5):
            headers.extend(f"POWER_{i} {kind} {unit}" for kind, unit in
                           (("voltage", "mV"), ("dV", "udV"), ("current", "uA"), ("power", "uW")))
        with path.open("w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(headers)
            for time in times:
                # Deliberately retain QIS current/power independently of dV/R.
                writer.writerow([time, 1200, 5000, 1000001, 1300000, 1200, -100, -19999, -24000,
                                 *([1200, 5000, 1000001, 1300000] * 2)])

    def test_raw_is_unchanged_and_each_input_uses_its_own_resistance(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "raw.csv"
            self.raw_csv(source)
            before = source.read_bytes()
            shunts = {"POWER_1": .005, "POWER_2": .010}
            active = root / "raw_connected.csv"
            export_connected_raw_csv(source, active)
            with active.open() as f:
                active_headers = next(csv.reader(f))
            self.assertFalse(any("POWER_3" in h or "POWER_4" in h for h in active_headers))
            report = convert_raw_csv(active, root, shunts, .01)
            self.assertEqual(source.read_bytes(), before)
            self.assertEqual(report["samples"], 2)
            with (root / "dram.csv").open() as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(float(rows[0]["CH1_current_A"]), 1)
            self.assertAlmostEqual(float(rows[0]["CH2_current_A"]), -.01)
            self.assertFalse(any(h.startswith(("CH3_", "CH4_")) for h in rows[0]))
            self.assertAlmostEqual(float(rows[0]["CH1_qis_power_W"]), 1.3)
            self.assertAlmostEqual(float(rows[0]["CH1_qis_current_A"]), 1.000001)
            self.assertEqual(float(rows[1]["time_s"]), .01)
            for ch, riser in (("CH1", 1), ("CH2", 1)):
                resistor = MAPPING[ch]["resistor"]
                self.assertTrue((root / f"{ch}_riser{riser}_{resistor}.csv").exists())
                self.assertEqual(MAPPING[ch]["slot"], "DIMM1")
                self.assertIsNone(MAPPING[ch]["rail"])
            self.assertEqual(set(MAPPING), {"CH1", "CH2"})
            self.assertEqual(MAPPING["CH2"]["resistor"], "R5")

    def test_gaps_duplicates_nonfinite_and_missing_raw_columns_are_rejected(self):
        for times in ((0, 20000), (0, 0), (10000, 0), (0, float("nan"))):
            with self.subTest(times=times), TemporaryDirectory() as folder:
                root = Path(folder)
                source = root / "raw.csv"
                self.raw_csv(source, times)
                with self.assertRaises(ValueError):
                    convert_raw_csv(source, root, {f"POWER_{i}": .005 for i in range(1, 5)}, .01)
        with TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "raw.csv"
            self.raw_csv(source)
            source.write_text(source.read_text().replace("POWER_2 dV udV", "POWER_2 voltage mV"))
            with self.assertRaises(ValueError):
                convert_raw_csv(source, root, {f"POWER_{i}": .005 for i in range(1, 5)}, .01)


if __name__ == "__main__":
    unittest.main()
