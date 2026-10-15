import tempfile
import unittest
from pathlib import Path

from delsys.session_analysis import add_point, export_html, new_session
from delsys.session_compare import compare_sessions, comparison_lines


def ride(rms, *, mapping="Vastus medialis|VM-R", sensor="SID-1", rate=2000.0):
    session = new_session({"patientId": "P1"}, {"3": {"rms_mv": 0.1}})
    session["channels"] = {"3": {"mapping_name": mapping, "sensor_serial": sensor,
                                  "label": "右股內側肌"}}
    session["filter"] = {"highpass_hz": 20, "lowpass_hz": 450, "notch_hz": None}
    session["sample_rate_hz"] = rate
    for index, value in enumerate(rms):
        add_point(session, "emg", {"time_s": index, "slot": 3, "rms_mv": value,
                                    "quality": "有效", "delta_mv": value - 0.1})
    return session


class ComparisonTests(unittest.TestCase):
    def test_two_rides_report_signed_median_and_p90(self):
        first, second = ride([0.1, 0.2, 0.3]), ride([0.2, 0.4, 0.6])
        result = compare_sessions(first, second)
        channel = result["channels"][0]
        self.assertEqual(channel["quality"], "可比較")
        self.assertAlmostEqual(channel["median_difference_mv"], 0.2)
        self.assertAlmostEqual(channel["median_change_percent"], 100)
        self.assertAlmostEqual(channel["first"]["p90_mv"], 0.28)
        self.assertIn("第二次 − 第一次", "\n".join(comparison_lines(result)))
        with tempfile.TemporaryDirectory() as folder:
            report = Path(folder) / "comparison.html"
            export_html(report, second, reference_session=first)
            document = report.read_text(encoding="utf-8")
            self.assertIn("本次肌電分析報表", document)
            self.assertIn("肌電摘要", document)
            self.assertNotIn("兩次 RMS 曲線", document)

    def test_incompatible_mapping_sensor_and_rate_are_explicit(self):
        original = ride([0.2])
        for changed, reason in ((ride([0.3], mapping="Vastus medialis|VM-L"), "肌肉或左右側對應不同"),
                                (ride([0.3], sensor="SID-2"), "感測器 SID 不同"),
                                (ride([0.3], rate=1000), "EMG 取樣率不同或缺失")):
            channel = compare_sessions(original, changed)["channels"][0]
            self.assertEqual(channel["quality"], reason)
            self.assertIsNone(channel["median_difference_mv"])

    def test_bilateral_relative_activation_uses_matching_muscle_sides(self):
        first, second = ride([0.2]), ride([0.3])
        for session, left_value in ((first, 0.1), (second, 0.3)):
            session["channels"]["4"] = {"mapping_name": "Vastus medialis|VM-L",
                                           "sensor_serial": "SID-2", "label": "左股內側肌"}
            add_point(session, "emg", {"time_s": 0, "slot": 4, "rms_mv": left_value,
                                        "quality": "無基準值"})
        result = compare_sessions(first, second)
        pair = result["bilateral"][0]
        self.assertAlmostEqual(pair["first_right_minus_left_percent"], 66.6666667)
        self.assertAlmostEqual(pair["second_right_minus_left_percent"], 0)


if __name__ == "__main__":
    unittest.main()
