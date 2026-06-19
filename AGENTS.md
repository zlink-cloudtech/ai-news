# AGENTS.md — AI 资讯追踪·Agent 开发指南

> 面向参与本仓库开发/维护的 AI Agent（和接手的人类开发者）。本文件聚焦「为什么这么设计 / 怎么改 / 踩过什么坑」，**不**重复 README 的「怎么用」。
>
> 配套阅读：
> - 读者视角：[README.md](README.md)
> - 决策日志（agent 私有）：`../recent_memory/decision/ai_news_*.md`（32 个文件）
> - 项目主档（agent 私有）：`../recent_memory/project/AI资讯追踪系统.md`

## 0. 三层文档分工

| 文件 | 读者 | 答什么问题 |
|------|------|-----------|
| **README.md** | 仓库读者 / 新人 | 这是什么？怎么用？去哪看？ |
| **AGENTS.md**（本文件） | AI Agent / 接手开发者 | 为什么这么设计？怎么改？踩过什么坑？ |
| `recent_memory/decision/`（私有） | Agent 私有 | 历史上做过什么决策？为什么？ |
| `MEMORY.md` / `基础设定/`（私有） | Agent 私有 | 当前状态、规则、敏感信息 |

`AGENTS.md` 引用 `recent_memory/` 是**只引用路径**，**不**复制内容；内容只在 agent 工作目录可见。

---

## 1. 仓库工程化

### 1.1 Git 链

- **远程**：HTTPS + PAT 嵌入 URL（PAT 仅限 `ai-news` 仓库 Contents Read/Write）
- **分支**：`main`（生产）+ `feature/vX.X.X-<short-desc>`（开发预发，详见 §10）— 6-18 主人规范起强制
- **post-commit hook**：每次 `git commit` 自动 `git push origin main`（**只对 main 生效**；feature 分支不自动 push）
- **Git LFS**：管理图片/视频/文档（当前无大文件入库）
- **commit 风格**：`feat(<scope>): <简述>` / `fix(<scope>): <简述>` / `refactor(<scope>): <简述>`
- **push 失败时**：用 `git push --no-verify` 绕过 git-lfs pre-push hook（hook 误报时）
- **tag**：合入 main 后立即 `git tag -a vX.X.X -m "..."` + `git push origin vX.X.X`（附注 tag，含完整版本说明）

### 1.2 关键版本演进（20 commits）

| 版本 | 主题 | 关键 commit |
|------|------|-------------|
| **v1.0** | 飞书 webhook + 云文档 | `ad55ae0` / `91e6a19` / `26a951f` |
| v1.0 演进 | 推送拆解 / 纯汇报体 / 探针 | `96c1aee` / `b6db6ad` / `07b3a58` |
| v1.0 修 | webhook 切换 / 签名校验 / 多渠道 | `0833f88` / `38153cf` / `2e6a71c` |
| v1.0 修 | 企微公开访问 / 消息类型白名单 | `fbd62ab` / `d7d28c8` |
| **v1.2** | 推送架构升级（去明文密钥 + purpose + 隐含 purpose） | `83597a1` |
| **v1.2.1** | wecom 渠道拆分（official + test） | `fa373d2` |
| **v1.3** | PM 日报 8 维度 + 飞书端节流 + 异常升级 hook | `c0286dc` |
| **v1.4** | 手动推送 + 发布记录 + PM 第 9 维度 | `aac59a4` |
| **v1.5** | 周报推送链路（计划中） | — |

完整 git log：`git log --oneline -20`

---

## 2. 推送架构（深度）

### 2.1 链路时序

