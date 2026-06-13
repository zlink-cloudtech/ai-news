"""
抓取 Replicate Blog (RSS)
板块: 🧍 数字人
源详情: 资讯源管理/已对接/数字人_Replicate-Blog.md

注意: Replicate 实际 RSS 路径是 /blog/rss（不是 .xml）
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _utils import (
    add_date_args, get_logger, parse_rss, filter_window,
    resolve_dates, save_items, enrich_items,
)

SOURCE = "replicate"
FEED_URL = "https://replicate.com/blog/rss"
LOG = get_logger(f"crawl_{SOURCE}")


def main() -> int:
    parser = argparse.ArgumentParser(description="抓取 Replicate Blog")
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
        if in_win and not getattr(args, "no_article", False):
            in_win = enrich_items(in_win, SOURCE, log=LOG, delay=0.5)
        path = save_items(SOURCE, date_str, in_win,
                          meta={"feed": FEED_URL, "window": [start.isoformat(), end.isoformat()]})
        LOG.info(f"[{date_str}] 窗口内 {len(in_win)} 条 -> {path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
