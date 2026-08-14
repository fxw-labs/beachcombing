#!/usr/bin/env python3
"""赶海 CLI —— beachcomb 辅助工具。

提供四类子命令供 Claude Code skill 与自动化工具调用：
  tide    <lat> <lon> <date>                  潮汐多级降级：key API -> 免费海洋/潮汐源 -> 农历半日潮骨架
  weather <lat> <lon> <date>                  天气与风浪（Open-Meteo 免费，失败不影响潮汐）
  geocode <地名>                              地理编码（OpenStreetMap Nominatim）
  ical    <date> <low_time> [title] [location] 生成 .ics 日历提醒文件内容

所有输出为 stdin / CLI 友好 JSON（ical 命令直接输出 .ics 文本）。严格用 stdlib，零第三方依赖，离线自动降级。
日期格式：YYYY-MM-DD，时间格式：HH:MM。

环境变量（可选）：
  BEACHCOMB_TIDE_API    潮汐 API key（WorldTides/Storm Glass，层1）
  BEACHCOMB_REQUEST_TO  每个 HTTP 请求超时秒数（缺省 6）
"""

import json
import os
import sys
import math
import datetime as dt
import urllib.parse
import urllib.request
import urllib.error

REQUEST_TIMEOUT = float(os.environ.get("BEACHCOMB_REQUEST_TO", "6"))
TIDE_API_KEY = os.environ.get("BEACHCOMB_TIDE_API", "").strip()


# ---------------------------------------------------------------- 农历与月相计算
def _julian_day(y: int, m: int, d: int) -> float:
    """计算儒略日（Julian Day）。"""
    if m <= 2:
        y -= 1
        m += 12
    a = y // 100
    b = 2 - a + a // 4
    return int(365.25 * (y + 4716)) + int(30.6001 * (m + 1)) + d + b - 1524.5


def _moon_age(jd: float) -> float:
    """计算相对于已知新月（2000-01-06）的月龄（天）。平均朔望月 29.53058867 天。"""
    known_new_moon_jd = 2451550.1
    return (jd - known_new_moon_jd) % 29.53058867


def lunar_phase_info(date: dt.date) -> dict:
    """返回农历日的骨架信息：{lunar_day, phase, spring}。

    大潮判定（朔望大潮）：
    农历初一(朔)、十五/十六(望)及前后各1~2天，引潮力最大，为大潮期。
    """
    jd = _julian_day(date.year, date.month, date.day)
    age = _moon_age(jd)
    lunar_day = int(age) + 1
    if lunar_day > 30:
        lunar_day = 30
    elif lunar_day < 1:
        lunar_day = 1

    # 距离朔(1/30)或望(15)的天数
    dist_shuo = min(abs(lunar_day - 1), abs(30 - lunar_day))
    dist_wang = abs(lunar_day - 15)
    min_dist = min(dist_shuo, dist_wang)

    if lunar_day in (1, 30):
        phase = "朔(新月)"
    elif lunar_day in (15, 16):
        phase = "望(满月)"
    elif lunar_day in (7, 8):
        phase = "上弦月"
    elif lunar_day in (22, 23):
        phase = "下弦月"
    elif min_dist <= 2:
        phase = f"农历{lunar_day} (近{'朔' if dist_shuo <= dist_wang else '望'})"
    else:
        phase = f"农历{lunar_day}"

    # 朔望前后 2 天以内属于典型大潮（初一、初二、初三、十四、十五、十六、十七、廿九、三十）
    spring = min_dist <= 2
    return {"lunar_day": lunar_day, "phase": phase, "spring": spring}


# ---------------------------------------------------------------- 潮汐估算骨架
def _format_time_hhmm(hours_float: float) -> str:
    """将浮点小时（0~24）转换为 HH:MM 格式。"""
    h = int(hours_float) % 24
    m = int(round((hours_float % 1) * 60))
    if m >= 60:
        h = (h + 1) % 24
        m = 0
    return f"{h:02d}:{m:02d}"


