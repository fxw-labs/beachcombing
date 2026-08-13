#!/usr/bin/env python3
"""赶海数据库校验脚本。

补点后运行：扫描 db/*.md，校验每个点条目是否符合 templates/point-entry.md 的结构，
并核对 db/INDEX.md 的登记与实际省份文件一致。

用法：
  python3 scripts/validate_db.py          # 校验全部 db/
  python3 scripts/validate_db.py shandong # 只校验指定省

退出码：0 通过；1 有错误；2 有警告。
"""

import os
import re
import sys

DB_DIR = os.path.join(os.path.dirname(__file__), "..", "db")
INDEX_FILE = os.path.join(DB_DIR, "INDEX.md")
ALLOWED_TYPES = {"沙质", "泥质", "礁石", "混合"}

H2 = re.compile(r"^## (.+)$")                                    # 点标题行
COORD = re.compile(r"^[-]?\d{1,2}\.\d+\s*,\s*[-]?\d{1,3}\.\d+$")
KEY_RE = re.compile(r"[-*]\s*\*\*([^\*]+)\*\*：(.+)$")           # "- **键**：值"
TYPE_RE = re.compile(r"[-*]\s*\*\*滩涂类型\*\*：(.+)$")
COORD_RE = re.compile(r"[-*]\s*\*\*坐标\*\*：(.+)$")
LAT_C, LON_C = 28, 120       # 中国大陆海岸参考中心
LAT_R, LON_R = 14, 30        # 允许偏移（度）——北到蓟辽，西到广西


def _normalize_type(raw):
    """从复合描述中提取主类型：'泥质为主'→泥质；'沙质 + 泥质'→沙质；'混合（…）'→混合"""
    s = raw.replace("（", "(").replace("）", ")").replace("·", " ").replace("+", " ").replace("，", " ").replace("为主", " ")
    s = re.sub(r"\([^)]*\)", " ", s)
    for t in ("混合", "沙质", "泥质", "礁石"):
        if t in s:
            return t
    return raw.strip()


def parse_points(text):
    """按 '## ' 切分条目，返回 {title: {coord, type, species_count}}。"""
    points, cur = {}, None
    for line in text.splitlines():
        m = H2.match(line.strip())
        if m:
            cur = m.group(1).strip()
            points[cur] = {"coord": None, "type": None, "species": 0}
            continue
        if cur is None:
            continue
        s = line.strip()
        cm = COORD_RE.match(s)
        tm = TYPE_RE.match(s)
        km = KEY_RE.match(s)
        if cm:
            # 去掉坐标后的说明性后缀（如 "（朱家尖附近）"）
            points[cur]["coord"] = re.sub(r"[（(].*$", "", cm.group(1)).strip()
        if tm:
            points[cur]["type"] = _normalize_type(tm.group(1))
        if km and "目标物种" in km.group(1):
            val = km.group(2)
            # 逗号/顿号分隔的物种计数
            points[cur]["species"] = len([x for x in re.split(r"[，、,]", val) if x.strip()])
    return points


def validate_file(path, key):
    errors, warns = [], []
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    points = parse_points(text)
    if not points:
        warns.append(f"[{key}] 无点条目")
    for title, p in points.items():
        label = f"[{key}] {title}"
        if p["coord"] is None:
            errors.append(f"{label}: 缺坐标")
        else:
            if not COORD.match(p["coord"]):
                errors.append(f"{label}: 坐标格式异常 ({p['coord']})")
            else:
                la, lo = map(float, p["coord"].replace("，", ",").split(","))
                if abs(la - LAT_C) > LAT_R or abs(lo - LON_C) > LON_R:
                    warns.append(f"{label}: 坐标可能在陆/境外 ({la}, {lo})")
        if p["type"] is None:
            errors.append(f"{label}: 缺滩涂类型")
        elif p["type"] not in ALLOWED_TYPES:
            errors.append(f"{label}: 滩涂类型非法 '{p['type']}'（允许 {sorted(ALLOWED_TYPES)}）")
        if p["species"] == 0:
            warns.append(f"{label}: 无目标物种条目")
    return errors, warns


def validate_index():
    """核对 INDEX.md 中登记的省份文件确实存在，且 '点数量' 与实际一致。"""
    errors, warns = [], []
    if not os.path.exists(INDEX_FILE):
        return ["INDEX.md 不存在"], []
    prov_files = []
    idx_map = {}
    with open(INDEX_FILE, "r", encoding="utf-8") as f:
        for line in f:
            m = re.match(r"\|\s*([一-鿿]+)\s*\|?\s*`db/(.+?)\.md`\s*\|\s*(\d+)\s*\|", line)
            if m:
                prov, fn, count = m.group(1), m.group(2), int(m.group(3))
                path = os.path.join(DB_DIR, fn + ".md")
                prov_files.append((prov, path, fn))
                idx_map[fn] = count
    for prov, path, fn in prov_files:
        if not os.path.exists(path):
            errors.append(f"索引登记 {prov} 但文件缺失 db/{fn}.md")
            continue
        with open(path, encoding="utf-8") as f:
            n = len(parse_points(f.read()))
        if idx_map.get(fn) != n:
            warns.append(f"[{prov}] 索引声称 {idx_map[fn]} 点，实际 {n} 点")
    return errors, warns


def main(argv):
    target = argv[1] if len(argv) > 1 else None
    total_err, total_warn = [], []

    if target:
        path = os.path.join(DB_DIR, target + ".md")
        if not os.path.exists(path):
            print(f"错误：db/{target}.md 不存在")
            return 1
        e, w = validate_file(path, target)
        total_err += e
        total_warn += w
    else:
        for fn in sorted(os.listdir(DB_DIR)):
            if fn.endswith(".md") and fn not in ("INDEX.md", "species.md"):
                e, w = validate_file(os.path.join(DB_DIR, fn), fn[:-3])
                total_err += e
                total_warn += w

    e, w = validate_index()
    total_err += e
    total_warn += w

    for x in total_err:
        print(f"ERROR  {x}")
    for x in total_warn:
        print(f"WARN   {x}")
    print(f"\n共 {len(total_err)} 错误，{len(total_warn)} 警告。")
    if total_err:
        return 1
    if total_warn:
        return 2
    print("校验通过 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))