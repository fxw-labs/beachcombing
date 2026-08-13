# 赶海 (beachcombing) — 潮汐与赶海攻略 skill

面向中国大陆的赶海查询能力：解析"地点 + 时间 + 意向"，输出可执行的赶海计划——退潮黄金窗口、大/小潮、适用性评分、目标物种、出发清单，以及最重要的 **安全撤退截止时刻**。

## 这是什么

一个 Claude Code skill（`SKILL.md` 为入口）+ 辅助数据与脚本。核心原则：

- **安全线最高优先级**：撤退时刻永远置顶；大潮给红色警告；估测数据绝不伪装精确。
- **诚实降级**：潮汐数据 `key API → 无 key 中国源 → 农历骨架`，兜底层醒目标注并压制评分。
- **老饕语气**：报告有温度、有时令、有吃法；但安全信息严肃无歧义。
- **评分只看潮汐**：天气展示但不计入评分。

## 目录结构

```
beachcombing/
├── SKILL.md                 # 入口：触发词、工作流、边界、降级链路、Non-Goals
├── bin/
│   └── beachcomb.py         # CLI：tide / weather / geocode（多级降级）
├── db/
│   ├── INDEX.md             # 省份索引（检索入口）
│   ├── species.md           # 物种知识：滩涂类型推断、挖法、时令、吃法、方言
│   ├── shandong.md          # 按省分区的赶海点数据（另：fujian/zhejiang/guangdong/hainan）
│   └── …
├── templates/
│   ├── report.md            # 输出报告结构
│   ├── point-entry.md       # 新增赶海点条目模板
│   └── safety.md            # 撤退时刻/大潮红警/滩涂风险/合规 片段
└── scripts/
    └── validate_db.py       # 数据库校验（补点后运行）
```

## 快速使用

在 Claude 对话中：

- "这周六去青岛赶海" → 解析地点时间，查 `db/INDEX.md` → 读取 `db/shandong.md` 对应点 → 调 CLI 拉潮汐/天气 → 生成报告。
- "厦门西海岸能挖到什么" → 未命中内置库则 geocode，按滩涂类型推断物种。
- "三亚的后天退潮什么时候" → 直接给目标时段。

CLI 可独立调用：

```bash
python3 bin/beachcomb.py tide <lat> <lon> 2026-08-15
python3 bin/beachcomb.py weather <lat> <lon> 2026-08-15
python3 bin/beachcomb.py geocode 青岛
```

可选增强：设置 `BEACHCOMB_TIDE_API=<key>`（WorldTides/Storm Glass）走精确潮汐链路；否则自动降级为农历骨架（醒目标注、评分 ≤2★）。

## 如何贡献赶海点

1. 复制 `templates/point-entry.md` 到对应省文件 `db/<province>.md`；
2. 按模板填真实信息；
3. 更新 `db/INDEX.md` 的数量与代表点登记；
4. 运行 `python3 scripts/validate_db.py` 确认通过。

## Non-Goals（v1）

钓鱼预报 / 冲浪·潜水海况 / 生物影像识别 / 精细法律核对。被问及时坦率说明能力范围。

## 免责

本 skill 提供辅助信息，不构成法律或安全建议。**出海赶海务必：结伴 · 看官方潮汐表 · 涨潮前撤离 · 远离陌生滩涂。**