def _is_daytime(time_str: str) -> bool:
    """判断 HH:MM 是否处于白天适宜赶海时段（06:00 ~ 19:00）。"""
    try:
        parts = time_str.split(":")
        h = int(parts[0])
        return 6 <= h < 19
    except Exception:
        return False


def _compute_golden_windows(low_waters: list, is_spring: bool) -> list:
    """根据低潮时刻与大/中/小潮动态生成黄金赶海窗口。

    大潮：低潮前后 ±2.0 小时
    中潮：低潮前后 ±1.5 小时
    小潮：低潮前后 ±1.0 小时
    """
    delta_hours = 2.0 if is_spring else 1.5
    windows = []
    for lw in low_waters:
        try:
            parts = lw.split(":")
            h, m = int(parts[0]), int(parts[1])
            lw_float = h + m / 60.0
            start_float = (lw_float - delta_hours) % 24
            end_float = (lw_float + delta_hours) % 24
            windows.append({
                "low_water": lw,
                "window_start": _format_time_hhmm(start_float),
                "window_end": _format_time_hhmm(end_float),
                "duration_hours": round(delta_hours * 2, 1),
                "is_daytime": _is_daytime(lw),
            })
        except Exception:
            continue
    return windows


def estimate_tide(date: dt.date) -> dict:
    """返回农历半日潮天文骨架估算，包含高潮与低潮完整四时刻序列，带 do_not_rely 标记。

    中国沿海多为正规/非正规半日潮，平均周期约 12.42 小时。
    低潮通常位于两次高潮中间（约相隔 6.21 小时）。
    高潮时刻每日较前一日推迟约 50 分钟（0.83 小时）。
    """
    lunar = lunar_phase_info(date)
    base_hr = (6.32 + (lunar["lunar_day"] % 15) * 0.83) % 24

    # 两次高潮
    hw1 = base_hr
    hw2 = (base_hr + 12.42) % 24
    high_waters_raw = sorted([hw1, hw2])
    high_waters = [_format_time_hhmm(t) for t in high_waters_raw]

    # 两次低潮（高潮后约 6.21 小时）
    lw1 = (hw1 + 6.21) % 24
    lw2 = (hw2 + 6.21) % 24
    low_waters_raw = sorted([lw1, lw2])
    low_waters = [_format_time_hhmm(t) for t in low_waters_raw]

    windows = _compute_golden_windows(low_waters, lunar["spring"])

    return {
        "source": "lunar_skeleton",
        "do_not_rely": True,
        "note": "农历天文骨架估算，误差可达 ±1 小时，仅供参考，切勿作为撤退决策的唯一依据，请以当地潮汐实测为准。",
        "lunar": lunar,
        "spring_tide": lunar["spring"],
        "high_waters": high_waters,
        "low_waters": low_waters,
        "golden_windows": windows,
    }


# ---------------------------------------------------------------- 网络请求
def http_get_json(url: str, custom_headers: dict = None) -> dict:
    """发送 HTTP GET 请求并解析返回 JSON，带超时与 User-Agent。"""
    headers = {"User-Agent": "beachcomb-skill/1.0 (Mozilla/5.0; compatible; contact: dev@beachcomb.local)"}
    if custom_headers:
        headers.update(custom_headers)
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        raise RuntimeError(f"net_error: {e}")


