"""
共用工具：HTTP 抓取 / RSS 解析 / 日期窗口 / 落盘
所有抓取脚本均依赖本模块。
"""
from __future__ import annotations

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
from zoneinfo import ZoneInfo

import feedparser
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ---------- 路径常量 ----------
REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "data" / "raw"
LOG_DIR = REPO_ROOT / "logs"
DATA_DIR.mkdir(parents=True, exist_ok=True)
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
