import unittest

from delsys.pedal.analysis import analyze_pedal_records, pedal_analysis_lines, pedal_summary_lines, summarize_pedal_records


class PedalAnalysisTests(unittest.TestCase):
    def test_summary_ignores_missing_values_and_uses_active_balance_samples(self):
        summary = summarize_pedal_records([
            {"timestamp": 10, "power_w": 100, "cadence_rpm": 80, "left_balance_percent": 45, "left_power_w": 45, "right_power_w": 55},
            {"timestamp": 12, "power_w": 200, "cadence_rpm": 100, "left_balance_percent": 55, "left_power_w": 110, "right_power_w": 90},
            {"timestamp": 14, "power_w": 0, "cadence_rpm": None, "left_balance_percent": 50, "left_power_w": 0, "right_power_w": 0},
        ])
        self.assertEqual(summary["sample_count"], 3)
        self.assertEqual(summary["duration_seconds"], 4)
        self.assertEqual(summary["average_power_w"], 100)
        self.assertEqual(summary["maximum_power_w"], 200)
        self.assertEqual(summary["average_cadence_rpm"], 90)
        self.assertEqual(summary["maximum_cadence_rpm"], 100)
        self.assertEqual(summary["average_left_balance_percent"], 50)
        self.assertEqual(summary["average_right_balance_percent"], 50)
        self.assertEqual(summary["average_left_power_w"], 77.5)
        self.assertEqual(summary["average_right_power_w"], 72.5)
        self.assertTrue(any("90.0 rpm" in line for line in pedal_summary_lines(summary)))

    def test_empty_summary_stays_safe(self):
        summary = summarize_pedal_records([])
        self.assertEqual(summary["sample_count"], 0)
        self.assertIsNone(summary["average_cadence_rpm"])
        self.assertIn("--", "\n".join(pedal_summary_lines(summary)))

    def test_sparse_crank_cadence_keeps_recent_active_balance_only(self):
        summary = summarize_pedal_records([
            {"timestamp": 0, "power_w": 100, "cadence_rpm": 80, "left_balance_percent": 45},
            {"timestamp": 2, "power_w": 100, "cadence_rpm": None, "left_balance_percent": 55},
            {"timestamp": 8, "power_w": 100, "cadence_rpm": None, "left_balance_percent": 80},
        ])
        self.assertEqual(summary["average_left_balance_percent"], 50)

    def test_advanced_analysis_creates_time_series_and_training_values(self):
        analysis = analyze_pedal_records([
            {"timestamp": 100, "power_w": 120, "cadence_rpm": 80, "left_balance_percent": 49},
            {"timestamp": 101, "power_w": 180, "cadence_rpm": None, "left_balance_percent": 51},
            {"timestamp": 102, "power_w": 240, "cadence_rpm": 95, "left_balance_percent": 50},
        ], 200)
        self.assertEqual([point["elapsed_s"] for point in analysis["series"]], [0, 1, 2])
        self.assertEqual([point["cadence_rpm"] for point in analysis["series"]], [80, 80, 95])
        self.assertEqual(analysis["power_1s"], [120, 180, 240])
        self.assertIsNone(analysis["normalized_power_w"])
        self.assertIsNone(analysis["tss"])
        self.assertEqual(sum(zone["seconds"] for zone in analysis["zones"]), 3)
        self.assertIn("FTP 設定：200 W", "\n".join(pedal_analysis_lines([], 200)))

    def test_missing_packets_are_not_counted_as_zero_power(self):
        rows = [{"timestamp": second, "power_w": 200} for second in range(35)]
        rows += [{"timestamp": 45, "power_w": 0}]
        result = analyze_pedal_records(rows, 200)
        self.assertEqual(result["missing_seconds"], 10)
        self.assertEqual(result["coasting_seconds"], 1)
        self.assertIsNone(result["power_1s"][40])
        self.assertEqual(result["peak_5s_w"], 200)
        self.assertEqual(result["normalized_power_w"], 200)
        self.assertIsNone(result["tss"])
        self.assertEqual(sum(zone["seconds"] for zone in result["zones"]), 36)

    def test_continuous_power_produces_np_and_tss(self):
        result = analyze_pedal_records(
            [{"timestamp": second, "power_w": 200} for second in range(61)], 200)
        self.assertAlmostEqual(result["normalized_power_w"], 200)
        self.assertAlmostEqual(result["intensity_factor"], 1)
        self.assertIsNotNone(result["tss"])
        self.assertEqual(result["peak_60s_w"], 200)
