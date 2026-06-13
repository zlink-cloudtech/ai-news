"""
抓取 GitHub Trending (via RSSHub)
板块: 💻 编程 Agent（横跨 LLM/数字人）
源详情: 资讯源管理/待对接/Agent_GitHub-Trending.md

前置条件: RSSHub 已部署（默认 http://localhost:1200）
可通过环境变量 RSSHUB_BASE 覆盖，例如 https://rsshub.example.com
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _utils import (
    add_date_args, get_logger, parse_rss, filter_window,
    resolve_dates, save_items,
)

SOURCE = "github_trending"
RSSHUB_BASE = os.environ.get("RSSHUB_BASE", "http://localhost:1200").rstrip("/")

# 关注的语言（AI/ML 领域最相关）
LANGUAGES = ["python", "typescript", "jupyter notebook", "go"]
LOG = get_logger(f"crawl_{SOURCE}")


def build_urls() -> list[tuple[str, str]]:
    """返回 [(语言, URL), ...]"""
    urls = []
    for lang in LANGUAGES:
        # /github/trending/daily/<lang>?since=daily
        url = f"{RSSHUB_BASE}/github/trending/daily/{lang.replace(' ', '%20')}"
        urls.append((lang, url))
    return urls


def main() -> int:
    parser = argparse.ArgumentParser(description="抓取 GitHub Trending (via RSSHub)")
    add_date_args(parser)
    args = parser.parse_args()

    # 健康检查 RSSHub
    try:
        import requests
        r = requests.get(f"{RSSHUB_BASE}/", timeout=5)
        r.raise_for_status()
    except Exception as e:
        LOG.error(f"RSSHub 不可达 ({RSSHUB_BASE}): {e}")
        LOG.error("请先部署 RSSHub: docker run -d -p 1200:1200 diygod/rsshub:latest")
        return 2

    all_items = []
    for lang, url in build_urls():
        try:
            items = parse_rss(url, source=f"{SOURCE}:{lang}")
            LOG.info(f"[{lang}] RSSHub 返回 {len(items)} 条")
            # 注入语言标签
            for it in items:
                it.tags.append(f"lang:{lang}")
                it.source = SOURCE  # 统一 source 字段
            all_items.extend(items)
        except Exception as e:
            LOG.warning(f"[{lang}] 抓取失败: {e}")

    # 按 (url, title) 去重（同一仓库可能被多种语言/多个 span 抓取）
    seen = set()
    deduped = []
    for it in all_items:
        key = (it.url, it.title)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(it)
    LOG.info(f"去重后: {len(deduped)} 条")

    for date_str, start, end in resolve_dates(args):
        in_win = filter_window(deduped, start, end)
        path = save_items(SOURCE, date_str, in_win,
                          meta={
                              "rsshub_base": RSSHUB_BASE,
                              "languages": LANGUAGES,
                              "window": [start.isoformat(), end.isoformat()],
                          })
        LOG.info(f"[{date_str}] 窗口内 {len(in_win)} 条 -> {path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
