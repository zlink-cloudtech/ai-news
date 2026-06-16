# AI 资讯追踪

> 每天 8:30 自动汇总"昨日"全球 AI 资讯，按话题板块整理 Top 10，每周做周报；自动推送到飞书群和企微群。
> 由 AI 调研专家 维护，王鸿奇 终审。

## ✨ 这是什么

| 环节 | 能力 |
|------|------|
| **抓取** | 10 个权威源（OpenAI / DeepMind / HuggingFace / LangChain / GitHub Trending / Replicate / Runway / 36kr / 量子位 / 机器之心），每源每日一次 |
| **生成** | 按板块（🧠 LLM 发展 / 💻 编程 Agent / 🧍 数字人 / 📦 其他）整理 Top 10，含量化评分、影响分析、跨话题洞察 |
| **推送** | 每日 8:30 飞书 + 企微双渠道，周报/专题复用同一链路（v1.2 推送架构） |
| **巡检** | 每日 12:00 / 20:00 PM 巡检 9 维度，异常触发 Owner 私聊升级（v1.3 + v1.4） |
| **手动推送** | 任意历史日期 / 任意消息类型手动触发，发布记录全程留痕（v1.4） |

## 🚀 快速开始

### 看资讯

- **飞书群 / 企微群**：每天 8:30 自动推送（卡片 + 云文档链接）
- **GitHub**：<https://github.com/zlink-cloudtech/ai-news/tree/main/每日资讯>
- **周报**：<https://github.com/zlink-cloudtech/ai-news/tree/main/每周汇总>

### 本地跑一次（开发 / 调试）

```bash
# 1. 克隆 + 装依赖
git clone https://github.com/zlink-cloudtech/ai-news.git
cd ai-news
pip install feedparser trafilatura cloudscraper beautifulsoup4 lxml html2text requests

# 2. 配 secrets（仅当你需要推送；纯本地跑可跳过）
cp .secrets.example .secrets
# 编辑 .secrets 填入飞书/企微 webhook（详见「配置」章节）

# 3. 抓取昨日（默认 8 源；HuggingFace 需网络可达，GitHub Trending 需 RSSHub）
python3 scripts/crawlers/run_all.py

# 4. 生成资讯
python3 scripts/generators/generate_daily.py

# 5. 推送（自动跳过当日已推过的；dry-run 不真发）
bash scripts/push_report.sh                # 真推
bash scripts/push_report.sh --dry-run      # 只看输出
bash scripts/push_report.sh --backfill daily 2026-06-15   # 手动推历史
```

更多参数：[scripts/crawlers/README.md](scripts/crawlers/README.md) / [scripts/generators/README.md](scripts/generators/README.md) / [scripts/push_report.sh](scripts/push_report.sh)（顶部注释）

## 📐 推送架构（v1.2 + v1.2.1 + v1.4）

### 渠道

| 渠道 | 类型 | 消息类型 | 触发时间 |
|------|------|---------|---------|
| 飞书 | 正式+管理+测试 | daily / weekly / special / management / test | 8:30 / 12:00 / 20:00 |
| 企业微信·正式 | 正式 | daily / weekly / special | 8:30 |
| 企业微信·测试 | 测试 | test | 手动 |

> v1.2.1 起企微拆分为「正式 + 测试」两个独立 webhook，避免测试消息污染生产群。

### 消息类型

- `daily` — 每日资讯（每日 8:30）
- `weekly` — 周报（每周日 8:30，v1.5 计划）
- `special` — 专题（手动）
- `management` — PM 巡检报告（每日 12:00 / 20:00）
- `test` — 探针 / 手动测试

### 链路概览

```
抓取 (run_all.py) → data/raw/<src>/<date>.json
       ↓
生成 (generate_daily.py) → 每日资讯/<date>.md
       ↓
推送 (push_report.sh) → 匹配 channels.json → 飞书云文档 + webhook
       ↓
发布记录 (v1.4) → data/published/<rtype>/<date>.json
       ↓
推送日志 → logs/daily/<date>-push.jsonl
```

详细架构、渠道矩阵、隐含 purpose 映射见 [AGENTS.md §2](AGENTS.md)。

## 📁 目录结构

