import math
import tempfile
import unittest
from pathlib import Path

from delsys.emg.baseline import FilterSettings
from delsys.emg.sensor_mapping import SensorMapping
from delsys.gui import EMGAnalysisImportWorker


class EMGAnalysisImportTests(unittest.TestCase):
    def test_raw_csv_import_builds_rms_analysis_for_full_recording(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "ride.csv"
            samples = ["time_seconds,S01_Rectus femoris"]
            samples.extend(
                f"{index / 1000:.3f},{0.001 * math.sin(index / 12):.8f}"
                for index in range(1000)
            )
            path.write_text("\n".join(samples), encoding="utf-8")
            worker = EMGAnalysisImportWorker(
                path, [SensorMapping(1, "右股直肌", "Rectus femoris", "RF-R")], {}, {}, FilterSettings(),
            )
            completed, errors = [], []
            worker.completed.connect(lambda session, filename: completed.append((session, filename)))
            worker.failed.connect(errors.append)
            worker.run()

        self.assertEqual(errors, [])
        self.assertEqual(completed[0][1], "ride.csv")
        session = completed[0][0]
        self.assertEqual(session["raw_files"], {"emg": "ride.csv"})
        self.assertEqual(len(session["emg"]), 5)
        self.assertTrue(all(point["rms_mv"] is not None for point in session["emg"]))


if __name__ == "__main__":
    unittest.main()
