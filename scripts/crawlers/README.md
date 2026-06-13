# AI 资讯抓取脚本（scripts/crawlers/）

## 设计目标

为「AI 资讯追踪」系统提供稳定的源数据采集能力，覆盖首期 7 个信息源，支持：
- **昨日窗口**（默认 00:00-23:59 Asia/Shanghai）过滤
- **统一数据格式**（`data/raw/<source>/<date>.json`）
- **可重入**（同一日期多次抓取会覆盖）
- **可调试**（`--all` 模式忽略窗口返回最新条目）

## 目录结构

```
scripts/crawlers/
├── __init__.py
├── _utils.py                  # 共用：HTTP / RSS 解析 / 日期窗口 / 落盘
├── crawl_openai.py            # OpenAI Blog (RSS)              🧠 LLM
├── crawl_deepmind.py          # DeepMind Blog (RSS)            🧠 LLM
├── crawl_huggingface.py       # HuggingFace Blog (RSS)         🧠 LLM*
├── crawl_langchain.py         # LangChain Blog (网页+JSON-LD)  💻 Agent
├── crawl_github_trending.py   # GitHub Trending (RSSHub)       💻 Agent**
├── crawl_replicate.py         # Replicate Blog (RSS)           🧍 数字人
├── crawl_runway.py            # Runway Research (网页+JSON-LD) 🧍 数字人
├── run_all.py                 # 一键跑全部入口
└── tests/
    └── test_hf.py             # HF 网络诊断（仅本机调试用）
```

> \* HuggingFace 在本机网络环境可能因 DNS 污染无法访问，部署到正确网络即可
> \** GitHub Trending 需要 RSSHub 已部署（见下面"前置条件"）

## 前置条件

### Python 依赖

```bash
pip install feedparser trafilatura cloudscraper beautifulsoup4 lxml html2text requests
```

### RSSHub（仅 github_trending 源需要）

```bash
docker run -d --name rsshub -p 1200:1200 diygod/rsshub:latest
# 验证
curl http://localhost:1200/

# 远程实例可通过环境变量覆盖
export RSSHUB_BASE=https://rsshub.example.com
```

## 快速开始

### 抓取昨日（默认）

```bash
# 单源
python3 scripts/crawlers/crawl_openai.py

# 默认 5 源（除 GitHub Trending 和 HuggingFace）
python3 scripts/crawlers/run_all.py

# 加上 HuggingFace（本机网络不可达时脚本会失败）
python3 scripts/crawlers/run_all.py --with-hf

# 加上 GitHub Trending（需 RSSHub）
python3 scripts/crawlers/run_all.py --with-github

# 全部 7 源
python3 scripts/crawlers/run_all.py --with-hf --with-github
```

### 指定日期 / 回溯

```bash
# 指定日期
python3 scripts/crawlers/crawl_openai.py --date 2026-06-12

# 回溯 N 天（昨日 + 前 N-1 天）
python3 scripts/crawlers/run_all.py --days 3
```

### 调试（忽略窗口）

```bash
python3 scripts/crawlers/crawl_openai.py --all
```

## 输出格式

每个源每天一个 JSON 文件：`data/raw/<source>/<date>.json`

```json
{
  "source": "openai",
  "date": "2026-06-12",
  "saved_at": "2026-06-13T12:55:26+08:00",
  "count": 2,
  "meta": {
    "feed": "https://openai.com/blog/rss.xml",
    "window": ["2026-06-12T00:00:00+08:00", "2026-06-12T23:59:59.999999+08:00"]
  },
  "items": [
    {
      "title": "...",
      "url": "...",
      "published": "2026-06-12T10:00:00+00:00",
      "published_ts": 1781258400,
      "summary": "...",
      "author": null,
      "tags": ["AI Adoption"],
      "source": "openai",
      "extra": {},
      "fetched_at": "2026-06-13T12:55:26+08:00"
    }
  ]
}
```

## 通用参数

所有脚本支持：

| 参数 | 说明 | 默认 |
|------|------|------|
| `--date YYYY-MM-DD` | 目标日期 | 昨日 |
| `--days N` | 回溯天数（0=只昨日；>0=昨日+前 N 天） | 0 |
| `--all` | 忽略窗口，返回 feed 全部最新 | False |

`run_all.py` 额外支持：

| 参数 | 说明 |
|------|------|
| `--with-github` | 包含 GitHub Trending（需 RSSHub） |
| `--with-hf` | 包含 HuggingFace（本机网络可能不通） |
| `--source <name>` | 只跑指定源（可多次传） |

## 时区与窗口

- **默认时区**：`Asia/Shanghai` (CST)
- **窗口**：`昨日 00:00:00` ~ `昨日 23:59:59.999999`（带 ±1h 容差）
- **来源时区**：RSS 的 `pubDate` 通常是 UTC，工具内部已转 UTC 再与窗口比较

## 反爬策略

- **RSS 源**（OpenAI / DeepMind / HF / Replicate）：单次 GET，无特殊反爬，正常 UA 即可
- **网页抓取**（LangChain / Runway）：BeautifulSoup + 串行抓取 + 1.0s 间隔（`--delay` 可调）
- **如触发反爬**：改用 `cloudscraper` 库（已安装），或减小 `--delay` 后的并发

## 添加新源

1. 在 `资讯源管理/待对接/` 下创建详情文件
2. 在 `scripts/crawlers/` 下创建 `crawl_<source>.py`（参考现有脚本）
3. 复用 `_utils.py` 的 `parse_rss` / `fetch_url` / `save_items`
4. 在 `run_all.py` 的 `DEFAULT_SOURCES` 或 `OPTIONAL_SOURCES` 中加入
5. 验证 → 提交 git

## 端到端验证记录（2026-06-13）

| 源 | 状态 | 备注 |
|----|------|------|
| OpenAI | ✅ | RSS 200, 1005 条，昨日 2 条 |
| DeepMind | ✅ | RSS 200, 100 条，前日 1 条 |
| HuggingFace | ⏸️ | 脚本就绪，本机 DNS 污染 |
| LangChain | ✅ | 14 篇全部解析，昨日 2 条 |
| GitHub Trending | ⏸️ | 等待 RSSHub 部署 |
| Replicate | ✅ | RSS 200, 122 条（URL 已修正为 `/blog/rss`） |
| Runway | ✅ | 7 项全部解析（更新频率低，窗口内 0 条符合预期） |

## 常见问题

**Q: 同一日期重复抓取会怎样？**
A: 覆盖。文件名固定，文件内容整体替换。建议首次抓取后立即推进"生成资讯"步骤。

**Q: --days 3 会生成几个文件？**
A: 3 个（昨日 + 前 1 天 + 前 2 天）。

**Q: 怎么把数据同步到 GitHub？**
A: 用 `scripts/news-commit.sh`（已有 hook 自动 push），或：
```bash
git add data/raw/ && git commit -m "feat(每日资讯): $(date +%Y-%m-%d)"
```
