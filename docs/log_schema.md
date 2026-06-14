# AI 资讯网 — 结构化日志 Schema v1.0

> 探针化 PM 巡检的根基。所有生产脚本必须按本 schema 写 JSON，探针才能解析。

---

## 一、目录布局

```
logs/
├── (text logs - 已有，不动)        # crawl_*.log / fetch_fulltext.log
└── daily/                          # 新增：结构化 JSON 日志
    ├── 2026-06-15-generate.json    # run_generate_daily.sh 写
    ├── 2026-06-15-push.json        # push_daily_to_feishu.sh 写
    └── 2026-06-15-inspect.json     # inspect_daily.sh 写（合并判定）
```

**为什么单开 `daily/` 子目录**：原 `logs/` 是文本爬虫日志，混进去不易分辨；探针只关心 `daily/` 下的 JSON。

---

## 二、`*-generate.json` — 抓取+生成产出

由 `scripts/run_generate_daily.sh` 结束前写。

```json
{
  "schema_version": "1.0",
  "date": "2026-06-15",
  "step": "generate",
  "ok": true,
  "started_at": "2026-06-15T01:00:05+08:00",
  "ended_at": "2026-06-15T01:23:45+08:00",
  "duration_sec": 1420,
  "metrics": {
    "sources_total": 8,
    "sources_ok": 8,
    "sources_failed": [],
    "items_total": 35,
    "items_red": 5,
    "items_yellow": 12,
    "items_blue": 18,
    "main_lines_count": 3,
    "llm_insights_count": 4
  },
  "outputs": {
    "report_path": "每日资讯/2026-06-15.md"
  },
  "error": null
}
```

**字段说明**：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `schema_version` | string | ✓ | 固定 `"1.0"`，便于将来兼容 |
| `date` | string | ✓ | 报告日期 `YYYY-MM-DD` |
| `step` | string | ✓ | 固定 `"generate"` |
| `ok` | bool | ✓ | 整体成功标志 |
| `started_at` / `ended_at` | ISO8601 | ✓ | 脚本起止时间（带时区） |
| `duration_sec` | int | ✓ | 运行时长 |
| `metrics.sources_total` | int | ✓ | 计划抓取源数 |
| `metrics.sources_ok` | int | ✓ | 实际成功源数 |
| `metrics.sources_failed` | array | ✓ | 失败源名列表（空数组表示全成功） |
| `metrics.items_total` | int | ✓ | 报告条数 |
| `metrics.items_red` | int | ✓ | 🔴 重要条数 |
| `metrics.items_yellow` | int | ✓ | 🟡 值得留意条数 |
| `metrics.items_blue` | int | ✓ | 🔵 知晓类条数 |
| `metrics.main_lines_count` | int | ✓ | 主线信号数 |
| `metrics.llm_insights_count` | int | ✓ | LLM 精炼洞察数 |
| `outputs.report_path` | string | ✓ | 报告落盘相对路径 |
| `error` | string \| null | ✓ | 错误信息（ok=true 时为 null） |

**stats 来源**：
- 报告 markdown 头部通常有汇总（如 `📊 8 源 / 35 条`）
- 用 `grep -E "^[📊🟢🔴🟡🔵]" <report>` 提取
- 或在生成器里直接 collect

---

## 三、`*-push.json` — 推送产出

由 `scripts/push_daily_to_feishu.sh` 结束前写。

```json
{
  "schema_version": "1.0",
  "date": "2026-06-15",
  "step": "push",
  "ok": true,
  "started_at": "2026-06-15T08:30:00+08:00",
  "ended_at": "2026-06-15T08:30:12+08:00",
  "duration_sec": 12,
  "metrics": {
    "items_pushed": 35
  },
  "outputs": {
    "doc_url": "https://my.feishu.cn/docx/EbwydDY9ooPaGDxWt8VcGG8Cngh",
    "report_path": "每日资讯/2026-06-15.md"
  },
  "webhook": {
    "code": 0,
    "msg": "ok"
  },
  "error": null
}
```

**字段说明**：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `outputs.doc_url` | string | ✓ | 飞书云文档 URL（推送 text 消息中那条） |
| `webhook.code` | int | ✓ | 飞书 webhook 响应 code（0=成功） |
| `webhook.msg` | string | ✓ | 飞书响应 msg |
| `metrics.items_pushed` | int | ✓ | 推送时报告条数（与 generate 一致） |

---

## 四、`*-inspect.json` — 探针合并判定

由 `scripts/inspect_daily.sh` 写。**这是 12:00/20:00 巡检实际读的文件。**

```json
{
  "schema_version": "1.0",
  "date": "2026-06-15",
  "inspected_at": "2026-06-15T12:00:30+08:00",
  "overall_ok": true,
  "alert_level": "ok",
  "steps": {
    "generate": { "ok": true, "items_total": 35, "red": 5, "main_lines": 3, "duration_sec": 1420 },
    "push": { "ok": true, "doc_url": "https://...", "webhook_code": 0, "duration_sec": 12 }
  },
  "alerts": [],
  "summary": "✅ 6-15 全链路 ok｜8 源 / 35 条（🔴5 / 🟡12） / 3 主线 / 推送成功"
}
```

**alert_level 取值**：

| level | 触发条件 | 探针动作 |
|---|---|---|
| `ok` | 所有 steps ok + metrics 满足阈值 | 静默或发极简 ✅ |
| `info` | 主线=0 或 🔴=0 | 群里发一行告知 |
| `warn` | items_total<20 或 sources_failed 1 个 | 群里发告警 + @PM |
| `critical` | generate.ok=false 或 push.ok=false | 群里发告警 + @主人 + 写 recent_memory/incident/ |

**判定规则**（硬编码到 inspect 脚本）：

```bash
# 阈值常量
MIN_ITEMS=20
MIN_RED=0          # 0 不算异常
MIN_MAIN_LINES=0   # 0 不算异常
MAX_SOURCE_FAIL=1  # 1 个源失败 → warn；2+ → critical

# 判定顺序
if ! generate.ok: critical
elif ! push.ok: critical
elif sources_failed >= 2: critical
elif sources_failed >= 1: warn
elif items_total < 20: warn
else: ok
# info 条件单独看
if ok and (red == 0 or main_lines == 0): info
```

---

## 五、探针动作契约

`inspect_daily.sh` 必须：
1. 读 `logs/daily/YYYY-MM-DD-{generate,push}.json`（缺失则视为对应 step ok=false）
2. 判定 alert_level
3. 写 `logs/daily/YYYY-MM-DD-inspect.json`
4. 若 level ∈ {warn, critical}：构造 webhook 告警 payload，推送
5. 若 level == ok：静默（或 12:00 极简发）
6. exit code：0=ok，1=warn，2=critical（便于 calendar 兜底判别）

---

## 六、向后的兼容

- 探针读不到某 step 的 JSON → 视为该 step `ok=false`（不报错，因为是"漏跑"而非"失败"）
- 旧 text log 保留不动，inspect 不读
- schema_version 字段为后续升级留位

---

## 七、相关文件

- `scripts/run_generate_daily.sh` — 写 generate JSON
- `scripts/push_daily_to_feishu.sh` — 写 push JSON
- `scripts/inspect_daily.sh` — 写 inspect JSON（探针主入口）
- `recent_memory/decision/probe_inspection_design.md` — 探针设计决策
