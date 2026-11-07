from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from quarch_capture import capture
from quarch_capture.common import device_lock


class CaptureEntryTests(unittest.TestCase):
    def test_fixed_duration_capture_uses_the_same_device_lock(self):
        with tempfile.TemporaryDirectory() as folder, patch("quarch_capture.common.RUNTIME_DIR", Path(folder) / "run"):
            with device_lock("USB::TEST"), patch.object(sys, "argv", ["capture", "--device", "USB::TEST", "--channel", "L2"]):
                with self.assertRaisesRegex(RuntimeError, "already owns"):
                    capture.main()

    def test_an_already_recording_instrument_is_not_reconfigured(self):
        device = Mock()
        device.send_command.side_effect = ["QTL2843 simulated", "ON"]
        vendor_device = types.ModuleType("quarchpy.device")
        vendor_device.quarchDevice = Mock(return_value=device)
        vendor_device.quarchPPM = Mock()
        qis = Mock()
        qis.qis_scan_devices.return_value = ["USB::TEST"]
        vendor_qis = types.ModuleType("quarchpy.qis")
        vendor_qis.isQisRunning = lambda: True
        vendor_qis.startLocalQis = Mock(return_value=qis)
        vendor_qis.closeQis = Mock()
        with tempfile.TemporaryDirectory() as folder, \
                patch("quarch_capture.common.RUNTIME_DIR", Path(folder) / "run"), \
                patch.dict(sys.modules, {"quarchpy.device": vendor_device, "quarchpy.qis": vendor_qis}), \
                patch.object(sys, "argv", ["capture", "--device", "USB::TEST", "--channel", "L2", "--output", str(Path(folder) / "out.csv")]), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(capture.cli(), 1)
        self.assertEqual([call.args[0] for call in device.send_command.call_args_list], ["*IDN?", "rec?"])
        vendor_device.quarchPPM.assert_not_called()


if __name__ == "__main__":
    unittest.main()
