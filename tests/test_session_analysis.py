import tempfile
import unittest
from pathlib import Path

from delsys.session_analysis import (add_point, analyze, compare_rms, export_html,
                                     load_session, new_session, save_session, summary_lines)


class SessionAnalysisTests(unittest.TestCase):
    def test_signed_baseline_comparison_and_invalid_baseline(self):
        self.assertAlmostEqual(compare_rms(0.08, 0.10)["delta_mv"], -0.02)
        self.assertAlmostEqual(compare_rms(0.08, 0.10)["normalized"], 0.8)
        self.assertAlmostEqual(compare_rms(0.08, 0.10)["percent"], -20)
        self.assertIsNone(compare_rms(0.08, 0)["normalized"])
        self.assertEqual(compare_rms(float("nan"), 0.10)["quality"], "EMG 資料不足")

    def test_sparse_alignment_segments_and_round_trip(self):
        session = new_session({"patientId": "P1", "name": "<researcher>"}, {"1": {"rms_mv": 0.1}})
        for t in (0.2, 1.2, 2.2):
            add_point(session, "emg", {"time_s": t, "slot": 1, "rms_mv": 0.2,
                                        "delta_mv": 0.1, "normalized": 2, "quality": "有效"})
        add_point(session, "pedal", {"time_s": 0.9, "power_w": 120, "cadence_rpm": 70})
        add_point(session, "trainer", {"time_s": 1.0, "resistance_native_reported": 5, "power_watts": 120})
        result = analyze(session)
        self.assertEqual(result["emg"]["1"]["comparison"]["mean"], 0.1)
        self.assertEqual(analyze(session, True)["emg"]["1"]["comparison"]["mean"], 2)
        self.assertEqual(result["aligned"][1]["emg_channels"]["1"]["rms_mv"], 0.2)
        self.assertEqual(result["resistance_segments"][0]["power_w"], 120)
        self.assertNotIn("pedal", result["aligned"][1])
        self.assertIn("RMS - baseline", "\n".join(summary_lines(result)))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "ride.analysis.json"
            save_session(path, session)
            self.assertEqual(load_session(path)["subject"]["patientId"], "P1")
            report = Path(folder) / "report.html"
            export_html(report, session, ftp_w=220)
            document = report.read_text(encoding="utf-8")
            self.assertIn("&lt;researcher&gt;", document)
            self.assertIn("踏板功率", document)
            self.assertIn("FTP 設定：220 W", document)
            self.assertIn("資料空窗不視為零功率", document)

    def test_missing_streams_and_incompatible_baseline_remain_explicit(self):
        session = new_session({}, {"2": {"rms_mv": 0.1}})
        add_point(session, "emg", {"time_s": 1, "slot": 2, "rms_mv": 0.2,
                                    "quality": "基準不相容"})
        result = analyze(session)
        self.assertEqual(result["emg"]["2"]["comparison"]["count"], 0)
        self.assertIn("S02：基準不相容", result["warnings"])
        self.assertIn("沒有踏板資料", result["warnings"])

    def test_distant_device_samples_do_not_get_misassigned(self):
        session = new_session({}, {})
        add_point(session, "emg", {"time_s": 1, "slot": 1, "rms_mv": 0.2,
                                    "quality": "無基準值"})
        add_point(session, "pedal", {"time_s": 20, "power_w": 100})
        result = analyze(session)
        self.assertIsNone(result["aligned"][10]["emg"])
        self.assertNotIn("pedal", result["aligned"][10])
        self.assertNotIn("EMG 與阻力計沒有可對齊的重疊時間", result["warnings"])

    def test_manual_emg_offset_changes_alignment_but_not_session_rms(self):
        session = new_session({}, {})
        add_point(session, "emg", {"time_s": 1, "slot": 1, "rms_mv": 0.25,
                                    "quality": "無基準值"})
        add_point(session, "trainer", {"time_s": 6, "resistance_native_reported": 5})
        session["emg_trainer_offset_s"] = 5
        result = analyze(session)
        self.assertEqual(result["aligned"][6]["emg_channels"]["1"]["rms_mv"], 0.25)
        self.assertEqual(result["emg"]["1"]["rms_mv"]["mean"], 0.25)
        self.assertEqual(session["emg"][0]["time_s"], 1)

    def test_nonoverlapping_emg_and_trainer_do_not_form_load_segment(self):
        session = new_session({}, {})
        add_point(session, "emg", {"time_s": 1, "slot": 1, "rms_mv": 0.2, "quality": "有效"})
        add_point(session, "trainer", {"time_s": 5, "resistance_native_reported": 8})
        result = analyze(session)
        self.assertEqual(result["resistance_segments"], [])
        self.assertIn("EMG 與阻力計沒有可對齊的重疊時間", result["warnings"])


if __name__ == "__main__":
    unittest.main()
