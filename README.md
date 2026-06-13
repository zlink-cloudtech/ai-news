# AI 资讯追踪仓库

> 每天 8:30 自动汇总"昨日"全球 AI 资讯，按话题板块（LLM 发展 / 编程 Agent / 数字人）整理 Top 10，每周做汇总。
> 由 **AI 调研专家** 维护，**王鸿奇** 终审。

## 目录结构

```
AI资讯追踪/
├── README.md                  # 本文件
├── AUTO_PUSH.md               # 自动 push 机制说明
├── 汇报方式.md                # 资讯展示和推送方式
├── 模板/                      # 模板文件
│   ├── 每日资讯模板.md
│   └── 每周汇总模板.md
├── 每日资讯/                  # 按日期归档的每日资讯
│   └── YYYY-MM-DD.md
├── 每周汇总/                  # 按周归档的每周汇总
│   └── YYYY-WXX.md
├── 资讯源管理/                # 信息源管理系统
│   ├── README.md
│   ├── 源清单.md
│   ├── 已对接/
│   ├── 待对接/
│   ├── 待预研/
│   └── 已废弃/
├── scripts/                   # 工具脚本
│   ├── news-commit.sh         # 一键 commit + push
│   ├── crawlers/              # 信息源抓取脚本（首期 7 源）
│   │   ├── README.md
│   │   ├── _utils.py
│   │   ├── crawl_openai.py
│   │   ├── crawl_deepmind.py
│   │   ├── crawl_huggingface.py
│   │   ├── crawl_langchain.py
│   │   ├── crawl_github_trending.py
│   │   ├── crawl_replicate.py
│   │   ├── crawl_runway.py
│   │   └── run_all.py
│   └── generators/            # 资讯生成器（读抓取数据 → 输出 Markdown）
│       ├── README.md
│       ├── _utils_gen.py
│       └── generate_daily.py
├── data/                      # 抓取数据（JSON）
│   └── raw/<source>/YYYY-MM-DD.json
└── .gitignore / .gitattributes
```

## 自动化约定

| 任务 | 触发时间 | 抓取范围 | 推送方式 |
|------|---------|---------|---------|
| 每日资讯 | 每天 8:30（CST） | 昨日 00:00 - 23:59 | 自动 commit + push |
| 每周汇总 | 每周日早 8:30（CST） | 本周一 - 周日 | 自动 commit + push |

## 话题板块（可扩展）

- 🧠 LLM 发展
- 💻 编程 Agent
- 🧍 数字人
- 📦 其他（可扩展）

每板块每期 **Top 10**（按"重要度 + 时新性 + 影响力"综合排序）。

## 远程仓库

- **HTTPS**：https://github.com/zlink-cloudtech/ai-news.git
- **组织**：zlink-cloudtech
- **凭据**：PAT 已嵌入 remote URL（细粒度权限）

## 关键技术点

- **Git 仓库 + 自动 push**：post-commit hook 触发 push
- **Git LFS**：管理图片/视频/文档等二进制资源
- **模板驱动**：所有资讯/汇总都按统一模板生成
- **源管理**：所有信息源在 `资讯源管理/` 集中管理、状态追踪
- **抓取脚本**：`scripts/crawlers/` 提供 7 源抓取，统一数据落 `data/raw/`

## 快速开始

查看最新每日资讯：
```bash
# 浏览器
open https://github.com/zlink-cloudtech/ai-news/tree/main/每日资讯

# git 拉取
git pull origin main
ls 每日资讯/
```

手动触发抓取（默认 5 源；加 --with-hf/--with-github 启用受限源）：
```bash
pip install feedparser trafilatura cloudscraper beautifulsoup4 lxml html2text
python3 scripts/crawlers/run_all.py
```

手动提交变更：
```bash
./scripts/news-commit.sh "feat(每日资讯): 2026-06-13"
```

## 维护

由 AI 调研专家 自动维护。
