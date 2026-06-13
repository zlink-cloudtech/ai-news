"""
抓取 机器之心 (GraphQL)
板块: 中文 AI 学术 + 行业资讯
源详情: 资讯源管理/已对接/资讯_机器之心.md

抓取策略:
- 站点是 SPA，无公开 RSS。已验证 `/rss` 返回 HTML 营销页。
- 实际数据源: GraphQL endpoint `https://www.jiqizhixin.com/graphql`
- 必须带两个头:
  1. `X-CSRF-Token`: 从首页 `<meta name="csrf-token">` 提取
  2. `X-Requested-With: XMLHttpRequest` （否则返回 HTML 而非 JSON）
- 文章列表: `articles(first: N, after: cursor)` 拿元信息（含 published_at）
- 文章全文: `node(id: "Article/<uuid>") { ... on Article { content simple_content } }`
  （用 node() 而非 article(id:)，因 article() 在带斜杠的 ID 上有兼容问题）
- 串行抓全文（默认 delay 1.0s）
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import cloudscraper

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _utils import (
    add_date_args, get_logger, Item, filter_window,
    resolve_dates, save_items, parse_any_datetime,
    enrich_items, CST,
)

SOURCE = "jiqizhixin"
GRAPHQL_URL = "https://www.jiqizhixin.com/graphql"
HOMEPAGE_URL = "https://www.jiqizhixin.com/"
LOG = get_logger(f"crawl_{SOURCE}")

# 列表查询：拿 30 条
LIST_QUERY = """
{
  articles(first: 30) {
    edges {
      node {
        id
        title
        path
        published_at
        description
        cover_image_url
        category_name
        author { name }
      }
      cursor
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

# 详情查询：拿全文（用 node() 全局查询兼容 ID 中的斜杠）
DETAIL_QUERY_TEMPLATE = """
{{
  node(id: "{node_id}") {{
    id
    ... on Article {{
      title
      path
      published_at
      description
      simple_content
      content
      foreword
    }}
  }}
}}
"""


def make_session() -> cloudscraper.CloudScraper:
    """构造带 CSRF + XHR 头的 cloudscraper session"""
    sc = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "linux", "desktop": True},
    )
    sc.trust_env = False
    # 1. 先拿 CSRF token
    r = sc.get(HOMEPAGE_URL, timeout=30)
    m = re.search(r'<meta name="csrf-token" content="([^"]+)"', r.text)
    csrf = m.group(1) if m else ""
    sc.headers.update({
        "X-CSRF-Token": csrf,
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Origin": "https://www.jiqizhixin.com",
        "Referer": "https://www.jiqizhixin.com/",
    })
    return sc


def gql_query(sc: cloudscraper.CloudScraper, query: str,
              variables: dict | None = None, *, retries: int = 2) -> dict:
    payload: dict[str, Any] = {"query": query}
    if variables:
        payload["variables"] = variables
    last_err: str | None = None
    for attempt in range(retries + 1):
        try:
            r = sc.post(GRAPHQL_URL, json=payload, timeout=30)
            r.raise_for_status()
            data = r.json()
            if "errors" in data:
                last_err = f"GraphQL errors: {data['errors']}"
                if attempt < retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
            return data
        except Exception as e:
            last_err = f"HTTP error: {e}"
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise
    return {"data": None, "errors": last_err}


def fetch_list(sc) -> list[Item]:
    """拉取首页 30 条文章（按 published_at 倒序）"""
    data = gql_query(sc, LIST_QUERY)
    if not data.get("data") or not data["data"].get("articles"):
        LOG.warning(f"列表为空: {data}")
        return []
    items: list[Item] = []
    for edge in data["data"]["articles"]["edges"]:
        n = edge["node"]
        node_id = n["id"]  # e.g. "Article/<uuid>"
        path = n.get("path") or ""
        url = urljoin("https://www.jiqizhixin.com", path) if path else ""
        title = (n.get("title") or "").strip()
        if not title or not url:
            continue
        pub_str = (n.get("published_at") or "").strip()  # "2026/06/13 11:19"
        # 解析为 ISO（按 UTC+8 当成 CST）
        pub_dt: datetime | None = None
        if pub_str:
            try:
                # 2026/06/13 11:19
                pub_dt = datetime.strptime(pub_str, "%Y/%m/%d %H:%M").replace(tzinfo=CST)
                pub_dt = pub_dt.astimezone(timezone.utc)
            except ValueError:
                pub_dt = parse_any_datetime(pub_str)
        author = None
        a = n.get("author")
        if isinstance(a, dict):
            author = a.get("name")
        cat = n.get("category_name") or ""
        desc = (n.get("description") or "").strip()[:500]
        items.append(Item(
            title=title,
            url=url,
            published=pub_dt.isoformat() if pub_dt else pub_str or None,
            published_ts=int(pub_dt.timestamp()) if pub_dt else None,
            summary=desc,
            author=author,
            tags=["jiqizhixin", cat] if cat else ["jiqizhixin"],
            source=SOURCE,
            extra={
                "node_id": node_id,
                "category": cat,
                "cover_image_url": n.get("cover_image_url"),
            },
        ))
    LOG.info(f"列表解析: {len(items)} 条")
    return items


def fetch_content(sc, node_id: str) -> dict | None:
    """拉取单篇文章全文，返回 {content_html, simple_content, foreword, description} 或 None"""
    query = DETAIL_QUERY_TEMPLATE.format(node_id=node_id)
    data = gql_query(sc, query)
    if not data.get("data") or not data["data"].get("node"):
        LOG.debug(f"[{node_id}] 详情为空")
        return None
    return data["data"]["node"]


def main() -> int:
    parser = argparse.ArgumentParser(description="抓取机器之心（中文 AI 学术/行业，GraphQL）")
    parser.add_argument("--delay", type=float, default=1.0,
                        help="详情页抓取间隔秒数（防反爬，默认 1.0）")
    add_date_args(parser)
    args = parser.parse_args()

    # 1. 构造带 CSRF 的 session + 拉列表
    try:
        sc = make_session()
    except Exception as e:
        LOG.error(f"初始化 session 失败: {e}")
        return 1

    try:
        items = fetch_list(sc)
    except Exception as e:
        LOG.error(f"列表拉取失败: {e}")
        return 1

    if not items:
        LOG.warning("列表为空，本次无输出")
        # 即便为空也要按日期落空 json（避免阻塞下游）
        for date_str, start, end in resolve_dates(args):
            save_items(SOURCE, date_str, [], meta={
                "graphql": GRAPHQL_URL,
                "window": [start.isoformat(), end.isoformat()],
                "note": "list returned 0 items",
            })
        return 0

    # 2. 窗口过滤
    # 机器之心默认拉首页 30 条，覆盖前 2-3 天足够；不再下钻分页
    # 先按窗口过滤，对窗口内文章再拉详情
    matched: dict[str, list[Item]] = {}
    for date_str, start, end in resolve_dates(args):
        in_win = filter_window(items, start, end)
        if in_win:
            matched[date_str] = in_win

    # 3. 对所有匹配项去重后再统一拉详情（避免重复请求）
    seen_ids: set[str] = set()
    to_fetch: list[Item] = []
    for items_in_day in matched.values():
        for it in items_in_day:
            node_id = it.extra.get("node_id") if isinstance(it.extra, dict) else None
            if node_id and node_id not in seen_ids:
                seen_ids.add(node_id)
                to_fetch.append(it)

    if to_fetch and not getattr(args, "no_article", False):
        LOG.info(f"准备拉 {len(to_fetch)} 篇详情（窗口内去重后）")
        for it in to_fetch:
            node_id = it.extra.get("node_id")
            try:
                detail = fetch_content(sc, node_id)
            except Exception as e:
                LOG.warning(f"详情拉取失败 [{it.url}]: {e}")
                if args.delay > 0:
                    time.sleep(args.delay)
                continue
            if not detail:
                if args.delay > 0:
                    time.sleep(args.delay)
                continue
            # 拼装 summary：优先 description（更精炼） -> foreword -> simple_content -> content 截前 800
            # 机器之心的 description 经常为空，所以从多个字段中选最长且非空的
            desc = (detail.get("description") or "").strip()
            fore = (detail.get("foreword") or "").strip()
            simple = (detail.get("simple_content") or "").strip()
            content_html = (detail.get("content") or "").strip()
            # 选最长有内容的字段，清洗 HTML
            candidates = []
            if desc:
                candidates.append(("description", desc))
            if fore:
                candidates.append(("foreword", fore))
            if simple:
                candidates.append(("simple_content", simple))
            if content_html:
                candidates.append(("content", content_html))
            if candidates:
                # 优先用 description，再简单，最后 content
                # 取最长非空字段，并清洗 HTML
                pick_name, pick_html = max(candidates, key=lambda x: len(x[1]))
                text = re.sub(r"<[^>]+>", " ", pick_html)
                text = re.sub(r"\s+", " ", text).strip()
                it.summary = text[:800]
            else:
                it.summary = desc[:500]
            # 标记详情已抓
            it.extra = dict(it.extra or {})
            it.extra["detail_fetched"] = True
            it.extra["has_full_content"] = bool(detail.get("content"))
            if args.delay > 0:
                time.sleep(args.delay)

    # 4. 落盘
    for date_str, start, end in resolve_dates(args):
        in_win = matched.get(date_str, [])
        path = save_items(SOURCE, date_str, in_win, meta={
            "graphql": GRAPHQL_URL,
            "window": [start.isoformat(), end.isoformat()],
            "list_total": len(items),
            "detail_fetched": sum(
                1 for it in in_win
                if isinstance(it.extra, dict) and it.extra.get("detail_fetched")
            ),
        })
        LOG.info(f"[{date_str}] 窗口内 {len(in_win)} 条 -> {path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
