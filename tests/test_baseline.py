import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from delsys.emg.baseline import BaselineManager, BaselineRecord, CalibrationSession, EMGPreprocessor, FilterSettings, RMSFeatureExtractor, load_raw_emg_csv


class BaselineTests(unittest.TestCase):
    def test_calibration_discards_first_ten_seconds(self):
        session = CalibrationSession(100, {0})
        session.add(np.vstack((np.zeros((1000, 1)), np.ones((1000, 1)) * .002)))
        self.assertTrue(session.complete)
        self.assertAlmostEqual(session.rms_mv(0), 2.0, places=9)

    def test_rms_is_chunking_invariant(self):
        values = np.ones((20, 1)) * .003
        one = RMSFeatureExtractor(1, 100, .2).add(values)[0]
        extractor = RMSFeatureExtractor(1, 100, .2)
        extractor.add(values[:7]); many = extractor.add(values[7:])[0]
        self.assertEqual(one, many)

    def test_preprocessor_is_channel_isolated(self):
        settings = FilterSettings(5, 40)
        isolated = EMGPreprocessor(2, 200, settings).process(np.column_stack((np.ones(100), np.zeros(100))))
        self.assertTrue(np.allclose(isolated[:, 1], 0))

    def test_status_and_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = BaselineManager(Path(directory) / "settings.json")
            manager.set_manual(1, 1, "RF-R", "SID", 2000, FilterSettings(), "test")
            manager.warning_percent, manager.focus_percent, manager.thresholds_enabled = 20, 40, True
            manager.save()
            loaded = BaselineManager(Path(directory) / "settings.json")
            result = loaded.evaluate(1, 1.5, "RF-R", "SID", 2000, FilterSettings())
            self.assertEqual(result["state"], "focus")
            self.assertEqual(loaded.evaluate(1, None, "RF-R", "SID", 2000, FilterSettings())["text"], "資料不足")
            self.assertTrue(json.loads((Path(directory) / "settings.json").read_text())["baselines"])

    def test_invalid_and_incompatible_baselines_are_neutral(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = BaselineManager(Path(directory) / "settings.json")
            manager.records[1] = BaselineRecord(1, 0.0, "RF-R", "SID", "now", 2000, {"highpass_hz": 20.0, "lowpass_hz": 450.0, "notch_hz": None})
            self.assertEqual(manager.evaluate(1, 1.0, "RF-R", "SID", 2000, FilterSettings())["text"], "基準值過低")
            manager.records[1] = BaselineRecord(1, 1.0, "RF-R", "SID", "now", 2000, {"highpass_hz": 20.0, "lowpass_hz": 450.0, "notch_hz": None})
            self.assertEqual(manager.evaluate(1, 1.0, "changed", "SID", 2000, FilterSettings())["text"], "基準不相容")
            self.assertEqual(manager.evaluate(2, 1.0, "RF-L", "SID", 2000, FilterSettings())["text"], "無基準值")

    def test_incomplete_calibration_cannot_replace_a_baseline(self):
        session = CalibrationSession(100, {0})
        session.add(np.ones((1999, 1)) * .001)
        self.assertFalse(session.complete)
        self.assertIsNone(session.rms_mv(0))

    def test_raw_csv_loader_uses_original_samples(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "raw.csv"
            lines = ["time_seconds,Rectus femoris"]
            lines.extend(f"{index / 100:.2f},0.001" for index in range(2001))
            path.write_text("\n".join(lines), encoding="utf-8")
            times, columns, rate = load_raw_emg_csv(path)
            self.assertAlmostEqual(rate, 100.0)
            self.assertEqual(len(times), 2000)
            self.assertEqual(len(columns["Rectus femoris"]), 2000)
            all_times, all_columns, _ = load_raw_emg_csv(path, limit_seconds=None)
            self.assertEqual(len(all_times), 2001)
            self.assertEqual(len(all_columns["Rectus femoris"]), 2001)

