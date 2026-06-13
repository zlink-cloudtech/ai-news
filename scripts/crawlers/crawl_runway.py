"""
抓取 Runway Research (网页抓取 + JSON-LD)
板块: 🧍 数字人
源详情: 资讯源管理/待对接/数字人_Runway-Research.md

策略:
1. 抓 /research 列表页，提取所有 /research/<slug> 链接
2. 对每个 slug 抓详情页，从 JSON-LD 拿 datePublished / headline / description
3. 按"昨日窗口"过滤

反爬注意: 单次会话串行抓取 + User-Agent；如被封可改用 cloudscraper
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _utils import (
    add_date_args, get_logger, Item, fetch_url, filter_window,
    resolve_dates, save_items, make_session, parse_any_datetime,
    CST,
)

SOURCE = "runway"
LIST_URL = "https://runwayml.com/research"
LOG = get_logger(f"crawl_{SOURCE}")

_SLUG_RE = re.compile(r"^/research/([a-z0-9][a-z0-9\-]{1,80})$")
_JSONLD_RE = re.compile(
    r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)


def discover_slugs(list_html: str) -> list[str]:
    """从 /research 列表页提取所有研究 slug"""
    slugs: set[str] = set()
    for m in re.finditer(r'href="(/research/[a-z0-9][a-z0-9\-]{1,80})"', list_html):
        href = m.group(1)
        slug = href.split("/")[-1]
        if slug in {"publications", "rss", "feed"}:
            continue
        slugs.add(slug)
    return sorted(slugs)


def extract_jsonld(html: str) -> dict | None:
    """从详情页提取第一个 JSON-LD 块并解析"""
    m = _JSONLD_RE.search(html)
    if not m:
        return None
    raw = m.group(1).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    # 通常是 list，取第一个
    if isinstance(data, list):
        data = data[0] if data else None
    if not isinstance(data, dict):
        return None
    return data


def fetch_one(session, slug: str) -> Item | None:
    """抓单个 research 详情"""
    url = f"https://runwayml.com/research/{slug}"
    try:
        html = session.get(url, timeout=30).text
    except Exception as e:
        LOG.warning(f"[{slug}] 抓取失败: {e}")
        return None

    ld = extract_jsonld(html)
    if not ld:
        LOG.warning(f"[{slug}] 无 JSON-LD，跳过")
        return None

    headline = (ld.get("headline") or "").strip()
    description = (ld.get("description") or "").strip()
    date_pub = (ld.get("datePublished") or "").strip()
    author = None
    a = ld.get("author")
    if isinstance(a, dict):
        author = a.get("name")
    elif isinstance(a, str):
        author = a

    if not headline or not date_pub:
        LOG.warning(f"[{slug}] 缺 headline/datePublished，跳过")
        return None

    dt = parse_any_datetime(date_pub)
    return Item(
        title=headline,
        url=url,
        published=dt.isoformat() if dt else date_pub,
        published_ts=int(dt.timestamp()) if dt else None,
        summary=description[:500],
        author=author,
        tags=["runway", "video-gen"],
        source=SOURCE,
        extra={"slug": slug, "jsonld_keys": list(ld.keys())},
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="抓取 Runway Research")
    parser.add_argument(
        "--delay", type=float, default=1.0,
        help="每页抓取间隔秒数（防反爬，默认 1.0）",
    )
    add_date_args(parser)
    args = parser.parse_args()

    session = make_session()

    # 1. 拿列表
    try:
        list_html = fetch_url(LIST_URL, session=session)
    except Exception as e:
        LOG.error(f"列表页抓取失败: {e}")
        return 1
    slugs = discover_slugs(list_html)
    LOG.info(f"发现 {len(slugs)} 个研究项: {slugs}")

    # 2. 逐个抓详情
    items: list[Item] = []
    for slug in slugs:
        it = fetch_one(session, slug)
        if it:
            items.append(it)
        if args.delay > 0:
            time.sleep(args.delay)

    LOG.info(f"成功解析 {len(items)} / {len(slugs)}")

    # 3. 窗口过滤 + 落盘
    for date_str, start, end in resolve_dates(args):
        in_win = filter_window(items, start, end)
        path = save_items(SOURCE, date_str, in_win,
                          meta={
                              "list_url": LIST_URL,
                              "total_discovered": len(slugs),
                              "total_parsed": len(items),
                              "window": [start.isoformat(), end.isoformat()],
                          })
        LOG.info(f"[{date_str}] 窗口内 {len(in_win)} 条 -> {path}")
        if not in_win and items:
            LOG.info(f"  窗口内无新发布；最近一条: {items[0].title} @ {items[0].published}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
