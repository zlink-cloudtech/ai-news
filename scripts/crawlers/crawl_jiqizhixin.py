"""
抓取 机器之心 (sitemap + REST API)
板块: 中文 AI 学术 + 行业资讯
源详情: 资讯源管理/已对接/资讯_机器之心.md

抓取策略 (D 方案, 2026-06-16 立项):
- 站点 GraphQL `articles/list/category` query 在生产环境被硬切 (HTTP 500),
  旧 query (edges/nodes) 形态全部失效.
- sitemap `/shared/sitemap.xml.gz` (30000+ URL, gzip 250KB) 仍可访问,
  详情页是 SPA 空壳 (无 og:title/meta data) 不能用 trafilatura.
- 实际数据源: REST API `https://www.jiqizhixin.com/api/v1/articles/<slug>`,
  200 OK 返完整 JSON: title/author/published_at/content/cover_image_url/description.

流程:
1. 拉 sitemap (gz) → 解压 → 解析 (URL + lastmod)
2. lastmod 窗口 [target 02:00, target+1 02:00) CST, 跳过凌晨 1:00 索引捎带的老文章
3. URL 标准化: strip "http://www.jiqizhixin.com/" 双前缀
4. 提取 slug, 调 /api/v1/articles/<slug> 拿 JSON
5. published_at 二次过滤 [target 00:00, target+1 00:00) CST
6. 落 data/raw/jiqizhixin/<date>.json (复用 save_items 接口)
7. enrich fulltext (复用 enrich_items / fetch_fulltext)
"""
from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import cloudscraper

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _utils import (
    add_date_args, get_logger, Item, filter_window,
    resolve_dates, save_items, parse_any_datetime,
    enrich_items, CST, REPO_ROOT,
)

SOURCE = "jiqizhixin"
SITEMAP_URL = "https://www.jiqizhixin.com/shared/sitemap.xml.gz"
ARTICLE_API = "https://www.jiqizhixin.com/api/v1/articles"  # + /<slug>
LOG = get_logger(f"crawl_{SOURCE}")

# lastmod 窗口 (相对 target 目标日): [target 02:00, target+1 02:00) CST
# 这个窗口能跳过:
#   - target-1 凌晨 1:00 sitemap 索引捎带的老文章 (lastmod ≈ target-1T01:00:0X, < target 02:00)
#   - target+1 凌晨 1:00 sitemap 索引捎带的老文章 (lastmod ≈ target+1T01:00:0X, < target+1 02:00)
# 只保留 target 当天白天真发 (lastmod 11:00-22:14 范围)
LASTMOD_WINDOW_HOURS_BEFORE = 2   # target 当天 0:00-2:00 也算 (极少数深夜发文)
LASTMOD_WINDOW_HOURS_AFTER = 26   # target+1 02:00 之前

# 限速: 站点不是反爬很严, 设个保守 delay 即可
DEFAULT_DELAY = 0.3


# ---------- URL 标准化 ----------
def normalize_url(raw_url: str) -> str | None:
    """把 sitemap 双 URL 前缀 (http://www.jiqizhixin.com/https://www.jiqizhixin.com/...)
    还原成单 URL; 提取出 /articles/<slug> 部分"""
    u = raw_url.strip()
    # 剥双前缀
    prefix = "http://www.jiqizhixin.com/"
    if u.startswith(prefix):
        rest = u[len(prefix):]
        if rest.startswith("http://") or rest.startswith("https://"):
            u = rest
    if "/articles/" not in u:
        return None
    return u


def extract_slug(url: str) -> str | None:
    """从 https://www.jiqizhixin.com/articles/<slug> 提取 slug"""
    m = re.search(r"/articles/([^/?#]+)", url)
    return m.group(1) if m else None


# ---------- sitemap 解析 ----------
_SITEMAP_URL_RE = re.compile(
    r"<url>\s*<loc>([^<]+)</loc>\s*<lastmod>([^<]+)</lastmod>",
    re.S,
)