```
[1:00 抓取 calendar]
  └─ scripts/crawlers/run_all.py
      └─ 10 源抓取 → data/raw/<src>/<date>.json
          └─ data/articles/<id>.json（抓全文缓存）

[8:30 daily 推送 calendar]
  └─ scripts/run_generate_daily.sh
      ├─ python3 scripts/crawlers/run_all.py
      ├─ python3 scripts/generators/generate_daily.py
      │     └─ 每日资讯/<date>.md
      └─ bash scripts/push_report.sh
          ├─ 读 channels.json → 匹配 feishu + wecom_official
          ├─ 隐含 purpose 映射（daily/weekly/special → official）
          ├─ skip-if-pushed 检查（已推过则静默跳过，v1.4）
          ├─ 建飞书云文档（lark-cli docs +media-upload / docs +update）
          ├─ 推 webhook（feishu / wecom）
          ├─ 写 data/published/daily/<date>.json（v1.4 发布记录）
          └─ 写 logs/daily/<date>-push.jsonl

[12:00 / 20:00 PM 巡检 calendar]
  └─ scripts/pm_inspect.sh
      ├─ python3 scripts/generators/generate_inspection.py
      │     ├─ 8 维度巡检（v1.3）
      │     ├─ + 📤 发布维度（v1.4：扫 data/published/daily/ 最近 7 天）
      │     ├─ data/inspections/<date>-<HH-MM>.md（详细）
      │     └─ data/inspections/<date>-<HH-MM>-feishu.md（飞书端 ≤1800 字符）
      ├─ bash scripts/push_report.sh（推 management 消息）
      └─ 异常触发 Owner 私聊（lark-cli im +message-send --as user）
          └─ 24h dedup（避免重复打扰）
```

### 2.2 渠道 × 消息类型矩阵

| channel / message_type | daily | weekly | special | management | test |
|------------------------|:-----:|:------:|:-------:|:----------:|:----:|
| feishu                 | ✅    | ✅     | ✅      | ✅         | ✅   |
| wecom_official         | ✅    | ✅     | ✅      | ❌         | ❌   |
| wecom_test             | ❌    | ❌     | ❌      | ❌         | ✅   |

定义见 `config/channels.json` `channels.<name>.message_types`（空数组 = 不推）。

### 2.3 隐含 purpose 映射

显式声明（`channel.purpose`）**不**覆盖隐含映射。隐含规则：

| message_type | 隐含 purpose |
|--------------|--------------|
| daily        | official     |
| weekly       | official     |
| special      | official     |
| management   | management   |
| test         | test         |

设计原因：v1.2 前显式声明的 `purpose: ["official"]` 会被错配 — 加 `wecom_test` 时若忘加 `test` 进 `purpose` 就漏推。

---

## 3. 配置与环境变量

### 3.1 渠道矩阵（`config/channels.json`）

- `schema_version: "1.2"` 强制迁移到 env 引用（`webhook_url_env` / `webhook_secret_env`）
- 缺 env 变量 → 加载 fail-fast（不静默回退明文）
- 字段说明见文件内 `_schema_doc_v1.2`

### 3.2 PM 巡检阈值（`config/inspector.yaml` v1.3 + v1.4）

| 字段 | 含义 | 默认 |
|------|------|------|
| `consecutive_zero_days` | 源连续 N 天 0 条触发 ⚠️ | 2 |
| `lookback_days` | 累计异常回溯窗口 | 7 |
| `disk_warn_pct` | 磁盘使用率告警 | 80 |
| `push_log_warn_bytes` | 推送日志单文件告警 | 10MB |
| `feishu_max_chars` | 飞书端 management 字符上限 | 1800 |
| `publish_lookback_days` | 第 9 维度「📤 发布」回溯窗口 | 7 |
| `escalate_on` | 触发 Owner 私聊的异常类型 | 见文件 |
| `escalate_dedup_hours` | 同一异常升级间隔 | 24 |

`escalate_on` 可选项：`consecutive_zero` / `calendar_missed` / `schema_invalid` / `publish_missed` (v1.4) / `publish_consecutive_failure` (v1.4)

### 3.3 Secrets（`.secrets`，gitignored）

| 变量 | 用途 |
|------|------|
| `FEISHU_AI_NEWS_WEBHOOK` | 飞书正式+管理+测试 群 webhook URL |
| `FEISHU_AI_NEWS_SECRET` | 飞书签名校验密钥（v1.0 后期启用） |
| `WECOM_AI_NEWS_OFFICIAL_WEBHOOK` | 企微生产群 webhook（v1.2.1 转正） |
| `WECOM_AI_NEWS_TEST_WEBHOOK` | 企微测试群 webhook（v1.2.1 新增） |

