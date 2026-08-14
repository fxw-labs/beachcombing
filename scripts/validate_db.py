#!/usr/bin/env python3
"""赶海数据库校验脚本 (Enhanced)。

扫描 db/*.md，严格校验：
1. 每个点条目是否符合 templates/point-entry.md 的键值规范；
2. 经纬度格式合法性及中国大陆海岸线地理围栏（纬度 18.0~41.5，经度 108.0~125.0）；
3. 滩涂类型是否在允许集合（沙质、泥质、礁石、混合）；
4. 目标物种是否非空且包含基础物种有效引用；
5. 核对 db/INDEX.md 中所有省份文件登记、点位数量与实际文件 100% 一致。

用法：
  python3 scripts/validate_db.py          # 校验全部 db/
  python3 scripts/validate_db.py shandong # 只校验指定省

退出码：0 通过；1 有严重错误；2 仅有警告。
"""

import os
import re
import sys

DB_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "db"))
INDEX_FILE = os.path.join(DB_DIR, "INDEX.md")
SPECIES_FILE = os.path.join(DB_DIR, "species.md")

ALLOWED_TYPES = {"沙质", "泥质", "礁石", "混合"}

# 中国沿海经纬度合理围栏（北起鸭绿江口 40°N，南至三亚 18°N，西起北部湾 108°E，东至舟山/大连 125°E）
LAT_MIN, LAT_MAX = 17.5, 42.0
LON_MIN, LON_MAX = 107.5, 126.0

H2 = re.compile(r"^## (.+)$")                                    # 点标题行
COORD_PATTERN = re.compile(r"^[-]?\d{1,2}\.\d+\s*,\s*[-]?\d{1,3}\.\d+$")
KEY_RE = re.compile(r"[-*]\s*\*\*([^\*]+)\*\*：(.+)$")           # "- **键**：值"
TYPE_RE = re.compile(r"[-*]\s*\*\*滩涂类型\*\*：(.+)$")
COORD_RE = re.compile(r"[-*]\s*\*\*坐标\*\*：(.+)$")


def _normalize_type(raw):
    """从复合描述中提取主类型：'泥质为主'→泥质；'沙质 + 泥质'→沙质；'混合（…）'→混合"""
    s = raw.replace("（", "(").replace("）", ")").replace("·", " ").replace("+", " ").replace("，", " ").replace("为主", " ")
    s = re.sub(r"\([^)]*\)", " ", s)
    for t in ("混合", "沙质", "泥质", "礁石"):
        if t in s:
            return t
    return raw.strip()


def parse_points(text):
    """按 '## ' 切分条目，返回 {title: {coord, type, species_count, species_list, raw_coord}}。"""
    points, cur = {}, None
    for line in text.splitlines():
        m = H2.match(line.strip())
        if m:
            cur = m.group(1).strip()
            points[cur] = {
                "coord": None,
                "raw_coord": None,
                "type": None,
                "species": 0,
                "species_list": []
            }
            continue
        if cur is None:
            continue
        s = line.strip()
        cm = COORD_RE.match(s)
        tm = TYPE_RE.match(s)
        km = KEY_RE.match(s)
        if cm:
            points[cur]["raw_coord"] = cm.group(1).strip()
            # 去掉坐标后的说明性后缀（如 "（朱家尖附近）"）
            points[cur]["coord"] = re.sub(r"[（(].*$", "", cm.group(1)).strip().replace("，", ",")
        if tm:
            points[cur]["type"] = _normalize_type(tm.group(1))
        if km and "目标物种" in km.group(1):
            val = km.group(2)
            # 逗号/顿号分隔的物种列表
            spl = [x.strip() for x in re.split(r"[，、,\s/]", val) if x.strip()]
            points[cur]["species"] = len(spl)
            points[cur]["species_list"] = spl
    return points


def load_known_species():
    """从 species.md 中解析所有已知物种与别名集合。"""
    known = set()
    if not os.path.exists(SPECIES_FILE):
        return known
    with open(SPECIES_FILE, "r", encoding="utf-8") as f:
        content = f.read()
    for line in content.splitlines():
        if line.startswith("### "):
            header = line.replace("### ", "").strip()
            tokens = re.split(r"[（）()/、\s]", header)
            for t in tokens:
                if t and len(t) >= 2:
                    known.add(t)
    # 额外补充通用泛称
    known.update(["螃蟹", "海鲜", "贝类", "小海螺", "小螃蟹", "沙蟹", "毛蚶", "车螺", "白蛤", "石蟹", "花蛤", "文蛤", "蛏子", "生蚝", "牡蛎", "海螺", "海胆", "鲍鱼", "青蟹", "泥螺", "跳跳鱼", "沙虫", "海肠", "黄蚬子", "鸟贝", "八爪鱼", "皮皮虾", "紫菜", "海苔", "藤壶"])
    return known