def fetch_sitemap(*, retries: int = 2) -> bytes:
    """下载 sitemap gz, 返回原始 bytes"""
    sc = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "linux", "desktop": True},
        delay=0,
    )
    sc.trust_env = False
    last_err: str | None = None
    for attempt in range(retries + 1):
        try:
            r = sc.get(SITEMAP_URL, timeout=60)
            r.raise_for_status()
            return r.content
        except Exception as e:
            last_err = str(e)
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"sitemap 下载失败 ({retries+1} 次): {last_err}")


def parse_sitemap(gz_bytes: bytes) -> list[dict[str, str]]:
    """解压 gz + 解析 URL+lastmod; 返回 [{url, lastmod, slug}, ...]"""
    xml = gzip.decompress(gz_bytes).decode("utf-8", errors="replace")
    out: list[dict[str, str]] = []
    for m in _SITEMAP_URL_RE.finditer(xml):
        raw_url, lastmod = m.group(1), m.group(2)
        norm = normalize_url(raw_url)
        if not norm:
            continue
        slug = extract_slug(norm)
        if not slug:
            continue
        out.append({"url": norm, "lastmod": lastmod, "slug": slug})
    return out


# ---------- API 拉取 ----------
def make_api_session() -> cloudscraper.CloudScraper:
    sc = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "linux", "desktop": True},
        delay=0,
    )
    sc.trust_env = False
    sc.headers.update({
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.jiqizhixin.com/",
    })
    return sc


def fetch_article_json(sc: cloudscraper.CloudScraper, slug: str,
                       *, retries: int = 2) -> dict | None:
    """拉单篇文章 JSON; 失败返 None"""
    url = f"{ARTICLE_API}/{slug}"
    last_err: str | None = None
    for attempt in range(retries + 1):
        try:
            # 不跟重定向: 站点 301/302 经常把没找到的 slug 跳到 /rss 营销页
            r = sc.get(url, timeout=30, allow_redirects=False)
            if r.status_code in (301, 302, 303, 307, 308):
                last_err = f"重定向到 {r.headers.get('Location', '?')[:60]}"
                return None  # 404 / 不可解析的 slug, 直接放弃
            if r.status_code == 404:
                return None
            r.raise_for_status()
            ct = r.headers.get("content-type", "")
            if "json" not in ct.lower():
                last_err = f"非 JSON 响应 ct={ct[:40]}"
                if attempt < retries:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                return None
            return r.json()
        except Exception as e:
            last_err = str(e)
            if attempt < retries:
                time.sleep(0.5 * (attempt + 1))
    LOG.warning(f"  [{slug}] 拉取失败 ({retries+1} 次): {last_err}")
    return None


def parse_published_at(s: str | None) -> datetime | None:
    """API 返回的 published_at 形如 '2026-06-15 18:10:47' (CST, 无时区)"""
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            dt = datetime.strptime(s, fmt).replace(tzinfo=CST)
            return dt.astimezone(timezone.utc)
        except ValueError:
            continue
    return parse_any_datetime(s)


def json_to_item(d: dict, *, source_url: str) -> Item | None:
    """把 API JSON 转成 Item"""
    title = (d.get("title") or "").strip()
    if not title:
        return None
    pub_dt = parse_published_at(d.get("published_at"))
    pub_iso = pub_dt.isoformat() if pub_dt else None
    pub_ts = int(pub_dt.timestamp()) if pub_dt else None
    author = None
    a = d.get("author")
    if isinstance(a, dict):
        author = a.get("name")
    desc = (d.get("description") or "").strip()[:500]
    cover = d.get("cover_image_url")
    # content 是 HTML, 存到 extra 留给 enrich_items 抓全文
    content_html = d.get("content") or ""
    # 从 content 截一段做 summary (无 HTML 标签)
    if content_html and not desc:
        text = re.sub(r"<[^>]+>", " ", content_html)
        text = re.sub(r"\s+", " ", text).strip()
        desc = text[:500]
    item = Item(
        title=title,
        url=source_url,
        published=pub_iso,
        published_ts=pub_ts,
        summary=desc,
        author=author,
        tags=["jiqizhixin"],
        source=SOURCE,
        extra={
            "api": ARTICLE_API,
            "cover_image_url": cover,
            "copyright": d.get("copyright"),
            "content_len": len(content_html),
            "author_id": a.get("id") if isinstance(a, dict) else None,
        },
    )
    return item