> 任何在 git 仓库**直接看到的**密钥都是事故 — 立刻 revoke + 重新生成。

### 3.4 LLM（`scripts/generators/_llm.py`）

- 模型：`deepseek-v4-flash`
- 缓存目录：`data/llm_cache/`（gitignored）
- 用途：core 精炼（每条资讯）+ impact 分析（基于关键词模板兜底）

---

## 4. 日历与 PM 节奏

| 时间 | 事件 | 用途 | 命令入口 |
|------|------|------|---------|
| 每天 1:00 | 抓取 | 抓 10 源昨日 | `python3 scripts/crawlers/run_all.py` |
| 每天 8:30 | daily 推送 | 抓+生成+推飞书企微 | `bash scripts/run_generate_daily.sh` |
| 每天 12:00 | PM 日间巡检 | 9 维度巡检 + 推 management | `bash scripts/pm_inspect.sh 12:00` |
| 每天 20:00 | PM 夜间巡检 | 同上 | `bash scripts/pm_inspect.sh 20:00` |
| 每周日 8:30 | weekly 推送 | v1.5 计划 | `bash scripts/run_generate_weekly.sh`（待建） |
| 每周日 4 段 | 迭代 | PM 机制 | 周复盘 |

详细 PM 机制、ROI 评分卡、4 象限复盘见 `../recent_memory/project/AI资讯网_PM机制.md`（agent 私有）。

---

## 5. 踩坑记录（13 条，按版本分组）

### v1.2（5 条）
1. **隐含 purpose 设计漏洞** — v1.2 加 `wecom_test` 时若忘加 `test` 进 `purpose` 就漏推。修复：加隐含映射（daily/weekly/special → official；management → management；test → test）。
2. **push log 命名错位** — v1.2 前用 push 时间命名，v1.2 起按 report_date 命名（8:30 推 6-15 → `2026-06-15-push.jsonl`），便于按报告日查日志。
3. **management 消息无校验词被飞书拒** — 飞书 webhook 要求消息体含校验词。修复：pusher 自动加 `【AI资讯·管理消息】` 前缀。
4. **lark-cli `--content` 要求相对路径** — 传绝对路径会被拒。修复：传 `每日资讯/<date>.md`（相对于仓库根）。
5. **`generate_inspection.py` 假设 raw 是 list** — 实际是 dict 含 `count` / `items`。修复：明确 `.get("items", [])`。

### v1.3（2 条）
6. **lark-cli 子命令** — `+messages-send`（带 `+` 前缀），不是 `messages-send`。
7. **lark-cli 私聊参数** — 用 `--user-id` + `--receive-id-type open_id`，**不**用 `--chat_id`。
8. **`recent_memory/` 在 agent workspace** — **不**在 git 仓库。写入路径别搞错（`/app/data/所有对话/主对话/recent_memory/`，**不**是 `AI资讯追踪/recent_memory/`）。

### v1.4（4 条）
9. **`edit_file` 失效时** — 改用 `python3 -c "import pathlib; p=pathlib.Path('...'); p.write_text(p.read_text().replace('OLD','NEW'))"` 或 `sed -i`。
10. **`render_*` 函数漏加新 section** — 实施期 + 验证期双重检查。bug 6 教训：v1.4 第 9 维度首次实施时 `render_full_report` / `render_feishu_summary` / `should_escalate` 三处都漏加，验证详细报告时才发现。**规则：新增维度必须搜全 `render_` 函数 + `should_escalate` + `make_conclusion` 三个地方**。
11. **`.gitignore` 行内中文注释** — git 把 `data/inspections/  # 中文` 视为 no-op pattern，pattern 不生效。修复：拆为独立 `# 注释行` + `pattern 行`。`*.gitignore` 写完后必须 `git check-ignore -v <path>` 验证。
12. **dry-run 早退点必须在 `NEED_DOCX=true` 之前** — 避免 dry-run 创建正式群文档（污染 docx 库）。
13. **`--backfill` 子脚本用相对路径** — 父脚本用绝对路径调子脚本失败。修复：子 `push_report.sh` 用 `cd "$(dirname "$0")/.."` + 相对路径。