```
AI资讯追踪/
├── README.md / AGENTS.md            # 仓库说明 / 开发者指南
├── AUTO_PUSH.md / 汇报方式.md        # 早期阶段文档（保留作历史）
├── LICENSE                           # MIT
├── .secrets.example                  # secrets 格式参考（.secrets 自身 gitignored）
│
├── 资讯源管理/                        # 源生命周期管理（已对接/待对接/待预研/已废弃）
├── 模板/                              # 资讯/周报 Markdown 模板
├── 每日资讯/                          # 生成结果（每日一份 .md）
├── 每周汇总/                          # 周报（v1.5 计划切换到 周报/）
│
├── scripts/
│   ├── crawlers/                     # 10 源抓取脚本（见 crawlers/README.md）
│   ├── generators/                   # daily / weekly / inspection 生成器
│   ├── push_report.sh                # 推送入口（v1.4 5 大块改造）
│   ├── pm_inspect.sh                 # PM 巡检包装
│   ├── published_record.py           # 发布记录 CLI（v1.4 新增）
│   ├── probe.py                      # 探针（健康检查 / 渠道探活）
│   ├── run_generate_daily.sh         # 每日抓取+生成+推送一站式入口
│   └── news-commit.sh                # 一键 commit（post-commit hook 自动 push）
│
├── config/                           # 配置（gitignored secrets 在仓库根 .secrets）
│   ├── channels.json                 # 推送渠道矩阵（v1.2.1）
│   └── inspector.yaml                # PM 巡检阈值（v1.3 + v1.4）
│
├── docs/                             # 技术 schema 文档
│   ├── data-published-schema.md      # 发布记录 schema（v1.4）
│   └── log_schema.md                 # 推送/生成/巡检 JSON 日志 schema
│
├── data/                             # 运行时数据（gitignored）
│   ├── raw/<source>/<date>.json      # 10 源抓取原始数据
│   ├── articles/                     # 抓全文缓存
│   ├── llm_cache/                    # LLM 精炼缓存（deepseek-v4-flash）
│   ├── inspections/                  # PM 巡检详细报告
│   └── published/                    # 发布记录（v1.4）
│
└── logs/                             # 爬虫 / 推送 / 巡检日志
    ├── crawl_*.log                   # 爬虫文本日志
    └── daily/                        # 结构化 JSON 日志（*-push.jsonl 等）
```

## ⚙️ 配置

### 1. 渠道密钥（`.secrets`，gitignored）

复制 `.secrets.example` 为 `.secrets`，按需填入：

```bash
FEISHU_AI_NEWS_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/<hook-id>
FEISHU_AI_NEWS_SECRET=<webhook-secret>
WECOM_AI_NEWS_OFFICIAL_WEBHOOK=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=<key>
WECOM_AI_NEWS_TEST_WEBHOOK=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=<key>
```

### 2. 渠道矩阵（`config/channels.json` v1.2.1）

```json
"channels": {
  "feishu":          {"enabled": true, "purpose": ["official","management","test"], "message_types": [...]},
  "wecom_official":  {"enabled": true, "purpose": ["official"], "message_types": ["daily","weekly","special"]},
  "wecom_test":      {"enabled": true, "purpose": ["test"],     "message_types": ["test"]}
}
```

`message_types` 为空数组 = 不推（默认安全）。v1.2 起 webhook URL/secret 改为 env 引用，**不**再存明文。

### 3. PM 巡检阈值（`config/inspector.yaml` v1.3 + v1.4）

| 字段 | 含义 | 默认 |
|------|------|------|
| `consecutive_zero_days` | 源连续 N 天 0 条触发 ⚠️ | 2 |
| `lookback_days` | 累计异常回溯窗口 | 7 |
| `disk_warn_pct` | 磁盘使用率告警 | 80 |
| `push_log_warn_bytes` | 推送日志单文件告警 | 10MB |
| `feishu_max_chars` | 飞书端 management 字符上限 | 1800 |
| `publish_lookback_days` | 第 9 维度「📤 发布」回溯窗口（v1.4） | 7 |
| `escalate_on` | 触发 Owner 私聊的异常类型 | 见文件 |

## 📊 当前进度

- ✅ 10 源接入（8 源默认 + 2 源可选）
- ✅ 飞书 + 企微双渠道（v1.2.1 拆分）
- ✅ PM 巡检 9 维度（v1.3 + v1.4）
- ✅ 手动推送 / 发布记录 / skip-if-pushed（v1.4）
- 🚧 周报推送链路（v1.5 计划中）

## 🛠️ 进阶

| 你想做什么 | 看哪里 |
|-----------|--------|
| 改代码 / 加新源 / 修推送 | [AGENTS.md](AGENTS.md) |
| 加新信息源 | [资讯源管理/README.md](资讯源管理/README.md) |
| 看发布记录 schema | [docs/data-published-schema.md](docs/data-published-schema.md) |
| 看推送/巡检 JSON 日志 schema | [docs/log_schema.md](docs/log_schema.md) |
| 看抓取脚本细节 | [scripts/crawlers/README.md](scripts/crawlers/README.md) |
| 看生成器细节 | [scripts/generators/README.md](scripts/generators/README.md) |
| 早期阶段说明 | [汇报方式.md](汇报方式.md) / [AUTO_PUSH.md](AUTO_PUSH.md) |

## 维护

由 AI 调研专家 自动维护，详细开发约定、踩坑记录、决策索引见 [AGENTS.md](AGENTS.md)。
