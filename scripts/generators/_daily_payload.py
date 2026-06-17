"""
daily v2.0 JSON payload 提取器
=============================

职责：
1. 把 generate_daily.py main() 已有的数据（groups / per_source / usage / one_line_summary）组装成 v2.0 daily JSON
2. 不调 LLM（LLM 精炼产物已由 generate_daily.py 在评分+精炼时调过，传进来即可）
3. 不读 md（md 是渲染产物之一）
4. 唯一真相源 = `data/published/daily/<date>.json`（含 LLM 精炼结构）

设计原则：
- 关注点分离：评分（generate_daily.py）+ LLM 精炼（generate_daily.py）+ payload 提取（本模块）+ 写 JSON（generate_daily.py）
- schema_version = "2.0"（v1.0 → v2.0 升级，break change，旧 JSON 保留兼容）
- 字段"只增不减"：本模块只产出新字段，保留 raw_summary 字段兼容旧 v1.0
- 渲染无关：payload 不含任何渲染字段（无 markdown 字符串 / 无飞书 docx 文本），由 renderers/ 模块派生

v1.0 → v2.0 字段对比：
  v1.0: 12 字段（schema_version / report_type / report_date / report_file / report_sha256 / report_size_bytes /
              first_pushed_at / last_pushed_at / push_count / pushed_by / doc_url / ok / channels / raw_summary / error / _sha_changed_since_first_push）
  v2.0: 35+ 字段（v1.0 全保留 + meta / boards / main_signals / one_line_summary / llm_usage / render_meta）

依赖：
  - _utils_gen.py: BOARD_META / BOARD_ORDER / ScoredItem / extract_keywords / extract_main_signals
  - generate_daily.py: 调用方传入 groups / per_source / usage / one_line_summary
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from _utils_gen import (
    BOARD_META,
    BOARD_ORDER,
    ScoredItem,
    classify_board,
    extract_keywords,
    extract_main_signals,
)


SCHEMA_VERSION = "2.0"
TZ_SH = ZoneInfo("Asia/Shanghai")


# ============== LLM call_records 接口设计 ==============
# 当前 LLMUsage 只有 calls / failed / cache_hits / last_provider / last_model
# v2.0 升级为 call_records[]，由 generate_daily.py 改造时补
# 本模块接受 call_records=None（向后兼容：旧 generate_daily.py 调用时降级为 summary 模式）
LLMPurpose = str  # "one_line_summary" | "cross_insights" | "summarize_article" | "impact" | "brief" | "followup"


def _build_llm_usage(usage, call_records: list[dict] | None = None) -> dict:
    """构建 llm_usage 段
    输入：
      usage: LLMUsage 实例（v1.4 接口）
      call_records: list of {"purpose", "prompt_hash", "output_chars", "duration_ms", "ok"} | None
    """
    if usage is None:
        return {
            "enabled": False,
            "calls": 0,
            "cache_hits": 0,
            "failed": 0,
            "provider": "",
            "model": "",
            "footer": "未启用 LLM（无 .secrets 配置或 --no-llm）",
            "call_records": [],
        }
    base = {
        "enabled": usage.calls > 0 or usage.failed > 0,
        "calls": usage.calls,
        "cache_hits": usage.cache_hits,
        "failed": usage.failed,
        "provider": usage.last_provider,
        "model": usage.last_model,
        "footer": usage.to_footer(),
        "call_records": call_records or [],
    }
    return base


# ============== Meta 段 ==============

def _build_meta(groups: dict[str, list[ScoredItem]], per_source: dict[str, int],
                date_iso: str) -> dict:
    """构建 meta 段：total / sources / priority counts / boards / hit_rate / week_label"""
    all_items = [it for v in groups.values() for it in v]
    total = len(all_items)
    high = sum(1 for it in all_items if it.grade == "🔴")
    mid = sum(1 for it in all_items if it.grade == "🟡")
    low = sum(1 for it in all_items if it.grade == "🟢")

    sources_with_zero = sorted([s for s, c in per_source.items() if c == 0])
    boards_with_data = sorted([b for b, items in groups.items() if items])
    n_sources_configured = len(per_source)
    n_sources_hit = sum(1 for v in per_source.values() if v > 0)
    hit_rate = (n_sources_hit / n_sources_configured) if n_sources_configured else 0.0

    # ISO 周（用于 weekly 聚合）
    dt = datetime.fromisoformat(date_iso + "T12:00:00+08:00")
    iso = dt.isocalendar()
    week_label = f"{iso[0]}-W{iso[1]:02d}"

    return {
        "total_items": total,
        "total_sources": n_sources_configured,
        "sources_with_zero": sources_with_zero,
        "high_priority_count": high,
        "mid_priority_count": mid,
        "low_priority_count": low,
        "boards_with_data": boards_with_data,
        "n_sources_configured": n_sources_configured,
        "n_sources_hit": n_sources_hit,
        "hit_rate": round(hit_rate, 4),
        "week_label": week_label,
    }


# ============== Boards 段 ==============

def _to_priority(grade: str) -> str:
    """🔴/🟡/🟢 → high/mid/low"""
    return {"🔴": "high", "🟡": "mid", "🟢": "low"}.get(grade, "mid")


def _format_item(it: ScoredItem, rank: int) -> dict:
    """ScoredItem → top_item dict（renderable item）"""
    return {
        "rank": rank,
        "title": it.title,
        "url": it.url,
        "source": it.source,
        "board": it.board,
        "priority": _to_priority(it.grade),
        "summary": it.summary,
        "matched_tags": it.matched_tags,
        "published_at": it.published_dt.isoformat() if it.published_dt else "",
        "score": it.score,
    }


def _extract_board_payload(board: str, items: list[ScoredItem],
                           top_n: int = 10) -> dict:
    """单个板块 → board payload dict"""
    meta = BOARD_META.get(board, BOARD_META["其他"])
    total = len(items)
    high = sum(1 for it in items if it.grade == "🔴")
    mid = sum(1 for it in items if it.grade == "🟡")
    low = sum(1 for it in items if it.grade == "🟢")
    keywords = extract_keywords(items, top_n=4)
    top_items = [_format_item(it, rank) for rank, it in enumerate(items[:top_n], 1)]
    return {
        "name": meta["name"],
        "emoji": meta["emoji"],
        "total": total,
        "high": high,
        "mid": mid,
        "low": low,
        "keywords": keywords,
        "top_items": top_items,
    }


def _build_boards(groups: dict[str, list[ScoredItem]], top_n: int = 10) -> dict:
    """5 个板块 → boards dict（按 BOARD_ORDER 顺序）"""
    out: dict[str, dict] = {}
    for board in BOARD_ORDER:
        items = groups.get(board, [])
        out[board] = _extract_board_payload(board, items, top_n=top_n)
    return out


# ============== Main Signals 段 ==============

def _format_signal_item(it: ScoredItem) -> dict:
    """ScoredItem → signal item dict（比 top_item 精简：不含 rank）"""
    return {
        "title": it.title,
        "url": it.url,
        "source": it.source,
        "board": it.board,
        "priority": _to_priority(it.grade),
        "summary": it.summary,
    }


def _build_main_signals(groups: dict[str, list[ScoredItem]],
                        top_n: int = 3, min_items: int = 2) -> list[dict]:
    """提取主线信号（复用 _utils_gen.extract_main_signals）
    返回 v2.0 signal dict 列表
    """
    all_items = [it for v in groups.values() for it in v]
    raw_signals = extract_main_signals(all_items, top_n=top_n, min_items=min_items)
    out: list[dict] = []
    for sig in raw_signals:
        out.append({
            "tag": sig["tag"],
            "display": sig["display"],
            "count": sig["count"],
            "sources": sig["sources"],
            "n_sources": len(sig["sources"]),
            "items": [_format_signal_item(it) for it in sig["items"][:5]],  # 限 Top 5 关联
            "llm_insight": None,  # v2.0 字段预留，主人原话"宁缺毋滥"暂不填充
        })
    return out


# ============== One Line Summary 段 ==============

def _build_one_line_summary(text: str, source: str = "rule") -> dict:
    """一句话总结
    text: 已生成的一句话总结
    source: "llm" | "rule"（标识来源）
    """
    return {
        "text": text or "",
        "source": source,
    }


# ============== Render Meta 段 ==============

def _build_render_meta(elapsed_seconds: float, report_file: str,
                       report_size_bytes: int | None) -> dict:
    """生成耗时 / md 文件信息（md 是渲染产物之一）"""
    return {
        "elapsed_seconds": round(elapsed_seconds, 2),
        "report_file": report_file,
        "report_size_bytes": report_size_bytes,
    }


# ============== 入口 ==============

def build_daily_payload(
    date_iso: str,
    groups: dict[str, list[ScoredItem]],
    per_source: dict[str, int],
    fetched_at: str,
    usage,  # LLMUsage
    one_line_summary_text: str,
    one_line_summary_source: str = "rule",
    elapsed_seconds: float = 0.0,
    llm_call_records: list[dict] | None = None,
    report_file: str | None = None,
    report_size_bytes: int | None = None,
) -> dict:
    """组装 daily v2.0 JSON payload（不含 publish_record 部分，由 published_record.write_record 写入）

    参数：
      date_iso: YYYY-MM-DD
      groups: 按板块分组的评分条目（generate_daily.py main() 已有）
      per_source: 每源命中条数
      fetched_at: 采集时间 ISO 字符串
      usage: LLMUsage 实例（v1.4 接口）
      one_line_summary_text: 已生成的一句话总结（LLM 或规则版）
      one_line_summary_source: "llm" | "rule"
      elapsed_seconds: 生成耗时
      llm_call_records: LLM 调用详细记录（v2.0 新字段，可选）
      report_file: 每日资讯 md 相对路径（"每日资讯/<date>.md"）
      report_size_bytes: md 文件大小

    返回：
      daily v2.0 payload dict（含 schema_version="2.0" / meta / boards / main_signals /
      one_line_summary / llm_usage / render_meta；不含 publish_record）

    注意：
      - 本函数**不写文件**（关注点分离）
      - 写文件由 published_record.write_record(daily_payload=payload, ...) 统一管理
      - 这样保证 v1.0 publish_record 字段（doc_url / first_pushed_at / push_count / channels）
        在 v1.0 → v2.0 升级时不被覆盖丢失
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "report_type": "daily",
        "report_date": date_iso,
        "generated_at": datetime.now(TZ_SH).isoformat(timespec="seconds"),
        "meta": _build_meta(groups, per_source, date_iso),
        "boards": _build_boards(groups, top_n=10),
        "main_signals": _build_main_signals(groups, top_n=3, min_items=2),
        "one_line_summary": _build_one_line_summary(
            one_line_summary_text, source=one_line_summary_source
        ),
        "llm_usage": _build_llm_usage(usage, call_records=llm_call_records),
        "render_meta": _build_render_meta(
            elapsed_seconds,
            report_file or f"每日资讯/{date_iso}.md",
            report_size_bytes,
        ),
    }


