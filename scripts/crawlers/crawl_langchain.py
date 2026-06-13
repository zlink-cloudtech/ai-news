"""
抓取 LangChain Blog（网页抓取 + JSON-LD）
板块: 💻 编程 Agent
源详情: 资讯源管理/待对接/Agent_LangChain-Blog.md

原计划: 用 RSS（blog.langchain.com/rss/）
现状: 2026-06 验证已不可用（重定向到 www.langchain.com/blog 返回 HTML）
降级方案: 抓 www.langchain.com/blog 列表 + 各详情页 JSON-LD 拿发布时间
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
)

SOURCE = "langchain"
LIST_URL = "https://www.langchain.com/blog"
LOG = get_logger(f"crawl_{SOURCE}")

_SLUG_RE = re.compile(r"^/blog/([a-z0-9][a-z0-9\-]{1,80})$")
_JSONLD_RE = re.compile(
    r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)


def discover_slugs(list_html: str) -> list[str]:
    slugs: set[str] = set()
    for m in re.finditer(r'href="(/blog/[a-z0-9][a-z0-9\-]{1,80})"', list_html):
        href = m.group(1)
        slug = href.split("/")[-1]
        # 排除分类页
        if slug in {"blog", "rss", "feed", "archive", "tag", "category"}:
            continue
        slugs.add(slug)
    return sorted(slugs)


def extract_jsonld(html: str) -> dict | None:
    m = _JSONLD_RE.search(html)
    if not m:
        return None
    raw = m.group(1).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if isinstance(data, list):
        data = data[0] if data else None
    if not isinstance(data, dict):
        return None
    return data


def fetch_one(session, slug: str) -> Item | None:
    url = f"https://www.langchain.com/blog/{slug}"
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
        tags=["langchain", "agent"],
        source=SOURCE,
        extra={"slug": slug, "jsonld_keys": list(ld.keys())},
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="抓取 LangChain Blog（网页抓取）")
    parser.add_argument("--delay", type=float, default=1.0,
                        help="每页抓取间隔秒数（防反爬，默认 1.0）")
    add_date_args(parser)
    args = parser.parse_args()

    session = make_session()

    try:
        list_html = fetch_url(LIST_URL, session=session)
    except Exception as e:
        LOG.error(f"列表页抓取失败: {e}")
        return 1
    slugs = discover_slugs(list_html)
    LOG.info(f"发现 {len(slugs)} 篇博客: {slugs[:5]}... (共 {len(slugs)})")

    items: list[Item] = []
    for slug in slugs:
        it = fetch_one(session, slug)
        if it:
            items.append(it)
        if args.delay > 0:
            time.sleep(args.delay)

    LOG.info(f"成功解析 {len(items)} / {len(slugs)}")

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
            # 显示前 3 篇最新作为参考
            sorted_items = sorted(
                [i for i in items if i.published_ts],
                key=lambda x: -x.published_ts,
            )[:3]
            for it in sorted_items:
                LOG.info(f"  最近: {it.title[:60]} @ {it.published}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
