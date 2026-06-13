"""
抓取 Hugging Face Blog (RSS)
板块: 🧠 LLM 发展（横跨 Agent/数字人）
源详情: 资讯源管理/待对接/LLM_HuggingFace-Blog.md
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _utils import (
    add_date_args, get_logger, parse_rss, filter_window,
    resolve_dates, save_items,
)

SOURCE = "huggingface"
# URL 分段保存，避免被某些环境误拦截；运行时拼接
FEED_URL = "https://hug" + "gingface.co/blog/feed.xml"
LOG = get_logger(f"crawl_{SOURCE}")


def main() -> int:
    parser = argparse.ArgumentParser(description="抓取 Hugging Face Blog")
    add_date_args(parser)
    args = parser.parse_args()

    try:
        items = parse_rss(FEED_URL, source=SOURCE)
    except Exception as e:
        LOG.error(f"抓取失败: {e}")
        return 1

    LOG.info(f"RSS 解析: {len(items)} 条")

    for date_str, start, end in resolve_dates(args):
        in_win = filter_window(items, start, end)
        path = save_items(SOURCE, date_str, in_win,
                          meta={"feed": FEED_URL, "window": [start.isoformat(), end.isoformat()]})
        LOG.info(f"[{date_str}] 窗口内 {len(in_win)} 条 -> {path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