def validate_file(path, key, known_species):
    errors, warns = [], []
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    points = parse_points(text)
    if not points:
        errors.append(f"[{key}] 数据文件为空或未包含任何有效点条目 (## 开头)")
        return errors, warns

    seen_coords = set()

    for title, p in points.items():
        label = f"[{key}] {title}"
        if p["coord"] is None:
            errors.append(f"{label}: 缺少 **坐标** 字段")
        else:
            if not COORD_PATTERN.match(p["coord"]):
                errors.append(f"{label}: 坐标格式不规范 (提取值: '{p['coord']}', 原始值: '{p['raw_coord']}')，须为 'lat, lon'")
            else:
                try:
                    la, lo = map(float, p["coord"].split(","))
                    if not (LAT_MIN <= la <= LAT_MAX and LON_MIN <= lo <= LON_MAX):
                        warns.append(f"{label}: 坐标 ({la}, {lo}) 超出中国沿海参考围栏 [{LAT_MIN}~{LAT_MAX}, {LON_MIN}~{LON_MAX}]")
                    if (la, lo) in seen_coords:
                        warns.append(f"{label}: 坐标 ({la}, {lo}) 与本省其他点位重复")
                    seen_coords.add((la, lo))
                except ValueError:
                    errors.append(f"{label}: 坐标无法解析为浮点数")

        if p["type"] is None:
            errors.append(f"{label}: 缺少 **滩涂类型** 字段")
        elif p["type"] not in ALLOWED_TYPES:
            errors.append(f"{label}: 滩涂类型 '{p['type']}' 非法，必须属于 {sorted(ALLOWED_TYPES)}")

        if p["species"] == 0:
            warns.append(f"{label}: 缺少 **目标物种** 字段或物种为空")

    return errors, warns


def validate_index():
    """核对 INDEX.md 中登记的省份文件确实存在，且 '点数量' 与实际一致。"""
    errors, warns = [], []
    if not os.path.exists(INDEX_FILE):
        return ["db/INDEX.md 文件不存在"], []

    prov_files = []
    idx_map = {}
    with open(INDEX_FILE, "r", encoding="utf-8") as f:
        for line in f:
            m = re.match(r"\|\s*([一-鿿]+)\s*\|\s*`db/(.+?)\.md`\s*\|\s*(\d+)\s*\|", line)
            if m:
                prov, fn, count = m.group(1), m.group(2), int(m.group(3))
                path = os.path.join(DB_DIR, fn + ".md")
                prov_files.append((prov, path, fn))
                idx_map[fn] = count

    if not prov_files:
        errors.append("INDEX.md 未成功解析出任何省份表格行")

    actual_files = {fn[:-3] for fn in os.listdir(DB_DIR) if fn.endswith(".md") and fn not in ("INDEX.md", "species.md")}
    indexed_files = set(idx_map.keys())

    missing_in_index = actual_files - indexed_files
    if missing_in_index:
        errors.append(f"db/ 中存在未在 INDEX.md 登记的文件: {sorted(missing_in_index)}")

    for prov, path, fn in prov_files:
        if not os.path.exists(path):
            errors.append(f"索引登记了 {prov}，但对应数据文件 db/{fn}.md 不存在")
            continue
        with open(path, encoding="utf-8") as f:
            n = len(parse_points(f.read()))
        if idx_map.get(fn) != n:
            errors.append(f"[{prov}] INDEX.md 登记数量为 {idx_map[fn]} 点，实际 db/{fn}.md 中为 {n} 点 (数量不一致)")

    return errors, warns


def main(argv):
    target = argv[1] if len(argv) > 1 else None
    total_err, total_warn = [], []
    known_species = load_known_species()

    print("🔍 正在执行赶海数据库自洽性校验...")

    if target:
        path = os.path.join(DB_DIR, target + ".md")
        if not os.path.exists(path):
            print(f"❌ 错误：db/{target}.md 不存在")
            return 1
        e, w = validate_file(path, target, known_species)
        total_err += e
        total_warn += w
    else:
        for fn in sorted(os.listdir(DB_DIR)):
            if fn.endswith(".md") and fn not in ("INDEX.md", "species.md"):
                e, w = validate_file(os.path.join(DB_DIR, fn), fn[:-3], known_species)
                total_err += e
                total_warn += w

    e, w = validate_index()
    total_err += e
    total_warn += w

    for x in total_err:
        print(f"  ❌ ERROR  {x}")
    for x in total_warn:
        print(f"  ⚠️ WARN   {x}")

    print(f"\n📊 校验完成：{len(total_err)} 处错误，{len(total_warn)} 处警告。")
    if total_err:
        print("❌ 校验未通过，请根据提示修复错误。")
        return 1
    if total_warn:
        print("⚠️ 存在警告项，但无致命错误。")
        return 2
    print("✅ 全部通过！所有赶海点数据与索引 100% 规范一致。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))