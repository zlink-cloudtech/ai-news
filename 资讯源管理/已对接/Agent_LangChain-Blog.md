# LangChain Blog

## 基本信息
- **板块**：💻 编程 Agent
- **URL**：https://www.langchain.com/blog
- **抓取方式**：L5 网页抓取（BeautifulSoup + JSON-LD）
- **更新频率**：1-3 篇/周
- **内容覆盖**：LangChain / LangGraph / DeepAgents / LangSmith 实战内容

## 接入状态
- **状态**：🟡 待对接
- **优先级**：P0
- **首期目标**：✅ 是
- **负责人**：AI 调研专家
- **创建日期**：2026-06-13
- **接入日期**：2026-06-13（已 L5 抓取跑通）
- **实际抓取脚本**：`scripts/crawlers/crawl_langchain.py`

## 历史变更
- 2026-06-13：原计划走 RSS（`blog.langchain.com/rss/`），实测已重定向到 `www.langchain.com/blog` 返回 SPA HTML，无 RSS 输出。降级为网页抓取 + JSON-LD 拿发布时间（与 Runway 一致方案）。

## 接入计划
- [x] 验证 RSS 路径 → 不可用
- [x] 抓取脚本开发（BeautifulSoup + JSON-LD）
- [x] "昨日窗口"过滤
- [x] 内容质量验证（端到端跑通，14 篇解析成功）
- [ ] 7 天稳定性验证

## 抓取示例

```python
# 见 scripts/crawlers/crawl_langchain.py
import requests
from bs4 import BeautifulSoup
r = requests.get('https://www.langchain.com/blog', headers={'User-Agent': '...'})
slugs = re.findall(r'href="(/blog/[a-z0-9\-]+)"', r.text)
# 再抓每个详情页，从 JSON-LD 拿 datePublished
```

## 备注
- 编程 Agent 板块"性价比最高"的单一源（覆盖 LangChain 全家桶）
- 列表页 ~14 篇；详情页 JSON-LD 含 datePublished / headline / description
- 注意：抓取频率 ≤ 1次/日即可（避免反爬）