# ============== CLI 调试 ==============

if __name__ == "__main__":
    import argparse
    import json as _json
    from pathlib import Path as _Path

    ap = argparse.ArgumentParser(description="daily v2.0 payload 调试")
    ap.add_argument("--date", required=True, help="YYYY-MM-DD")
    ap.add_argument("--raw-root", default="data/raw")
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--write", action="store_true", help="写文件到 data/published/daily/<date>.json（merge v1.0 publish_record）")
    args = ap.parse_args()

    # 模拟 generate_daily.py main() 数据流
    from generate_daily import (
        LLMUsage, load_raw_items, build_scored_items, group_by_board,
        load_llm_client,
    )

    _REPO_ROOT = _Path(__file__).resolve().parent.parent.parent
    raw_root = (_REPO_ROOT / args.raw_root).resolve()

    usage = LLMUsage()
    if not args.no_llm:
        llm = load_llm_client()
    else:
        llm = None

    raw_items, per_source = load_raw_items(args.date, raw_root)
    scored = build_scored_items(raw_items, args.date)
    groups = group_by_board(scored)

    # 简化：one_line_summary 用规则版（实际由 generate_daily.py 调 LLM）
    from generate_daily import build_one_line_summary
    main_signals_raw = extract_main_signals(
        [it for v in groups.values() for it in v], top_n=3, min_items=2
    )
    summary = build_one_line_summary(args.date, groups, per_source, main_signals_raw)

    payload = build_daily_payload(
        date_iso=args.date,
        groups=groups,
        per_source=per_source,
        fetched_at=datetime.now(TZ_SH).isoformat(),
        usage=usage,
        one_line_summary_text=summary,
        one_line_summary_source="rule",
        elapsed_seconds=0.0,
    )

    if args.dry_run:
        print(_json.dumps(payload, ensure_ascii=False, indent=2))
    elif args.write:
        # 走 published_record.write_record merge 模式（保留 v1.0 publish_record）
        import sys
        sys.path.insert(0, str(_REPO_ROOT / "scripts"))
        from published_record import write_record
        rec = write_record(
            report_type="daily",
            report_file=f"每日资讯/{args.date}.md",
            channels_result={},
            doc_url=None,
            pushed_by="backfill_v2",
            ok=False,
            daily_payload=payload,
            report_date=args.date,
        )
        print(f"[OK] 写入 {rec.get('_path', 'data/published/daily/' + args.date + '.json')}")
        print(f"     schema_version={payload['schema_version']}")
        print(f"     total_items={payload['meta']['total_items']}")
        print(f"     boards={list(payload['boards'].keys())}")
        print(f"     main_signals={len(payload['main_signals'])}")
        print(f"     one_line_summary.source={payload['one_line_summary']['source']}")
    else:
        print(_json.dumps({
            "schema_version": payload["schema_version"],
            "total_items": payload["meta"]["total_items"],
            "boards": list(payload["boards"].keys()),
            "main_signals_count": len(payload["main_signals"]),
            "one_line_summary": payload["one_line_summary"],
        }, ensure_ascii=False, indent=2))
