#!/usr/bin/env python3
"""单元测试套件 —— 测试 bin/beachcomb.py 核心算法、降级链路、评分体系及 CLI 命令。

运行方式：
  python3 -m unittest discover tests
  或者
  python3 tests/test_beachcomb.py
"""

import datetime as dt
import io
import json
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

# 将项目根目录加入 sys.path
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
BIN_DIR = os.path.join(BASE_DIR, "bin")
if BIN_DIR not in sys.path:
    sys.path.insert(0, BIN_DIR)

import beachcomb


class TestAstronomicalCalculations(unittest.TestCase):
    """测试天文日历与农历月相推算。"""

    def test_julian_day_calculation(self):
        # 2000-01-01 12:00 -> JD 2451545.0 (标准天文基准)
        jd = beachcomb._julian_day(2000, 1, 1)
        self.assertAlmostEqual(jd, 2451544.5, delta=1.0)

    def test_lunar_phase_new_moon(self):
        # 2026-08-13 为农历七月初一 (朔日)
        d = dt.date(2026, 8, 13)
        info = beachcomb.lunar_phase_info(d)
        self.assertIn("lunar_day", info)
        self.assertTrue(1 <= info["lunar_day"] <= 30)
        self.assertTrue(info["spring"])  # 朔日为大潮期

    def test_lunar_phase_full_moon(self):
        # 农历十五/十六附近
        d = dt.date(2026, 8, 27)  # 约十五
        info = beachcomb.lunar_phase_info(d)
        self.assertTrue(info["spring"])


class TestTideEstimation(unittest.TestCase):
    """测试农历半日潮四时刻推算及黄金窗口生成。"""

    def test_estimate_tide_structure(self):
        d = dt.date(2026, 8, 15)
        res = beachcomb.estimate_tide(d)

        self.assertEqual(res["source"], "lunar_skeleton")
        self.assertTrue(res["do_not_rely"])
        self.assertIn("high_waters", res)
        self.assertIn("low_waters", res)
        self.assertIn("golden_windows", res)

        # 必须同时输出 2 个高潮与 2 个低潮时刻
        self.assertEqual(len(res["high_waters"]), 2)
        self.assertEqual(len(res["low_waters"]), 2)

        # 时间格式必须为 HH:MM
        for hw in res["high_waters"]:
            self.assertRegex(hw, r"^\d{2}:\d{2}$")
        for lw in res["low_waters"]:
            self.assertRegex(lw, r"^\d{2}:\d{2}$")

    def test_golden_windows_calculation(self):
        windows = beachcomb._compute_golden_windows(["02:30", "14:30"], is_spring=True)
        self.assertEqual(len(windows), 2)
        self.assertEqual(windows[0]["low_water"], "02:30")
        self.assertFalse(windows[0]["is_daytime"])
        self.assertEqual(windows[1]["low_water"], "14:30")
        self.assertTrue(windows[1]["is_daytime"])
        self.assertEqual(windows[1]["duration_hours"], 4.0)


class TestSuitabilityScore(unittest.TestCase):
    """测试赶海适用性评分规则。"""

    def test_estimated_source_capped_at_two_stars(self):
        """核心安全规则：估测源最高不得超过 2 星。"""
        spring_lunar = {"spring": True, "phase": "农历初一 (朔)"}
        score, reason = beachcomb.suitability_score(spring_lunar, ["14:00"], is_real_data=False)
        self.assertLessEqual(score, 2)
        self.assertIn("封顶2★", reason)

        neap_lunar = {"spring": False, "phase": "农历初八"}
        score, reason = beachcomb.suitability_score(neap_lunar, ["14:00"], is_real_data=False)
        self.assertLessEqual(score, 2)

    def test_real_data_daytime_spring_gives_five_stars(self):
        """实测源 + 大潮 + 白天低潮 -> 5 星。"""
        spring_lunar = {"spring": True, "phase": "农历十五 (望)"}
        score, reason = beachcomb.suitability_score(spring_lunar, ["14:00"], is_real_data=True)
        self.assertEqual(score, 5)
        self.assertIn("绝佳", reason)

    def test_real_data_night_spring_gives_three_stars(self):
        """实测源 + 大潮 + 仅夜间低潮 -> 3 星。"""
        spring_lunar = {"spring": True, "phase": "农历十五 (望)"}
        score, reason = beachcomb.suitability_score(spring_lunar, ["02:00"], is_real_data=True)
        self.assertEqual(score, 3)


class TestICalGeneration(unittest.TestCase):
    """测试 .ics 日历内容生成。"""

    def test_ical_format(self):
        d = dt.date(2026, 8, 15)
        res = beachcomb.ical(d, "14:30", "青岛石老人赶海", "石老人海滩")

        self.assertIn("BEGIN:VCALENDAR", res)
        self.assertIn("END:VCALENDAR", res)
        self.assertIn("SUMMARY:青岛石老人赶海", res)
        self.assertIn("LOCATION:石老人海滩", res)
        self.assertIn("DTSTART:20260815T143000", res)
        self.assertIn("DTEND:20260815T163000", res)
        self.assertIn("BEGIN:VALARM", res)


class TestCLIExecution(unittest.TestCase):
    """测试命令行子命令调用。"""

    def test_cli_help(self):
        captured_output = io.StringIO()
        with patch("sys.stdout", captured_output):
            code = beachcomb.main(["beachcomb.py", "--help"])
        self.assertEqual(code, 0)
        self.assertIn("赶海 CLI", captured_output.getvalue())

    def test_cli_tide_command(self):
        captured_output = io.StringIO()
        with patch("sys.stdout", captured_output):
            code = beachcomb.main(["beachcomb.py", "tide", "36.09", "120.47", "2026-08-15"])
        self.assertEqual(code, 0)
        out = json.loads(captured_output.getvalue())
        self.assertIn("high_waters", out)
        self.assertIn("low_waters", out)
        self.assertIn("suitability", out)
        self.assertIn("stars", out["suitability"])

    def test_cli_ical_command(self):
        captured_output = io.StringIO()
        with patch("sys.stdout", captured_output):
            code = beachcomb.main(["beachcomb.py", "ical", "2026-08-15", "14:30", "测试赶海", "大连金石滩"])
        self.assertEqual(code, 0)
        self.assertIn("BEGIN:VCALENDAR", captured_output.getvalue())
        self.assertIn("大连金石滩", captured_output.getvalue())

    def test_cli_bad_arguments(self):
        captured_output = io.StringIO()
        with patch("sys.stdout", captured_output):
            code = beachcomb.main(["beachcomb.py", "tide", "invalid_lat"])
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
