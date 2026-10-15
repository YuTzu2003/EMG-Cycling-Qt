import tempfile
import unittest
from pathlib import Path

from delsys.pedal.fit_export import exportPedalFit
from delsys.pedal.fit_import import load_pedal_fit


class FitImportTests(unittest.TestCase):
    def test_reads_standard_records_from_exported_fit(self):
        source = [
            {"timestamp": 1_700_000_000.0, "power_w": 200, "cadence_rpm": 90, "left_balance_percent": 47},
            {"timestamp": 1_700_000_001.0, "power_w": 220, "cadence_rpm": 95, "left_balance_percent": 48},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = exportPedalFit(source, Path(directory) / "activity.fit")
            rows = load_pedal_fit(path)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["power_w"], 200)
        self.assertEqual(rows[1]["cadence_rpm"], 95)
        self.assertEqual(rows[0]["left_balance_percent"], 47)
        self.assertEqual(rows[1]["right_balance_percent"], 52)
        self.assertEqual(rows[1]["timestamp"] - rows[0]["timestamp"], 1)
