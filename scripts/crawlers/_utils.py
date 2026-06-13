"""
共用工具：HTTP 抓取 / RSS 解析 / 日期窗口 / 落盘 / 全文抓取
所有抓取脚本均依赖本模块。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import feedparser
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ---------- 路径常量 ----------
REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "data" / "raw"
ARTICLES_DIR = REPO_ROOT / "data" / "articles"
LOG_DIR = REPO_ROOT / "logs"
DATA_DIR.mkdir(parents=True, exist_ok=True)
ARTICLES_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

# 上海时区
CST = ZoneInfo("Asia/Shanghai")
UTC = timezone.utc

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36 "
    "(AI-News-Tracker/0.1; +https://github.com/zlink-cloudtech/ai-news)"
)

# ---------- 日志 ----------
def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        fmt = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        logger.addHandler(sh)
        # 每日一个日志文件
        log_file = LOG_DIR / f"{name}.log"
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    return logger


# ---------- HTTP 客户端 ----------
def make_session(retries: int = 3, backoff: float = 0.5) -> requests.Session:
    """带自动重试的 HTTP Session"""
    s = requests.Session()
    retry = Retry(
        total=retries,
        backoff_factor=backoff,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "HEAD"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers.update({
        "User-Agent": DEFAULT_UA,
        "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
    })
    return s


def fetch_url(url: str, *, session: requests.Session | None = None,
              timeout: int = 30, allow_redirects: bool = True) -> str:
    """GET 一个 URL，返回 text；失败抛异常。"""
    s = session or make_session()
    resp = s.get(url, timeout=timeout, allow_redirects=allow_redirects)
    resp.raise_for_status()
    # 编码猜测
    if resp.encoding and resp.encoding.lower() == "iso-8859-1":
        resp.encoding = resp.apparent_encoding or "utf-8"
    return resp.text


# ---------- 日期窗口 ----------
def yesterday_window(tz: ZoneInfo = CST) -> tuple[datetime, datetime]:
    """返回昨日 00:00:00 到 23:59:59.999999（默认上海时区）"""
    now = datetime.now(tz)
    y = (now - timedelta(days=1)).date()
    start = datetime(y.year, y.month, y.day, 0, 0, 0, tzinfo=tz)
    end = datetime(y.year, y.month, y.day, 23, 59, 59, 999999, tzinfo=tz)
    return start, end


def parse_any_datetime(s: str | None) -> datetime | None:
    """尝试把各种时间字符串解析为带时区的 datetime"""
    if not s:
        return None
    s = s.strip()
    # RFC 2822（RSS 最常用）
    try:
        dt = parsedate_to_datetime(s)
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt.astimezone(UTC)
    except Exception:
        pass
    # ISO 8601
    try:
        # 兼容 "2026-06-12T10:00:00.000Z" / "2026-06-12T10:00:00+00:00"
        s2 = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s2)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except Exception:
        pass
    return None


def in_window(dt: datetime | None, start: datetime, end: datetime,
              tolerance_hours: int = 1) -> bool:
    """是否在 [start, end] 窗口内（带 ±1h 容差）"""
    if dt is None:
        return False
    dt_utc = dt.astimezone(UTC)
    start_utc = start.astimezone(UTC) - timedelta(hours=tolerance_hours)
    end_utc = end.astimezone(UTC) + timedelta(hours=tolerance_hours)
    return start_utc <= dt_utc <= end_utc


# ---------- 数据结构 ----------
@dataclass
class Item:
    title: str
    url: str
    published: str | None = None           # ISO 8601 字符串
    published_ts: int | None = None         # epoch seconds
    summary: str = ""
    author: str | None = None
    tags: list[str] = field(default_factory=list)
    source: str = ""
    extra: dict[str, Any] = field(default_factory=dict)
    fetched_at: str = field(
        default_factory=lambda: datetime.now(CST).isoformat(timespec="seconds")
    )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------- RSS 解析 ----------
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def clean_html(s: str | None, max_len: int = 600) -> str:
    """去除 HTML 标签 + 合并空白 + 截断"""
    if not s:
        return ""
    s = _HTML_TAG_RE.sub(" ", s)
    s = s.replace("&nbsp;", " ").replace("&amp;", "&") \
         .replace("&lt;", "<").replace("&gt;", ">") \
         .replace("&quot;", '"').replace("&#39;", "'")
    s = _WS_RE.sub(" ", s).strip()
    if len(s) > max_len:
        s = s[: max_len - 1] + "…"
    return s


def parse_rss(url: str, *, source: str, limit: int | None = None) -> list[Item]:
    """解析 RSS / Atom feed，标准化为 Item 列表"""
    text = fetch_url(url)
    feed = feedparser.parse(text)
    if feed.bozo and not feed.entries:
        raise RuntimeError(f"RSS 解析失败: {url} -> {feed.bozo_exception}")
    items: list[Item] = []
    for e in feed.entries[:limit] if limit else feed.entries:
        link = (e.get("link") or "").strip()
        title = (e.get("title") or "").strip()
        if not link or not title:
            continue
        pub_dt = parse_any_datetime(e.get("published") or e.get("updated"))
        # 兜底：部分非标准 RSS（如 36kr "2026-06-13 16:37:21  +0800"）无法被
        # parsedate_to_datetime 解析，但 feedparser 通常能拿到 published_parsed
        # 注意：feedparser 已把时间转 UTC，struct_time 里的小时是 UTC 小时，
        # 必须用 calendar.timegm（按 UTC 解释）而不是 mktime（按本地时间）
        if pub_dt is None and e.get("published_parsed"):
            try:
                import calendar
                ts = calendar.timegm(e["published_parsed"])
                pub_dt = datetime.fromtimestamp(ts, tz=UTC)
            except Exception:
                pass
        summary = clean_html(
            e.get("summary") or e.get("description") or ""
        )
        author = (e.get("author") or "").strip() or None
        tags = [t.get("term", "").strip() for t in (e.get("tags") or []) if t.get("term")]
        items.append(Item(
            title=title,
            url=link,
            published=pub_dt.isoformat() if pub_dt else None,
            published_ts=int(pub_dt.timestamp()) if pub_dt else None,
            summary=summary,
            author=author,
            tags=tags,
            source=source,
        ))
    return items


def filter_window(items: Iterable[Item], start: datetime, end: datetime,
                  tolerance_hours: int = 1) -> list[Item]:
    """按发布时间窗口过滤"""
    out: list[Item] = []
    for it in items:
        if not it.published_ts:
            continue
        dt = datetime.fromtimestamp(it.published_ts, tz=UTC)
        if in_window(dt, start, end, tolerance_hours):
            out.append(it)
    return out


# ---------- 落盘 ----------
def save_items(source: str, date: str, items: list[Item], *,
               meta: dict[str, Any] | None = None) -> Path:
    """保存为 data/raw/<source>/<date>.json"""
    out_dir = DATA_DIR / source
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{date}.json"
    payload = {
        "source": source,
        "date": date,
        "saved_at": datetime.now(CST).isoformat(timespec="seconds"),
        "count": len(items),
        "meta": meta or {},
        "items": [it.to_dict() for it in items],
    }
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return out_path


# ---------- 全文抓取（落盘为 Markdown） ----------
_SLUG_FALLBACK_LEN = 16
_SAFE_SLUG_RE = re.compile(r"[^a-z0-9\-]+")


def slug_from_url(url: str, *, fallback_slug: str | None = None) -> str:
    """从 URL 提取安全的 slug；fallback 用 md5(url)[:16]"""
    if fallback_slug:
        s = fallback_slug.lower()
    else:
        parsed = urlparse(url or "")
        parts = [p for p in parsed.path.strip("/").split("/") if p]
        s = parts[-1].lower() if parts else ""
        # 去扩展名
        s = re.sub(r"\.[a-z0-9]{1,5}$", "", s)
    # 保留 [a-z0-9-]
    s = _SAFE_SLUG_RE.sub("-", s).strip("-")
    # 限长 80
    if len(s) > 80:
        s = s[:80] + "-" + hashlib.md5((url or s).encode()).hexdigest()[:8]
    return s or hashlib.md5((url or "").encode()).hexdigest()[:_SLUG_FALLBACK_LEN]


def fetch_fulltext(url: str, *, session: requests.Session | None = None,
                   min_chars: int = 200, max_chars: int = 12000,
                   max_retries: int = 2) -> str | None:
    """抓全文 + markdown 化（trafilatura）；返回 Markdown 文本或 None

    重要：始终用 cloudscraper 直连（不走沙箱 HTTP_PROXY，且能绕 Cloudflare 等反爬）。
    OpenAI / Anthropic / LangChain 等目标站对直连 requests 返回 403，需要 cloudscraper。
    抓 RSS 时再用共享代理 session。

    反爬注意：cloudscraper 第一次 challenge 偶尔会 403；默认重试 max_retries 次。
    """
    try:
        import trafilatura
        import cloudscraper
    except ImportError as e:
        get_logger("fetch_fulltext").error(f"缺少依赖: {e}; 请先 pip install trafilatura cloudscraper")
        return None

    last_err: str | None = None
    for attempt in range(max_retries + 1):
        # 每次重试用不同 UA
        sc = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "linux", "desktop": True},
            delay=0,  # 关闭内部 sleep，靠外层退避
        )
        sc.trust_env = False
        try:
            resp = sc.get(url, timeout=30)
            resp.raise_for_status()
            html = resp.text
        except Exception as e:
            last_err = f"GET 失败: {e}"
            time.sleep(1.5 * (attempt + 1))
            continue
        try:
            extracted = trafilatura.extract(
                html,
                include_comments=False,
                include_tables=False,
                favor_precision=True,
                with_metadata=False,
            )
        except Exception as e:
            last_err = f"EXTRACT 失败: {e}"
            time.sleep(1.0)
            continue
        if extracted and len(extracted) >= min_chars:
            if len(extracted) > max_chars:
                extracted = extracted[:max_chars] + "\n\n*[... 正文过长已截断，原文见链接]*"
            return extracted
        last_err = f"正文太短: {len(extracted or '')} 字符 (min={min_chars})"
        time.sleep(1.0)
    if last_err:
        get_logger("fetch_fulltext").warning(f"[{url}] {last_err}（{max_retries+1} 次重试均失败）")
    return None


def save_article(source: str, slug: str, content: str, *, url: str) -> Path:
    """落 data/articles/<source>/<slug>.md（带源信息 frontmatter 注释头）"""
    out_dir = ARTICLES_DIR / source
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{slug}.md"
    if out_path.exists():
        return out_path  # 缓存命中
    header = (
        f"<!-- source: {source} | url: {url} | "
        f"fetched_at: {datetime.now(CST).isoformat(timespec='seconds')} -->\n\n"
    )
    out_path.write_text(header + content + "\n", encoding="utf-8")
    return out_path


def enrich_item_with_article(item: Item, source: str, *,
                             session: requests.Session | None = None,
                             force: bool = False) -> Item:
    """
    抓全文 + 落盘 + 把相对路径写回 item.extra['article_path']。

    - 优先用 item.extra['slug']（如 LangChain/Runway 已存）
    - 抓全文失败 / 长度不足：不阻塞 item 落盘，只是不写 article_path
    - 已落盘的文件：跳过抓取（除非 force=True）
    """
    fallback = item.extra.get("slug") if isinstance(item.extra, dict) else None
    slug = slug_from_url(item.url, fallback_slug=fallback)
    out_path = ARTICLES_DIR / source / f"{slug}.md"

    # 缓存命中
    if out_path.exists() and not force:
        item.extra = dict(item.extra or {})
        item.extra["article_path"] = str(out_path.relative_to(REPO_ROOT))
        item.extra["article_cached"] = True
        return item

    content = fetch_fulltext(item.url, session=session)
    if content:
        save_article(source, slug, content, url=item.url)
        item.extra = dict(item.extra or {})
        item.extra["article_path"] = str(out_path.relative_to(REPO_ROOT))
        item.extra["article_chars"] = len(content)
    return item


def enrich_items(items: list[Item], source: str, *,
                 session: requests.Session | None = None,
                 log: logging.Logger | None = None,
                 delay: float = 0.0) -> list[Item]:
    """对一批 items 串行抓全文 + 落盘。失败不阻塞，安静跳过。"""
    out: list[Item] = []
    for it in items:
        try:
            out.append(enrich_item_with_article(it, source, session=session))
        except Exception as e:
            if log:
                log.warning(f"全文抓取异常 [{it.url}]: {e}")
            out.append(it)
        if delay > 0:
            time.sleep(delay)
    return out


# ---------- CLI 辅助 ----------
def add_date_args(parser) -> None:
    """为 argparse 添加 --date / --days / --all 参数"""
    parser.add_argument(
        "--date", help="目标日期 YYYY-MM-DD（默认昨日）"
    )
    parser.add_argument(
        "--days", type=int, default=0,
        help="回溯天数（0=只抓昨日；>0=抓昨日+前 N 天）",
    )
    parser.add_argument(
        "--all", action="store_true",
        help="忽略窗口，返回 feed 全部最新条目（仅供调试）",
    )
    parser.add_argument(
        "--no-article", action="store_true",
        help="不抓全文（只存 RSS summary）",
    )


def resolve_dates(args) -> list[tuple[str, datetime, datetime]]:
    """根据 CLI 参数返回 [(date_str, start, end), ...] 列表"""
    if getattr(args, "all", False):
        # 调试模式：返回单个占位日期，窗口设到极宽
        return [("all", datetime(2000, 1, 1, tzinfo=UTC), datetime.now(UTC))]
    if args.date:
        d = datetime.strptime(args.date, "%Y-%m-%d").date()
        start = datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=CST)
        end = datetime(d.year, d.month, d.day, 23, 59, 59, 999999, tzinfo=CST)
        return [(d.isoformat(), start, end)]
    out: list[tuple[str, datetime, datetime]] = []
    now = datetime.now(CST)
    for k in range(0, max(1, args.days) + 1):
        d = (now - timedelta(days=1 + k)).date()
        start = datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=CST)
        end = datetime(d.year, d.month, d.day, 23, 59, 59, 999999, tzinfo=CST)
        out.append((d.isoformat(), start, end))
    return out