完整修复表见 `../recent_memory/decision/ai_news_v1_2_push_architecture.md` / `ai_news_v1_3_pm_daily_report.md` / `ai_news_v1_4_manual_push_published.md`。

---

## 6. 决策记录索引

所有重大决策都在 `../recent_memory/decision/ai_news_*.md`（agent 私有，**不**在 git 仓库）。

**最新 4 个关键决策**（实施前必读）：

| 决策 | 文件 |
|------|------|
| v1.4 手动推送 + 发布记录 + PM 第 9 维度 | `ai_news_v1_4_manual_push_published.md` |
| v1.3 PM 日报 8 维度 + 飞书节流 + 异常升级 | `ai_news_v1_3_pm_daily_report.md` |
| v1.2.1 wecom 渠道拆分 | `ai_news_wecom_official_split_v121.md` |
| v1.2 推送架构升级（去明文密钥 + purpose） | `ai_news_v1_2_push_architecture.md` |

其余 28 个决策覆盖：源评估 / 飞书集成 / 推送失败 19001 / 沙箱 workaround / 报告格式原则 / 多渠道 push v2 / 周报生成器 / 中文源 3 集成 / LLM 实现 / 文档公开访问 / 签名校验 / GitHub 源评估 / RSSHub 评估 / 探针安全原则 / 速度赛 bug 修复 / 沙箱 workaround / 等等。

---

## 7. 测试方法

### 7.1 端到端

```bash
# dry-run（不推送，只看输出）
bash scripts/push_report.sh --dry-run

# 推 management 消息测试（PM 巡检）
bash scripts/pm_inspect.sh 12:00 --no-escalate

# 手动推历史 daily（v1.4）
python3 scripts/published_record.py record --type daily --date 2026-06-15
bash scripts/push_report.sh --backfill daily 2026-06-15

# 手动推历史 weekly（v1.5 落地后）
bash scripts/push_report.sh --backfill weekly 2026-W24
```

### 7.2 探针（健康检查 / 渠道探活）

```bash
python3 scripts/probe.py --channel feishu
python3 scripts/probe.py --channel wecom_official
python3 scripts/probe.py --channel wecom_test
```

### 7.3 发布记录（v1.4）

```bash
# 查询某日发布状态
python3 scripts/published_record.py status --type daily --date 2026-06-15

# 列出最近 N 天发布记录
python3 scripts/published_record.py list --type daily --days 7

# 清理超期记录（默认保留 90 天）
python3 scripts/published_record.py cleanup --keep-days 90
```

### 7.4 单元 / 集成

```bash
# 评分规则
python3 -c "from scripts.generators._utils_gen import score_item; print(score_item({...}, '2026-06-15'))"

# LLM 调用（带缓存）
python3 -c "from scripts.generators._llm import call_llm; print(call_llm('test prompt'))"
```

---

## 8. 未来规划

### 8.1 v1.5（周报推送链路）— 设计稿已发，等主人拍板

5 个待澄清问题：

- **Q1 路径修复**：A（推荐）改 `generate_weekly.py --out-dir` 默认 `每周汇总` → `周报`（与 v1.2 命名风格一致）
- **Q2 推送节奏**：A（推荐）周日 8:30 推上一周 / B 周一 8:30 / C 不补 schedule / D 周日晚 20:00
- **Q3 推送内容格式**：A 标题+链接 / B（推荐）标题+一句话总结+链接
- **Q4 周报文件命名**：ISO 周 `2026-W24`（当前实现）/ 日期段 / 其他
- **Q5 周报历史回填**：v1.4 `--backfill weekly` 已支持，是否 calendar 触发补推历史周报

详细设计稿见飞书群（chat_id `oc_fff41f90fa147c92871c778651731f8d`，6-17 00:12 消息）。

### 8.2 待主人拍板（4 项长期未决）

