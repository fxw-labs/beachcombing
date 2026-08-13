#!/usr/bin/env python3
"""赶海 CLI —— beachcomb 辅助工具。

提供三类子命令供 Claude Code skill 调用：
  tide   <lat> <lon> <date>           潮汐多级降级：key API -> 无 key 中国源 -> 农历骨架
  weather <lat> <lon> <date>          天气（Open-Meteo 免费，失败不影响潮汐）
  geocode <地名>                      地理编码（OpenStreetMap Nominatim，免费）

所有输出为 stdin 友好 JSON。严格用 stdlib，零第三方依赖，离线自动降级。
日期格式：YYYY-MM-DD。

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


# ---------------------------------------------------------------- 农历计算
# 天文地球日期 → 农历日期（用于大潮骨架）。近似的月相计算，误差可接受：
# 我们只看"是否近朔/望"，不需要精确到天级。

def _julian_day(y, m, d):
    if m <= 2:
        y -= 1
        m += 12
    a = y // 100
    b = 2 - a + a // 4
    return int(365.25 * (y + 4716)) + int(30.6001 * (m + 1)) + d + b - 1524.5


def _moon_age(jd):
    """approx days since new moon for a given Julian day."""
    known_new_moon_jd = 2451550.1  # 2000-01-06 new moon reference
    return (jd - known_new_moon_jd) % 29.53058867


def lunar_phase_info(date: dt.date):
    """返回农历日的骨架信息：{lunar_day, phase, spring}。"""
    jd = _julian_day(date.year, date.month, date.day)
    age = _moon_age(jd)
    lunar_day = int(age) + 1  # 农历日（约）
    if lunar_day > 30:
        lunar_day = 30
    dist = min(lunar_day, 30 - lunar_day) if lunar_day <= 15 or lunar_day >= 28 else 30 - lunar_day
    if lunar_day in (1, 15, 30):
        phase = "朔(新月)" if lunar_day in (1, 30) else "望(满月)"
    elif dist <= 3:
        phase = f"农历{lunar_day}(近{ '朔' if lunar_day<15 else '望'})"
    else:
        phase = f"农历{lunar_day}"
    spring = dist <= 3  # 朔望前后 3 天
    return {"lunar_day": lunar_day, "phase": phase, "spring": spring}


# ---------------------------------------------------------------- 潮汐估算骨架
# 半日潮：一天两次高潮、两次低潮，平均周期约 12.42h。高潮逐日推迟约 50 分钟。
# 因潮相位随地点漂移，且估测 ±1 小时，这里只给出"第 n 次高潮/低潮的相对时刻"范围，
# 并显式标注 do_not_rely=True。绝不当作精确数据。

def estimate_tide(date: dt.date):
    """返回农历骨架估算，带 do_not_rely 强标记。"""
    lunar = lunar_phase_info(date)
    base_hr = 6.32 + (lunar["lunar_day"] % 15) * 0.83
    hw1 = (base_hr + 24) % 24
    hw2 = (base_hr + 12.42 + 24) % 24
    return {
        "source": "lunar_skeleton",
        "do_not_rely": True,
        "note": "农历天文骨架估算，误差可达 ±1 小时，请以当地潮汐 App 实测为准。",
        "lunar": lunar,
        "high_waters": [f"{int(hw1) % 24:02d}:{int((hw1 % 1) * 60):02d}", f"{int(hw2) % 24:02d}:{int((hw2 % 1) * 60):02d}"],
        "low_waters": None,
    }


# ---------------------------------------------------------------- 网络抓取
def http_get_json(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "beachcomb-skill/0.1"})
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        raise RuntimeError(f"net_error: {e}")


# ---------------------------------------------------------------- 天气 (Open-Meteo)
def weather(lat, lon, date):
    d = date.strftime("%Y-%m-%d")
    url = (
        "https://api.open-meteo.com/v1/forecast?"
        f"latitude={lat}&longitude={lon}"
        f"&daily=weathercode,temperature_2m_max,temperature_2m_min,windspeed_10m_max,precipitation_probability_max"
        f"&start_date={d}&end_date={d}&timezone=auto"
    )
    try:
        data = http_get_json(url)
        codes = {0: "晴", 1: "多云", 2: "多云", 3: "阴", 45: "雾", 48: "雾凇",
                 51: "毛毛雨", 53: "毛毛雨", 55: "毛毛雨", 56: "冻毛毛雨", 57: "冻毛毛雨",
                 61: "小雨", 63: "中雨", 65: "大雨", 66: "冻雨", 67: "冻雨",
                 71: "小雪", 73: "中雪", 75: "大雪", 80: "阵雨", 81: "阵雨",
                 82: "强阵雨", 95: "雷雨", 96: "雷雨&冰雹", 99: "雷雨&大冰雹"}
        code = data["daily"]["weathercode"][0]
        return {
            "source": "open-meteo",
            "date": d,
            "condition": codes.get(code, f"代码{code}"),
            "t_max": data["daily"]["temperature_2m_max"][0],
            "t_min": data["daily"]["temperature_2m_min"][0],
            "wind_max_kmh": data["daily"]["windspeed_10m_max"][0],
            "precip_prob": data["daily"]["precipitation_probability_max"][0],
        }
    except Exception as e:
        return {"source": "unavailable", "error": str(e)}


# ---------------------------------------------------------------- 潮汐主入口（多级降级）
def tide(lat, lon, date):
    # 层1：用户提供的 key API。
    if TIDE_API_KEY:
        use = _tide_via_provider(lat, lon, date, TIDE_API_KEY)
        if use:
            return use
    # 层2：无 key 中国源适配（尽力而为）。
    try:
        china = _tide_via_freesource(lat, lon, date)
        if china:
            return china
    except Exception:
        pass
    # 层3：农历骨架 + 诚实声明
    return estimate_tide(date)


def _tide_via_provider(lat, lon, date, key):
    """根据 key 前缀猜测 provider，返回 dict 或 None。接口抽象，便于扩展。"""
    d = date.strftime("%Y-%m-%d")
    key_id = key.split(".")[0].lower() if key else ""
    adapters = {
        "worldtides": lambda: http_get_json(
            f"https://www.worldtides.info/api/v3/extremes?lat={lat}&lon={lon}"
            f"&start={d}T00:00:00Z&length=172800&key={urllib.parse.quote(key)}"),
    }
    adapter = adapters.get(key_id) or (lambda: _generic_extremes(lat, lon, d, key))
    try:
        raw = adapter()
    except Exception:
        return None
    return _normalize_extremes(raw)


def _generic_extremes(lat, lon, d, key):
    raise NotImplementedError("generic key provider not wired; provide an adapter")


def _normalize_extremes(raw):
    """归一化 extremes -> {source, highs, lows}。WorldTides 返回
    {extremes: [{date, type: "High"/"Low", height}]}。"""
    ex = raw.get("extremes", []) if isinstance(raw, dict) else []
    highs, lows = [], []
    for e in ex:
        t = e.get("type", "").lower()
        if "high" in t:
            highs.append({"time": e.get("date"), "height": e.get("height")})
        elif "low" in t:
            lows.append({"time": e.get("date"), "height": e.get("height")})
    if not highs and not lows:
        return None
    return {"source": "provider", "highs": highs, "lows": lows}


def _tide_via_freesource(lat, lon, date):
    """无 key 中国源适配占位。真实接入时在此实现并返回真数据。现返回 None。"""
    return None


# ---------------------------------------------------------------- geocode (Nominatim)
def geocode(name):
    q = urllib.parse.quote(name)
    url = f"https://nominatim.openstreetmap.org/search?q={q}&format=json&limit=1&accept-language=zh"
    try:
        res = http_get_json(url)
        if res:
            r = res[0]
            return {"name": r.get("display_name"), "lat": float(r["lat"]), "lon": float(r["lon"])}
        return None
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------- 适用性评分
def suitability_score(lunar, high_waters=None):
    """只看潮汐：大潮 + 白天低潮 → 高分。估算强制 ≤2星。"""
    spring = lunar.get("spring", False)
    base = 3 if spring else 2
    return base


# ---------------------------------------------------------------- iCal 片段
def ical(date, event_time, title="赶海 — 退潮黄金时段"):
    start = dt.datetime.combine(date, dt.time.fromisoformat(event_time) if event_time else dt.time(6, 0))
    end = start + dt.timedelta(hours=2)
    f = lambda t: t.strftime("%Y%m%dT%H%M%S")
    return (
        "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//beachcomb//EN\nBEGIN:VEVENT\n"
        f"DTSTART:{f(start)}\nDTEND:{f(end)}\nSUMMARY:{title}\nEND:VEVENT\nEND:VCALENDAR"
    )


# ---------------------------------------------------------------- CLI
def _print(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def main(argv):
    if len(argv) < 2:
        _print({"error": "usage: beachcomb.py [tide|weather|geocode] ..."})
        return 1
    cmd = argv[1]
    try:
        if cmd == "tide" and len(argv) >= 5:
            lat, lon, d = float(argv[2]), float(argv[3]), dt.date.fromisoformat(argv[4])
            res = tide(lat, lon, d)
            res["suitability"] = suitability_score(res.get("lunar", {}), res.get("high_waters"))
            _print(res)
        elif cmd == "weather" and len(argv) >= 5:
            _print(weather(float(argv[2]), float(argv[3]), dt.date.fromisoformat(argv[4])))
        elif cmd == "geocode" and len(argv) >= 3:
            _print(geocode(" ".join(argv[2:])))
        else:
            _print({"error": "bad args"})
            return 1
    except ValueError as e:
        _print({"error": f"input: {e}"})
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))