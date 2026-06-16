# AI 资讯追踪·发布记录 schema（v1.4 引入）

> **目的**：记录每份已发布的资讯（daily / weekly / special），供手动推送去重 / 补推 / PM 巡检 / 审计用。
> **位置**：`data/published/`（gitignored，运行时数据）
> **生成方**：`scripts/published_record.py`（被 `scripts/push_report.sh` EXIT trap 自动调用）
> **清理**：默认保留 90 天（`scripts/published_record.py cleanup --days 90`）

---

## 1. 目录结构

```
data/published/
├── index.json              # 索引：最近 90 天发布摘要（每条 ~200B）
├── daily/
│   ├── 2026-06-15.json    # 单日发布详情
│   ├── 2026-06-16.json
│   └── ...
├── weekly/
│   └── 2026-W24.json
├── special/
│   └── 2026-06-foundation-models.json
└── _dryrun/                # 干跑模式记录（不入主索引；调试用）
    └── 2026-06-15.json
```

---

## 2. 单条记录 schema（`data/published/<type>/<date>.json`）

```json
{
  "schema_version": "1.0",
  "report_type": "daily",
  "report_date": "2026-06-15",
  "report_file": "每日资讯/2026-06-15.md",
  "report_sha256": "abc123def456...",
  "report_size_bytes": 4521,
  "first_pushed_at": "2026-06-16T08:30:00+08:00",
  "last_pushed_at": "2026-06-16T08:30:00+08:00",
  "push_count": 1,
  "pushed_by": "calendar:8:30",
  "doc_url": "https://my.feishu.cn/docx/Aa1EdIk6BoofenxqO0qcOspBnCe",
  "ok": true,
  "channels": {
    "feishu": {"ok": true, "code": 0, "msg": "success"},
    "wecom_official": {"ok": true, "code": 0, "msg": "ok"}
  },
  "raw_summary": {
    "sources": {"qbitai": 5, "jiqizhixin": 14, "openai": 8, "36kr": 3, "deepmind": 0, "replicate": 0, "runway": 0, "langchain": 7, "huggingface": 0, "github_trending": 10},
    "total_articles": 47,
    "sources_with_zero": ["deepmind", "replicate", "runway", "huggingface"]
  },
  "error": null
}
```

### 字段说明

| 字段 | 类型 | 说明 |
|---|---|---|
| `schema_version` | string | 固定 `"1.0"` |
| `report_type` | string | `daily` / `weekly` / `special` |
| `report_date` | string | daily=`YYYY-MM-DD`；weekly=`YYYY-Www`（如 `2026-W24`）；special=报告名 basename |
| `report_file` | string | 相对仓库根的路径（如 `每日资讯/2026-06-15.md`） |
| `report_sha256` | string | 报告文件 SHA256（用于检测原文件改动后重推） |
| `report_size_bytes` | int | 报告文件字节数 |
| `first_pushed_at` | string | 首次推送时间（ISO8601 + tz） |
| `last_pushed_at` | string | 最近一次推送时间（ISO8601 + tz） |
| `push_count` | int | 累计推送次数（含 `--repush`） |
| `pushed_by` | string | 推送来源：`calendar:8:30` / `manual` / `backfill` / `dry-run` |
| `doc_url` | string\|null | 飞书云文档 URL（daily/weekly/special 有；management/test 无） |
| `ok` | bool | 任一渠道推送成功即 true；全部失败为 false |
| `channels` | object | 各渠道结果明细（key=渠道名） |
| `raw_summary.sources` | object | 各源 raw 抓取条数（key=源名） |
| `raw_summary.total_articles` | int | 总抓取条数 |
| `raw_summary.sources_with_zero` | string[] | 抓取为 0 条的源名列表（便于一眼识别问题源） |
| `error` | string\|null | 失败时的错误信息；成功为 null |

---

## 3. 索引 schema（`data/published/index.json`）

```json
{
  "schema_version": "1.0",
  "generated_at": "2026-06-16T23:41:00+08:00",
  "entries": [
    {
      "report_type": "daily",
      "report_date": "2026-06-15",
      "last_pushed_at": "2026-06-16T08:30:00+08:00",
      "push_count": 1,
      "ok": true,
      "channels_ok": ["feishu", "wecom_official"],
      "doc_url": "https://my.feishu.cn/docx/Aa1EdIk6BoofenxqO0qcOspBnCe"
    }
  ]
}
```

`entries` 数组按 `last_pushed_at` 倒序；自动清理 90 天外的；最新在前。

---

## 4. 推送来源标识（`pushed_by`）

| 值 | 触发场景 |
|---|---|
| `calendar:8:30` | 早上 8:30 daily 自动推送 |
| `calendar:20:00` | PM 巡检推送（management 类型） |
| `manual` | 主人手动调用 `push_report.sh 每日资讯/2026-06-15.md` |
| `backfill` | 主人调用 `--backfill daily --since X --until Y` 补推 |
| `dry-run` | 主人调用 `--dry-run` 干跑（不真推，落档 `_dryrun/`） |

---

## 5. 与 v1.3 PM 巡检联动

v1.3 PM 巡检第 9 维度「📤 发布」扫 `data/published/daily/`：

| 异常类型 | 检测规则 | 触发条件 |
|---|---|---|
| 漏推 | `每日资讯/<date>.md` 存在但 `data/published/daily/<date>.json` 不存在 | 最近 7 天有 ≥ 1 天漏推 |
| 推送失败累计 | `data/published/daily/<date>.json` 中 `ok=false` | 连续 ≥ 2 天失败 |

异常触发 v1.3 owner 私聊升级 hook（24h dedup）。

---

## 6. CLI 速查

```bash
# 列出已发布
python3 scripts/published_record.py list [daily|weekly|special|all]

# 查询某日是否已推
python3 scripts/published_record.py status daily 2026-06-15

# 清理 90 天外
python3 scripts/published_record.py cleanup --days 90

# 干跑写档
python3 scripts/published_record.py record --dry-run daily 每日资讯/2026-06-15.md '{}'
```

`scripts/push_report.sh` EXIT trap 会自动调用 `record` 子命令。