| 议题 | 提出时间 | 备选 |
|------|---------|------|
| RSSHub 部署路线 | 6-15 14:25 | A1 ngrok / A2 Cloudflare Tunnel / A3 云服务器 / B 直抓 HTML |
| P2/P3 是否升级飞书任务清单 | 6-15 20:00 | 升 / 不升（已问 4 次未答） |
| deepmind / replicate / runway 累计异常处置 | 6-16 21:55 | A 容忍 / B 从 channels.json 移除 / C 补抓诊断 |
| 飞书任务清单 guid `7390130b-b251-4b10-9ea2-85d11a1caf2a` | 6-14 15:20 | 建 / 不建 |

### 8.3 数据/产物

| 路径 | 大小 | 内容 |
|------|------|------|
| `data/raw/<src>/<date>.json` | ~270K | 10 源 × N 天抓取原始数据 |
| `data/articles/<id>.json` | ~610K | 抓全文缓存（深读时生成） |
| `data/llm_cache/` | ~30K | LLM 精炼缓存（gitignored） |
| `data/inspections/<date>-<HH-MM>.md` | ~20K | PM 巡检详细报告 |
| `data/published/<rtype>/<date>.json` | ~40K | 发布记录（v1.4，gitignored） |
| `logs/daily/<date>-push.jsonl` | ~100K | 推送审计日志 |

---

## 9. 推进新需求的标准流程

1. **摸现状**：先 `git log --oneline -10` + 读对应决策文件（`decision/`）
2. **设计稿**：出 4 目标 + 5 设计 + N 个待澄清问题，发飞书群
3. **拍板**：主人回复选项（如 "B/A/B/B/A/B/A"）
4. **落地**：按拍板分 3-5 步实施，每步独立 commit
5. **测试**：端到端 + 探针 + 单元（详见 §7）
6. **决策记录**：写 `decision/ai_news_v1_X_*.md`（含完整拍板链 + 实施步骤 + bug 修复）
7. **汇报**：飞书群发 commit 链接 + 关键新增 + 验证点 + 决策记录路径

详细节奏可参考 `../recent_memory/decision/ai_news_v1_4_manual_push_published.md`（v1.4 完整 4 步 + 6 bug 修复 + 验证全过的标准范例）。

---

## 10. Git 分支与预发测试流程（6-18 主人规范·v1.2.2 实施起强制）

> **背景**：v1.2.2 之前所有开发都直接在 main 上 commit + push（单人开发 + 沙箱 + 高频小改）。6-18 主人拍板：从下一次新需求开始走"开发分支 → 预发测试 → 合入"的标准流程，避免未验证代码污染 main / 企微生产群。
>
> **生效起点**：v1.2.2 commit `15f826a` 已为支持本流程实施完 `--include-manual` / `--only` / `--doc-title-prefix` 三个参数。

### 10.1 5 步标准流程

```
1. **创建 feature 分支**（基于 main HEAD）
   git checkout main && git pull
   git checkout -b feature/vX.X.X-<short-desc>

2. **在分支上开发**：commit 不限次数，message 风格 feat/fix/refactor(<scope>)

3. **预发测试**（涉及 push 链路时）：
   python3 -m scripts.cli push-report <md> \
       --include-manual --only wecom_test --doc-title-prefix="[预发]"
   → 推到 wecom_test（manual_only=true，calendar 自动任务不选）
   → 主人在测试群确认 daily 形态可打开 + 内容正确

4. **合入 main**（主人确认测试通过后）：
   git checkout main
   git merge --no-ff feature/vX.X.X-<short-desc> \
     -m "Merge feature/vX.X.X-<short-desc>"
   git push origin main
   git branch -d feature/vX.X.X-<short-desc>  # 本地删除

5. **打 tag**（合入后立即）：
   git tag -a vX.X.X -m "<版本主题>"
   git push origin vX.X.X
```

### 10.2 命名规范