# ---------------------------------------------------------------- 天气 (Open-Meteo)
def weather(lat: float, lon: float, date: dt.date) -> dict:
    """调用 Open-Meteo 获取气温、天气现象、极大风速、降水概率。"""
    d = date.strftime("%Y-%m-%d")
    url = (
        "https://api.open-meteo.com/v1/forecast?"
        f"latitude={lat}&longitude={lon}"
        f"&daily=weathercode,temperature_2m_max,temperature_2m_min,windspeed_10m_max,precipitation_probability_max"
        f"&start_date={d}&end_date={d}&timezone=auto"
    )
    try:
        data = http_get_json(url)
        codes = {
            0: "晴朗", 1: "大部晴朗", 2: "多云", 3: "阴天",
            45: "有雾", 48: "沉积雾",
            51: "轻微毛毛雨", 53: "中度毛毛雨", 55: "密集毛毛雨", 56: "轻微冻毛毛雨", 57: "密集冻毛毛雨",
            61: "小雨", 63: "中雨", 65: "大雨", 66: "冻雨", 67: "强冻雨",
            71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
            80: "微弱阵雨", 81: "中度阵雨", 82: "剧烈阵雨",
            85: "小阵雪", 86: "大阵雪",
            95: "雷阵雨", 96: "雷雨伴有轻微冰雹", 99: "雷雨伴有强冰雹"
        }
        code = data["daily"]["weathercode"][0]
        wind_max = data["daily"]["windspeed_10m_max"][0]
        # 风速等级转换 (km/h -> 蒲福风级近似)
        if wind_max < 12:
            beaufort = "微风 (1-2级)"
        elif wind_max < 29:
            beaufort = "和风/清风 (3-4级)"
        elif wind_max < 39:
            beaufort = "劲风 (5级)"
        elif wind_max < 50:
            beaufort = "强风 (6级) ⚠️ 浪大危险"
        else:
            beaufort = "大风/烈风 (7级以上) 🔴 严禁下海"

        precip_prob = data["daily"]["precipitation_probability_max"][0]
        return {
            "source": "open-meteo",
            "date": d,
            "condition": codes.get(code, f"天气代码{code}"),
            "t_max": data["daily"]["temperature_2m_max"][0],
            "t_min": data["daily"]["temperature_2m_min"][0],
            "wind_max_kmh": wind_max,
            "wind_scale": beaufort,
            "wind_warning": wind_max >= 39,
            "precip_prob": precip_prob,
        }
    except Exception as e:
        return {"source": "unavailable", "error": str(e)}


# ---------------------------------------------------------------- 潮汐主入口（多级降级）
def tide(lat: float, lon: float, date: dt.date) -> dict:
    """潮汐多级降级主入口：
    Level 1: 用户提供的 key API (WorldTides / Storm Glass)
    Level 2: 免费海洋海况数据源适配 (Open-Meteo Marine 等)
    Level 3: 农历半日潮天文骨架
    """
    # Level 1: Key API
    if TIDE_API_KEY:
        use = _tide_via_provider(lat, lon, date, TIDE_API_KEY)
        if use:
            return use

    # Level 2: 免费海洋源适配（获取海况增强）
    try:
        free_res = _tide_via_freesource(lat, lon, date)
        if free_res:
            return free_res
    except Exception:
        pass

    # Level 3: 农历骨架估算
    return estimate_tide(date)


def _tide_via_provider(lat: float, lon: float, date: dt.date, key: str) -> dict | None:
    """根据 key 前缀请求商业潮汐接口。"""
    d = date.strftime("%Y-%m-%d")
    key_id = key.split(".")[0].lower() if key else ""
    adapters = {
        "worldtides": lambda: http_get_json(
            f"https://www.worldtides.info/api/v3/extremes?lat={lat}&lon={lon}"
            f"&start={d}T00:00:00Z&length=172800&key={urllib.parse.quote(key)}"
        ),
    }
    adapter = adapters.get(key_id) or (lambda: _generic_extremes(lat, lon, d, key))
    try:
        raw = adapter()
        return _normalize_extremes(raw, date)
    except Exception:
        return None


def _generic_extremes(lat: float, lon: float, d: str, key: str):
    raise NotImplementedError("generic key provider not configured")


def _normalize_extremes(raw: dict, target_date: dt.date) -> dict | None:
    """将商业 API 返回归一化。"""
    ex = raw.get("extremes", []) if isinstance(raw, dict) else []
    highs, lows = [], []
    for e in ex:
        t = e.get("type", "").lower()
        date_str = e.get("date", "")
        # 仅取对应日期的潮时
        time_part = date_str[11:16] if len(date_str) >= 16 else date_str
        if "high" in t:
            highs.append(time_part)
        elif "low" in t:
            lows.append(time_part)
    if not highs and not lows:
        return None

    lunar = lunar_phase_info(target_date)
    windows = _compute_golden_windows(lows, lunar["spring"])

    return {
        "source": "provider_api",
        "do_not_rely": False,
        "lunar": lunar,
        "spring_tide": lunar["spring"],
        "high_waters": highs,
        "low_waters": lows,
        "golden_windows": windows,
    }