# ---------- 抓取主流程 ----------
def crawl_for_date(target_date: str, *, delay: float = DEFAULT_DELAY,
                   ) -> tuple[list[Item], dict[str, str], dict[str, Any]]:
    """抓取 target_date (YYYY-MM-DD) 当天发布的文章

    返回 (matched_items, content_map, meta). 失败返 ([], {}, meta).
    content_map = {slug: content_html} 用于后续 enrich 落 data/articles/.
    """
    # 1. 解析日期 + 窗口
    d = datetime.strptime(target_date, "%Y-%m-%d").date()
    target_start = datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=CST)
    target_end = target_start + timedelta(days=1)
    # lastmod 窗口 [target 02:00 CST, target+1 02:00 CST]
    # 跳过 target-1 凌晨 1:00 sitemap 索引捎带 (lastmod < target 02:00 CST)
    # 跳过 target+1 凌晨 1:00 sitemap 索引捎带 (lastmod < target+1 02:00 CST, 但 < target+1 02:00 时实际是 target+1 1:00 索引, 不在窗口内因 lastmod < target+1 02:00 但 >= target 02:00 — 这部分会被错纳为 "target 真发")
    # 经验: 6-15 真发文章 lastmod 都在 11:00-22:14 范围, 凌晨 1:00 索引捎带的都在 01:00:0X 范围, 窗口 [target 02:00, target+1 02:00) 严格区分
    lastmod_lo = target_start + timedelta(hours=LASTMOD_WINDOW_HOURS_BEFORE)
    lastmod_hi = target_start + timedelta(hours=LASTMOD_WINDOW_HOURS_AFTER)
    lastmod_lo_iso = lastmod_lo.astimezone(timezone.utc).isoformat()
    lastmod_hi_iso = lastmod_hi.astimezone(timezone.utc).isoformat()
    LOG.info(f"目标 {target_date} | lastmod 窗口 [{lastmod_lo_iso}, {lastmod_hi_iso})")

    # 2. 拉 sitemap
    try:
        gz = fetch_sitemap()
    except Exception as e:
        LOG.error(f"sitemap 下载失败: {e}")
        return [], {}, {"error": "sitemap_fetch_failed", "detail": str(e)}

    entries = parse_sitemap(gz)
    LOG.info(f"sitemap 解析: {len(entries)} 条 entry")

    # 3. lastmod 窗口过滤
    candidates: list[dict[str, str]] = []
    for e in entries:
        try:
            lm_dt = datetime.fromisoformat(e["lastmod"])
        except ValueError:
            continue
        if lastmod_lo <= lm_dt < lastmod_hi:
            candidates.append(e)
    LOG.info(f"lastmod 窗口过滤后: {len(candidates)} 候选 slug")

    if not candidates:
        return [], {}, {
            "sitemap_url": SITEMAP_URL,
            "sitemap_entries": len(entries),
            "lastmod_window": [lastmod_lo_iso, lastmod_hi_iso],
            "candidates_after_lastmod": 0,
            "note": "lastmod 窗口无候选 (sitemap 可能未更新到 target 当天)",
        }

    # 去重 slug
    seen: set[str] = set()
    uniq: list[dict[str, str]] = []
    for e in candidates:
        if e["slug"] in seen:
            continue
        seen.add(e["slug"])
        uniq.append(e)
    if len(uniq) < len(candidates):
        LOG.info(f"slug 去重: {len(candidates)} -> {len(uniq)}")
        candidates = uniq

    # 4. 抓 API
    sc = make_api_session()
    items: list[Item] = []
    content_map: dict[str, str] = {}  # slug -> content_html (供后续 enrich 用)
    api_ok = 0
    api_fail = 0
    out_of_window = 0
    parse_fail = 0
    for e in candidates:
        d_json = fetch_article_json(sc, e["slug"])
        if d_json is None:
            api_fail += 1
            if delay > 0:
                time.sleep(delay)
            continue
        api_ok += 1
        # 缓存 content (后续 enrich 用)
        content_map[e["slug"]] = d_json.get("content") or ""
        item = json_to_item(d_json, source_url=e["url"])
        if not item:
            parse_fail += 1
            if delay > 0:
                time.sleep(delay)
            continue
        # 二次过滤 published_at
        if item.published_ts is None:
            out_of_window += 1
            if delay > 0:
                time.sleep(delay)
            continue
        item_dt = datetime.fromtimestamp(item.published_ts, tz=timezone.utc)
        if not (target_start.astimezone(timezone.utc) <= item_dt < target_end.astimezone(timezone.utc)):
            out_of_window += 1
            if delay > 0:
                time.sleep(delay)
            continue
        items.append(item)
        if delay > 0:
            time.sleep(delay)

    LOG.info(
        f"API 抓取汇总: 候选 {len(candidates)} | 200 OK {api_ok} | "
        f"失败 {api_fail} | 解析失败 {parse_fail} | 出窗 {out_of_window} | "
        f"最终匹配 {len(items)}"
    )
    # 排序: published_at 倒序
    items.sort(key=lambda it: (it.published_ts or 0, it.title), reverse=True)

    meta = {
        "sitemap_url": SITEMAP_URL,
        "article_api": ARTICLE_API,
        "lastmod_window": [lastmod_lo_iso, lastmod_hi_iso],
        "window": [
            target_start.astimezone(timezone.utc).isoformat(),
            target_end.astimezone(timezone.utc).isoformat(),
        ],
        "sitemap_entries": len(entries),
        "candidates_after_lastmod": len(candidates),
        "api_ok": api_ok,
        "api_fail": api_fail,
        "out_of_window": out_of_window,
        "parse_fail": parse_fail,
    }
    return items, content_map, meta


