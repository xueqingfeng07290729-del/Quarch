"""DC power units, fixture discovery and full-rate acquisition regressions."""

import csv
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
import xml.etree.ElementTree as ET

from quarch_capture.channels import (available_power_channels, fixture_analog_groups,
                                    instrument_model, selected_synthetic_functions)
from quarch_capture.capture import export_channel_csv
from quarch_capture.energy import energy_from_csv
from quarch_capture.measurement import Recorder
from quarch_capture.qis_backend import QisBackend
from quarch_capture.stream_data import EnergySink
from quarch_capture.watch import parse_args


FIXTURE = Path(__file__).parent / "fixtures/qtl2980-channels.xml"


def definitions():
    root = ET.fromstring(FIXTURE.read_text())
    return [SimpleNamespace(function=c.findtext("Param[Name='Function']/Value"))
            for c in root.findall("SyntheticChannels/Channel")]


class PamChannelTests(unittest.TestCase):
    def test_real_fixture_exposes_two_rails_and_total_at_250khz(self):
        self.assertEqual(available_power_channels(definitions()), ("3.3V", "VIO_1V8", "Tot"))
        self.assertEqual(fixture_analog_groups(FIXTURE.read_text()), {0: 250000})
        self.assertEqual(instrument_model("Part#: QTL2312-01"), "QTL2312")
        with self.assertRaises(RuntimeError):
            instrument_model("Part#: QTL12312")

    def test_missing_or_faulted_fixture_is_rejected(self):
        for xml in ["<XmlResponse/>", FIXTURE.read_text().replace("<Value>OK</Value>", "<Value>FAIL</Value>")]:
            with self.assertRaises(ValueError):
                fixture_analog_groups(xml)
        with self.assertRaises(ValueError):
            available_power_channels([])

    def test_total_creates_dependencies_once_before_sum_even_if_out_of_order(self):
        parts = definitions()
        selected = selected_synthetic_functions([parts[2], parts[1], parts[0]], "Tot", False)
        self.assertEqual(selected, [p.function for p in parts])
        self.assertEqual(selected_synthetic_functions(parts, "3.3V", True), [parts[0].function])
        with self.assertRaises(ValueError):
            selected_synthetic_functions(parts, "L2", False)

    def test_microwatts_and_total_are_not_milliwatts_or_double_counted(self):
        sink = EnergySink("Tot", .01)
        sink.set_header("Time uS,3.3V power uW,VIO_1V8 power uW,Tot power uW")
        sink.feed("0,1500000,250000,1750000\n10000,2500000,250000,2750000\n")
        report = sink.snapshot()
        self.assertEqual(report["samples"], 2)
        self.assertAlmostEqual(report["Tot"]["mean_power_W"], 2.25)
        self.assertAlmostEqual(report["Tot"]["energy_J"], .045)

    def test_power_units_preserve_sign_and_reject_ambiguous_or_missing_power(self):
        for unit, value in [("W", -1.5), ("mW", -1500), ("uW", -1500000)]:
            sink = EnergySink("3.3V", .01)
            sink.set_header(f"Time uS,3.3V power {unit}")
            sink.feed(f"0,{value}\n")
            self.assertAlmostEqual(sink.snapshot()["3.3V"]["energy_J"], -.015)
        for header in ["Time uS,3.3V voltage mV,3.3V current uA",
                       "Time uS,3.3V power uW,3.3V power uW",
                       "Time uS,3.3V power kW"]:
            with self.assertRaises(ValueError):
                EnergySink("3.3V", .01).set_header(header)

    def test_csv_roundtrip_exports_only_the_selected_rail(self):
        with TemporaryDirectory() as folder:
            source, selected = Path(folder) / "all.csv", Path(folder) / "rail.csv"
            source.write_text("Time uS,3.3V voltage mV,3.3V current uA,3.3V power uW,VIO_1V8 power uW\n"
                              "0,3000,1000000,2800000,100\n1000,4000,1000000,3200000,100\n")
            export_channel_csv(source, selected, "3.3V")
            result = energy_from_csv(selected, channel="3.3V")
            # Integrate measured interval-mean power, not mean(V) * mean(I).
            self.assertAlmostEqual(result["3.3V"]["energy_J"], .006)
            self.assertEqual(result["output_rate_Hz"], 1000)
            with selected.open() as handle:
                self.assertNotIn("VIO_1V8 power uW", next(csv.reader(handle)))

    def test_resample_is_applied_before_restoring_full_rate_analogs(self):
        events = []
        backend = QisBackend("USB::test", "10ms")
        backend.model, backend.analog_groups, backend.minimum_interval_s = "QTL2312", {0: 250000}, .000004
        backend.record_state = Mock(return_value="OFF")
        backend.power = SimpleNamespace(stream_resample_mode=lambda value: events.append(value) or "OK")
        backend._command = lambda command: events.append(command)
        backend.set_resample("1ms")
        self.assertEqual(events, ["1ms", "rec:averaging:group 0 1"])
        with self.assertRaises(ValueError):
            selected_synthetic_functions(definitions(), "missing", False)

    def test_session_validates_the_connected_fixture_before_recording(self):
        with TemporaryDirectory() as folder:
            backend = SimpleNamespace(available_channels=("3.3V", "VIO_1V8", "Tot"), begin=Mock(),
                                      record_state=Mock(return_value="OFF"))
            recorder = Recorder(backend, "3.3V", .01, folder)
            with self.assertRaisesRegex(ValueError, "unavailable"):
                recorder.start(channel="L2", save_csv=True)
            backend.begin.assert_not_called()
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_watch_accepts_dc_and_retains_the_ac_rate_limit(self):
        self.assertEqual(parse_args(["--channel", "3.3V", "--4us"]).interval, "4us")
        self.assertEqual(parse_args(["--channel", "Tot"]).channel, "Tot")
        with self.assertRaises(SystemExit):
            parse_args(["--L2", "--4us"])


if __name__ == "__main__":
    unittest.main()