def _tide_via_freesource(lat: float, lon: float, date: dt.date) -> dict | None:
    """查询 Open-Meteo Marine 开放海洋数据，若成功则结合天文骨架输出高质量混合预报。"""
    d = date.strftime("%Y-%m-%d")
    url = (
        "https://marine-api.open-meteo.com/v1/marine?"
        f"latitude={lat}&longitude={lon}&daily=wave_height_max,wave_period_max"
        f"&start_date={d}&end_date={d}&timezone=auto"
    )
    try:
        marine_data = http_get_json(url)
        wave_max = marine_data["daily"]["wave_height_max"][0]
        wave_period = marine_data["daily"]["wave_period_max"][0]
        # 获取骨架并融合海洋波浪特征
        skeleton = estimate_tide(date)
        skeleton["marine"] = {
            "source": "open-meteo-marine",
            "wave_height_max_m": wave_max,
            "wave_period_max_s": wave_period,
            "wave_warning": wave_max >= 1.5 if wave_max is not None else False
        }
        return skeleton
    except Exception:
        return None


# ---------------------------------------------------------------- 地理编码 (Nominatim)
def geocode(name: str) -> dict | None:
    """使用 OpenStreetMap Nominatim 进行地名地理编码，支持中国沿海地名解析。"""
    q = urllib.parse.quote(name)
    url = f"https://nominatim.openstreetmap.org/search?q={q}&format=json&limit=1&accept-language=zh"
    try:
        res = http_get_json(url)
        if res and len(res) > 0:
            r = res[0]
            return {
                "name": r.get("display_name"),
                "lat": round(float(r["lat"]), 4),
                "lon": round(float(r["lon"]), 4)
            }
        return {"error": f"未找到地点 '{name}' 的坐标，请尝试输入更具体的沿海区县或标志性景点。"}
    except Exception as e:
        return {"error": f"地理编码服务暂时不可用 ({e})"}


# ---------------------------------------------------------------- 适用性评分
def suitability_score(lunar: dict, low_waters: list = None, is_real_data: bool = False) -> tuple[int, str]:
    """计算赶海适用性评分 (1~5 星) 及理由。

    安全原则：
    1. 若为估测数据（is_real_data=False），评分上限严格压制在 ≤ 2 星。
    2. 若为实测数据：
       - 大潮 + 白天低潮：5 星（绝佳）
       - 大潮 + 夜间低潮：3 星（潮位虽低但夜间赶海视野受限）
       - 小潮 + 白天低潮：3 星（白天好走但退潮露滩有限）
       - 小潮 + 夜间低潮：1~2 星（不推荐）
    """
    is_spring = lunar.get("spring", False)
    phase = lunar.get("phase", "")

    # 判断是否有白天低潮
    has_daytime_low = False
    if low_waters:
        has_daytime_low = any(_is_daytime(lw) for lw in low_waters)

    if not is_real_data:
        # 估测降级：最高 ≤ 2 星
        if is_spring:
            return 2, f"{phase}临近大潮期，但当前为天文骨架估测，请以实测为准 (封顶2★)"
        else:
            return 1, f"{phase}潮差较小且为估测数据，推荐择日或结合本地潮汐实测出发 (1★)"

    # 实测数据评分
    if is_spring and has_daytime_low:
        return 5, f"{phase}大潮期且低潮在白天黄金时段，退潮广、露滩深，绝佳赶海日！"
    elif is_spring and not has_daytime_low:
        return 3, f"{phase}大潮退潮充足，但最低潮位于夜间，夜赶需备强光手电并结伴，注意安全。"
    elif not is_spring and has_daytime_low:
        return 3, f"{phase}小潮/中潮期，白天可正常赶海，但露滩面积有限，适合浅滩挖蛤。"
    else:
        return 2, f"{phase}小潮期且低潮偏夜间，赶海窗口窄、收获预期较低。"