def main() -> int:
    parser = argparse.ArgumentParser(description="抓取机器之心（sitemap + REST API）")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY,
                        help="API 请求间隔秒数（默认 0.3）")
    add_date_args(parser)
    args = parser.parse_args()

    for date_str, start, end in resolve_dates(args):
        try:
            items, content_map, meta = crawl_for_date(date_str, delay=args.delay)
        except Exception as e:
            LOG.error(f"[{date_str}] 抓取异常: {e}")
            # 即便异常也写空 json + error meta (避免阻塞下游)
            path = save_items(SOURCE, date_str, [], meta={
                "error": "crawl_exception",
                "detail": str(e),
            })
            LOG.warning(f"[{date_str}] 异常落空 -> {path}")
            continue

        # 落盘 (空也写, 跟 GraphQL 旧版行为一致)
        path = save_items(SOURCE, date_str, items, meta=meta)
        LOG.info(f"[{date_str}] 窗口内 {len(items)} 条 -> {path}")

        # enrich fulltext: 直接用 API content HTML 提纯 markdown, 落 data/articles/jiqizhixin/<slug>.md
        # 避免 _utils.enrich_items 走 fetch_fulltext (对机器之心 SPA 空壳无效)
        if items:
            import trafilatura
            from _utils import save_article
            enriched = 0
            for it in items:
                slug = extract_slug(it.url) or ""
                if not slug:
                    continue
                content_html = content_map.get(slug) or ""
                if not content_html:
                    continue
                try:
                    md = trafilatura.extract(
                        content_html,
                        include_comments=False,
                        include_tables=False,
                        favor_precision=True,
                        with_metadata=False,
                    )
                except Exception as ex:
                    LOG.warning(f"  trafilatura fail [{slug}]: {ex}")
                    continue
                if not md or len(md) < 200:
                    continue
                try:
                    article_path = save_article(SOURCE, slug, md, url=it.url)
                except Exception as ex:
                    LOG.warning(f"  save_article fail [{slug}]: {ex}")
                    continue
                it.extra = dict(it.extra or {})
                it.extra["article_path"] = str(article_path.relative_to(REPO_ROOT))
                it.extra["article_chars"] = len(md)
                it.extra["article_cached"] = False
                enriched += 1
            LOG.info(f"[{date_str}] enrich fulltext: {enriched}/{len(items)} 篇")
            # 重新写一次 raw json, 把 article_path 写回
            save_items(SOURCE, date_str, items, meta=meta)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