| 要素 | 规则 | 示例 |
|------|------|------|
| 分支名 | `feature/vX.X.X-<short-desc>` | `feature/v1.3-pm-p0-p1`、`feature/v1.3.1-wecom-markdown-fix` |
| Tag 名 | `vX.X.X`（与分支前缀版本号一致） | `v1.2.2`、`v1.3` |
| 合并策略 | `--no-ff`（保留 feature commit + merge commit 双痕迹） | — |
| docx 测试标题前缀 | `[预发]`（与 `--doc-title-prefix` 一致） | `[预发] 🤖 AI每日资讯 \| 2026-06-17（昨日）` |
| commit author | `AI资讯追踪 <ai-news@zlink.cloud>` | （沙箱无 git config 时需 `-c user.name=... -c user.email=...`） |

### 10.3 适用范围

| 改动类型 | 是否走流程 | 预发测试 |
|----------|-----------|---------|
| push 链路（`scripts/push_report.py` / `scripts/renderers/`） | **必须** | wecom_test |
| 渠道配置（`config/channels.json` / `.secrets`） | **必须** | wecom_test |
| 云文档 API（lark-cli 调用） | **必须** | wecom_test |
| 抓取/生成器（`scripts/crawlers/` / `scripts/generators/`） | **必须** | 抓 dry-run + 主 agent 自检 |
| 纯文档（`AGENTS.md` / `README.md` / 决策记录） | **建议**（分支+合入，但不走 wecom_test） | 主人直接 review |
| data/ 运行时数据 | **不需** | 生产任务自动落档 |
| LLM 提示词 | **建议**（v2.0 渲染器会读 prompt） | dry-run 渲染输出对比 |

### 10.4 预发测试命令参考

```bash
# 推 daily 到 wecom_test（带 [预发] 前缀）
python3 -m scripts.cli push-report "每日资讯/2026-06-17.md" \
    --include-manual --only wecom_test --doc-title-prefix="[预发]"

# 推 weekly 到 wecom_test
python3 -m scripts.cli push-report "每周汇总/2026-W25.md" \
    --include-manual --only wecom_test --doc-title-prefix="[预发-周报]"

# dry-run（不真发，只看渲染结果）
python3 -m scripts.cli push-report "每日资讯/2026-06-17.md" \
    --include-manual --only wecom_test --doc-title-prefix="[预发]" --dry-run
```

### 10.5 与 v1.2.2 manual_only 的关系

- **v1.2.2 commit `15f826a`** 就是为支持本流程而实施
- **wecom_test 配置**：`manual_only=true` + `purpose=["test","official"]` + `message_types` 包含 daily/weekly/special — calendar 自动任务**永远不选** wecom_test，只有手动 `--include-manual` 才会触发
- **6-18 12:33 主人确认**：6-17 报告（docx `SIK5dqUxJoISwBxFkktcySwYn6b`）可在企微正常打开；`_render_daily_for_wecom` 仍用 `[👉 完整报告](URL)` markdown 形式，**不修改为裸 URL**

### 10.6 异常处理

- **push 失败**（git-lfs hook 误报）：`git push --no-verify origin main`（不绕安全检查，只绕缺失 git-lfs 二进制的 hook）
- **merge 冲突**：在 feature 分支上 rebase main 后再 merge；不要在 main 上直接解冲突
- **预发失败**：主人拍板回退（`git reset --hard <good-commit>`）或修复后重测；不要把失败代码合入 main
- **tag 错了**：`git tag -d vX.X.X`（本地）+ `git push --delete origin vX.X.X`（远端）+ 重新打；**已经基于错误 tag 引用代码**则需额外补救
- **mock 污染真实数据**（6-19 v2.3.4 踩坑）：**禁止** mock 走 `subprocess.run` 调真实脚本测本地写入——会污染 `data/published/*.json` 等运行时产物。**正确做法**：直接 `import` 真实模块的 `write_record()` 等函数（不走 subprocess），monkey-patch 模块级 `REPO_ROOT` / `PUB_DIR` / `RAW_DIR` 切到临时目录。**典型教训**：v2.3.4 push_report 改造验证时第一版走 subprocess 把 `data/published/daily/2026-06-18.json` 的 doc_url 覆盖成 `https://test.feishu.cn/docx/test`、push_count 2→5、channels 字典被清空——从 `logs/daily/2026-06-18-push.jsonl` 重建关键字段才恢复。