# ---------------------------------------------------------------- iCal 生成
def ical(date: dt.date, event_time: str, title: str = "赶海 — 退潮黄金时段", location: str = "", duration_hours: float = 2.0) -> str:
    """生成标准 iCalendar (.ics) 文件文本。"""
    try:
        parts = event_time.split(":")
        h, m = int(parts[0]), int(parts[1])
        start = dt.datetime.combine(date, dt.time(h, m))
    except Exception:
        start = dt.datetime.combine(date, dt.time(8, 0))

    end = start + dt.timedelta(hours=duration_hours)
    fmt = lambda t: t.strftime("%Y%m%dT%H%M%S")
    now_str = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    uid = f"beachcomb-{int(start.timestamp())}@beachcombing.skill"

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Beachcombing Skill//CN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{now_str}",
        f"DTSTART:{fmt(start)}",
        f"DTEND:{fmt(end)}",
        f"SUMMARY:{title}",
        f"LOCATION:{location}" if location else "LOCATION:海滩/礁石区",
        "DESCRIPTION:赶海黄金窗口：请在涨潮前（高潮时刻前 1~2 小时）务必安全撤离滩涂！",
        "STATUS:CONFIRMED",
        "BEGIN:VALARM",
        "TRIGGER:-PT30M",
        "DESCRIPTION:赶海出发与安全提醒",
        "ACTION:DISPLAY",
        "END:VALARM",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------- CLI 入口
def _print(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def print_help():
    help_text = """赶海 CLI (beachcomb) 使用说明:
  python3 beachcomb.py tide <lat> <lon> <date>
      查询指定经纬度和日期的潮汐信息（高低潮时刻、大潮状态、黄金窗口、适用性评分）
      示例: python3 bin/beachcomb.py tide 36.09 120.47 2026-08-15

  python3 beachcomb.py weather <lat> <lon> <date>
      查询指定地点的天气、风速等级及风浪预警
      示例: python3 bin/beachcomb.py weather 36.09 120.47 2026-08-15

  python3 beachcomb.py geocode <地名>
      解析中文沿海地名经纬度
      示例: python3 bin/beachcomb.py geocode 青岛石老人

  python3 beachcomb.py ical <date> <low_time> [title] [location]
      生成赶海黄金时段的 iCalendar (.ics) 日历事件
      示例: python3 bin/beachcomb.py ical 2026-08-15 14:30 "青岛石老人赶海" "石老人海滩"
"""
    print(help_text)


def main(argv):
    if len(argv) < 2 or argv[1] in ("-h", "--help", "help"):
        print_help()
        return 0

    cmd = argv[1]
    try:
        if cmd == "tide" and len(argv) >= 5:
            lat = float(argv[2])
            lon = float(argv[3])
            d = dt.date.fromisoformat(argv[4])
            res = tide(lat, lon, d)
            is_real = not res.get("do_not_rely", True)
            score, reason = suitability_score(res.get("lunar", {}), res.get("low_waters", []), is_real)
            res["suitability"] = {
                "score": score,
                "stars": "★" * score + "☆" * (5 - score),
                "reason": reason
            }
            _print(res)
            return 0

        elif cmd == "weather" and len(argv) >= 5:
            lat = float(argv[2])
            lon = float(argv[3])
            d = dt.date.fromisoformat(argv[4])
            _print(weather(lat, lon, d))
            return 0

        elif cmd == "geocode" and len(argv) >= 3:
            name = " ".join(argv[2:])
            _print(geocode(name))
            return 0

        elif cmd == "ical" and len(argv) >= 4:
            d = dt.date.fromisoformat(argv[2])
            low_time = argv[3]
            title = argv[4] if len(argv) > 4 else "赶海 — 退潮黄金时段"
            location = argv[5] if len(argv) > 5 else ""
            ics_content = ical(d, low_time, title, location)
            print(ics_content)
            return 0

        else:
            _print({"error": f"参数错误。请使用 'python3 bin/beachcomb.py --help' 查看用法。"})
            return 1

    except ValueError as e:
        _print({"error": f"输入格式错误: {e}"})
        return 1
    except Exception as e:
        _print({"error": f"执行异常: {e}"})
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))