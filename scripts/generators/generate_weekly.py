"""
每周汇总生成器 v2.2（议题 H：weekly 重做）
====================================

读 7 天 daily v2.0 JSON（schema 2.0）→ 聚合 + 跨周对比 + 周度 Top 10 +
板块热度图 + 跨话题洞察（白名单 4 类）→ 渲染 → 落盘 `每周汇总/YYYY-Www.md`

v2.2 相对 v1.5 的 4 大核心改动（议题 H）：
  1. 数据源：daily md（正则解析）→ daily v2.0 JSON（结构化）
  2. LLM：trend 砍 LLM + insights 砍 LLM（议题 A B 方案）→ 100% 规则
  3. 推送节奏：周日 8:30（v1.5，已 cancel）→ 周一 8:30 推上一周（commit Y 新建 calendar）
  4. 模板：+跨周对比 / +板块热度图 / +周度 Top 10 / -下周关注清单

用法：
    python3 scripts/generators/generate_weekly.py --week 2026-W24
    python3 scripts/generators/generate_weekly.py                    # 默认 = 本周（周一~周日，Asia/Shanghai）
    python3 scripts/generators/generate_weekly.py --week 2026-W24 --dry-run
    python3 scripts/generators/generate_weekly.py --week 2026-W24 --no-llm   # v2.2 已默认 --no-llm；保留参数兼容
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent  # scripts/generators → AI资讯追踪
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "generators"))

from _utils_gen import (  # noqa: E402
    BOARD_META,
    BOARD_ORDER,
)


# ============== ISO 周工具 ==============

def week_dates(year: int, week: int) -> tuple[date, date]:
    """给定 ISO (year, week) → 返 (周一, 周日)"""
    jan4 = date(year, 1, 4)
    iso_jan4 = jan4.isocalendar()
    week1_monday = jan4 - timedelta(days=iso_jan4[2] - 1)
    target_monday = week1_monday + timedelta(weeks=week - 1)
    sunday = target_monday + timedelta(days=6)
    return target_monday, sunday


def week_label(year: int, week: int) -> str:
    return f"{year}-W{week:02d}"


def current_week() -> tuple[int, int]:
    """Asia/Shanghai 当前 ISO 周"""
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    cal = today.isocalendar()
    return cal[0], cal[1]


def prev_week(year: int, week: int) -> tuple[int, int]:
    """给定 ISO (year, week) → 返上一周 ISO (year, week)"""
    monday, _ = week_dates(year, week)
    prev_monday = monday - timedelta(days=7)
    cal = prev_monday.isocalendar()
    return cal[0], cal[1]


# ============== 数据结构 ==============

@dataclass
class DailyWeekSummary:
    """从 daily v2.0 JSON 解析的当日概要（v2.2 新增：JSON 数据源）"""
    date: str                       # YYYY-MM-DD
    file_path: Path                 # 源 JSON 路径
    exists: bool                    # JSON 是否存在
    board_stats: dict[str, dict]    # {board: {total, high, mid, low, keywords}}
    main_signals: list[dict]        # main_signals 列表
    one_line_summary: str           # one_line_summary.text
    total_items: int                # meta.total_items
    top_items: list[dict]           # 全部 top_items 扁平（用于周度 Top 10）
    doc_url: str                    # publish_record.doc_url or 顶层 doc_url
    hit_rate: float                 # meta.hit_rate
    boards_with_data: list[str]     # meta.boards_with_data
    sources_with_zero: list[str]    # meta.sources_with_zero


def _empty_board_stats() -> dict[str, dict]:
    return {b: {"total": 0, "high": 0, "mid": 0, "low": 0, "keywords": []}
            for b in BOARD_ORDER}


def _parse_v2_daily(data: dict, date_iso: str, file_path: Path) -> DailyWeekSummary:
    """解析 daily v2.0 JSON → DailyWeekSummary"""
    boards_data = data.get("boards", {}) or {}
    board_stats: dict[str, dict] = _empty_board_stats()
    top_items_flat: list[dict] = []

    for board_key in BOARD_ORDER:
        b = boards_data.get(board_key, {}) or {}
        board_stats[board_key] = {
            "total": b.get("total", 0),
            "high": b.get("high", 0),
            "mid": b.get("mid", 0),
            "low": b.get("low", 0),
            "keywords": list(b.get("keywords", []) or []),
        }
        for it in b.get("top_items", []) or []:
            top_items_flat.append({
                **it,
                "board": board_key,  # 显式标 board
            })

    # doc_url 优先取 publish_record.doc_url（v2.0 阶段 5 修复），兜底顶层 doc_url
    pub_record = data.get("publish_record", {}) or {}
    doc_url = pub_record.get("doc_url") or data.get("doc_url") or ""

    meta = data.get("meta", {}) or {}

    return DailyWeekSummary(
        date=date_iso,
        file_path=file_path,
        exists=True,
        board_stats=board_stats,
        main_signals=list(data.get("main_signals", []) or []),
        one_line_summary=(data.get("one_line_summary", {}) or {}).get("text", ""),
        total_items=meta.get("total_items", 0),
        top_items=top_items_flat,
        doc_url=doc_url,
        hit_rate=meta.get("hit_rate", 0.0),
        boards_with_data=list(meta.get("boards_with_data", []) or []),
        sources_with_zero=list(meta.get("sources_with_zero", []) or []),
    )


def load_week_daily_jsons(monday: date, sunday: date,
                          daily_json_root: Path) -> list[DailyWeekSummary]:
    """读 7 天 daily v2.0 JSON（v2.2 数据源升级；缺失日期标 exists=False）"""
    out: list[DailyWeekSummary] = []
    cur = monday
    while cur <= sunday:
        iso = cur.isoformat()
        fp = daily_json_root / f"{iso}.json"
        if not fp.exists():
            out.append(DailyWeekSummary(
                date=iso, file_path=fp, exists=False,
                board_stats=_empty_board_stats(),
                main_signals=[], one_line_summary="", total_items=0,
                top_items=[], doc_url="", hit_rate=0.0,
                boards_with_data=[], sources_with_zero=[],
            ))
        else:
            try:
                data = json.loads(fp.read_text(encoding="utf-8"))
                out.append(_parse_v2_daily(data, iso, fp))
            except Exception as e:
                print(f"[WARN] daily v2.0 JSON 解析失败: {fp}: {e}", file=sys.stderr)
                out.append(DailyWeekSummary(
                    date=iso, file_path=fp, exists=False,
                    board_stats=_empty_board_stats(),
                    main_signals=[], one_line_summary="", total_items=0,
                    top_items=[], doc_url="", hit_rate=0.0,
                    boards_with_data=[], sources_with_zero=[],
                ))
        cur += timedelta(days=1)
    return out


# ============== 聚合 ==============

def aggregate_week(summaries: list[DailyWeekSummary]) -> dict:
    """聚合 7 天数据 → 周度统计（v2.2 增强：板块级 + 周度 Top 10 + 信息源覆盖）"""
    week_stats: dict[str, dict] = {}
    for board in BOARD_ORDER:
        total = high = mid = low = 0
        for s in summaries:
            if not s.exists:
                continue
            bs = s.board_stats.get(board, {})
            total += bs.get("total", 0)
            high += bs.get("high", 0)
            mid += bs.get("mid", 0)
            low += bs.get("low", 0)
        week_stats[board] = {"total": total, "high": high, "mid": mid, "low": low}

    # 主题词累计（按板块）
    keyword_counter: dict[str, Counter] = {b: Counter() for b in BOARD_ORDER}
    for s in summaries:
        if not s.exists:
            continue
        for board, bs in s.board_stats.items():
            for kw in bs.get("keywords", []):
                keyword_counter[board][kw] += 1
    week_keywords = {b: [k for k, _ in counter.most_common(4)]
                     for b, counter in keyword_counter.items()}

    # 主线信号累计（按 display 聚合）
    signal_counter: Counter = Counter()
    signal_sources: dict[str, set] = defaultdict(set)
    for s in summaries:
        if not s.exists:
            continue
        for sig in s.main_signals:
            disp = sig.get("display", sig.get("tag", ""))
            count = sig.get("count", 0)
            signal_counter[disp] += count
            for src in sig.get("sources", []) or []:
                signal_sources[disp].add(src)
    top_signals = signal_counter.most_common(5)

    # 抓取覆盖（哪些日期存在日报）
    days_with_data = sum(1 for s in summaries if s.exists and s.total_items > 0)
    days_missing = 7 - days_with_data

    # 周度 Top 10（按 score desc 跨板块；v2.2 新增）
    weekly_top10: list[dict] = sorted(
        [it for s in summaries if s.exists for it in s.top_items],
        key=lambda x: (-(x.get("score", 0) or 0), x.get("published_at", "")),
    )[:10]

    # 信息源覆盖（v2.2 增强：直接读 sources_with_zero + 用 top_items 统计源命中天数）
    source_days: dict[str, set[str]] = defaultdict(set)
    source_mentions: dict[str, int] = defaultdict(int)
    for s in summaries:
        if not s.exists:
            continue
        for it in s.top_items:
            src = it.get("source", "")
            if src:
                source_days[src].add(s.date)
                source_mentions[src] += 1
    # 累计 0 源（7 天都 0 命中）
    cumulative_zero_sources: list[str] = sorted([
        src for src, days in source_days.items() if not days
    ])
    # 本周 0 命中源（7 天都没出现）
    weekly_zero_sources: list[str] = sorted(cumulative_zero_sources)

    return {
        "week_stats": week_stats,
        "week_keywords": week_keywords,
        "top_signals": top_signals,
        "signal_sources": signal_sources,
        "days_with_data": days_with_data,
        "days_missing": days_missing,
        "weekly_top10": weekly_top10,                  # v2.2 新增
        "source_days": source_days,                    # v2.2 重构
        "source_mentions": source_mentions,            # v2.2 重构
        "weekly_zero_sources": weekly_zero_sources,    # v2.2 新增
    }


def cross_week_compare(curr: dict, prev: dict | None) -> dict:
    """跨周对比：vs 上周（v2.2 新增；上周数据缺失或为 0 时降级为 unavailable）"""
    if prev is None:
        return {"available": False, "reason": "上周 daily JSON 不存在或不可读"}

    # 边界：上周完全无数据（0 条 / 0 天有数据）→ 不可比
    prev_total = sum(s["total"] for s in prev["week_stats"].values())
    prev_days_with_data = prev.get("days_with_data", 0)
    if prev_total == 0 and prev_days_with_data == 0:
        return {"available": False, "reason": f"上周 0 条数据（{prev_days_with_data}/7 天有数据），无法对比"}

    out: dict = {"available": True}
    # 总条数
    curr_total = sum(s["total"] for s in curr["week_stats"].values())
    prev_total = sum(s["total"] for s in prev["week_stats"].values())
    out["total_delta"] = curr_total - prev_total
    out["total_pct"] = (curr_total - prev_total) / prev_total * 100 if prev_total > 0 else None

    # 高优条数
    curr_high = sum(s["high"] for s in curr["week_stats"].values())
    prev_high = sum(s["high"] for s in prev["week_stats"].values())
    out["high_delta"] = curr_high - prev_high

    # 板块条数对比
    out["board_delta"] = {}
    for board in BOARD_ORDER:
        cb = curr["week_stats"][board]["total"]
        pb = prev["week_stats"][board]["total"]
        out["board_delta"][board] = {
            "curr": cb, "prev": pb, "delta": cb - pb,
        }

    # 信息源覆盖对比
    out["zero_sources_delta"] = sorted(
        set(curr["weekly_zero_sources"]) - set(prev["weekly_zero_sources"])
    )

    return out


# ============== 规则版洞察（B 方案：白名单 4 类） ==============

def build_insights_whitelist(agg: dict, summaries: list[DailyWeekSummary]) -> list[str]:
    """议题 A B 方案：洞察收紧为白名单 4 类（v2.2 全规则，0 LLM 调用）
    4 类：
      ① 跨源聚合（top_signals 跨 ≥2 源）
      ② 资本节奏（含资本/融资/估值主线信号）
      ③ 监管动态（含监管/安全主线信号）
      ④ 覆盖缺口（板块空缺 / 源连续 0 命中）
    """
    insights: list[str] = []
    top = agg["top_signals"]

    # ① 跨源聚合（取第一个跨 ≥2 源的信号）
    cross_src_signals = [
        (d, c) for d, c in top
        if len(agg["signal_sources"].get(d, [])) >= 2
    ]
    if cross_src_signals:
        first_display, first_count = cross_src_signals[0]
        n_sources = len(agg["signal_sources"].get(first_display, []))
        sources = sorted(agg["signal_sources"].get(first_display, []))
        if n_sources >= 3:
            insights.append(
                f"**跨源聚合**：{first_display} 本周累计 {first_count} 条，"
                f"跨 {n_sources} 个独立源（{', '.join(sources)}）确认，"
                f"提示该方向已是行业级共识而非单点噪音。"
            )
        else:
            insights.append(
                f"**双源确认**：{first_display} 本周累计 {first_count} 条，"
                f"跨 {n_sources} 个源（{', '.join(sources)}）"
                f"被独立报道，信号强度较高。"
            )

    # ② 资本节奏
    capital_signals = [(d, c) for d, c in top
                       if "💰" in d or "资本" in d or "融资" in d or "估值" in d]
    if capital_signals:
        d, c = capital_signals[0]
        insights.append(
            f"**资本节奏**：{d} 本周累计 {c} 条，"
            f"提示 AI 行业资本流动仍在加速。"
        )

    # ③ 监管动态
    reg_signals = [(d, c) for d, c in top
                   if "⚖️" in d or "监管" in d or "安全" in d or "合规" in d]
    if reg_signals:
        d, c = reg_signals[0]
        insights.append(
            f"**监管动态**：{d} 本周累计 {c} 条，监管侧动作需持续追踪。"
        )

    # ④ 覆盖缺口
    empty_boards = [b for b in BOARD_ORDER if agg["week_stats"][b]["total"] == 0]
    zero_sources = agg.get("weekly_zero_sources", []) or []
    gap_msgs: list[str] = []
    if empty_boards:
        names = "、".join(BOARD_META[b]["name"] for b in empty_boards)
        gap_msgs.append(f"板块 {names} 无新增")
    if zero_sources:
        gap_msgs.append(f"源 {', '.join(f'`{s}`' for s in zero_sources)} 连续 0 命中")
    if gap_msgs:
        insights.append(
            f"**覆盖缺口**：本周 {' / '.join(gap_msgs)}，"
            f"可能需评估是否补充对应源或修抓取。"
        )

    return insights[:5] if insights else ["本周数据较少，跨话题趋势暂不显著。"]


# ============== 渲染（v2.2 模板升级） ==============

def render_weekly_report(
    year: int, week: int, monday: date, sunday: date,
    summaries: list[DailyWeekSummary], agg: dict,
    cross: dict | None,
    elapsed_seconds: float = 0.0,
) -> str:
    """渲染周汇总 Markdown（v2.2 模板：+跨周对比 / +板块热度图 / +周度 Top 10 / -下周关注清单）"""
    label = week_label(year, week)
    date_range = f"{monday.strftime('%m.%d')} - {sunday.strftime('%m.%d')}"
    full_date_range = f"{monday.isoformat()} ~ {sunday.isoformat()}"
    total = sum(s["total"] for s in agg["week_stats"].values())
    total_high = sum(s["high"] for s in agg["week_stats"].values())
    total_mid = sum(s["mid"] for s in agg["week_stats"].values())
    total_low = sum(s["low"] for s in agg["week_stats"].values())

    lines: list[str] = []
    # ============== 头部 ==============
    lines.append(f"# 📊 AI每周资讯汇总 | {year} 年第 {week} 周（{date_range}）")
    lines.append("")
    lines.append(f"> **汇总周期**：本周（{full_date_range}）")
    lines.append(f"> **生成时间**：{datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M')}（Asia/Shanghai）")
    lines.append("> **话题板块**：🧠 LLM 发展 · 💻 Agent 框架与工具 · 🧍 数字人 · 🏢 行业动态 · 📦 其他（可扩展）")
    lines.append("> **每版块条数**：周度 Top 10（按\"重要度 + 时新性\"排序；不足按实际）")
    lines.append("")
    if agg["days_with_data"] < 7:
        lines.append(f"> ⚠️ **WIP**：本周 {agg['days_with_data']} 天数据可用，{agg['days_missing']} 天日报缺失（系统未生成或 0 命中）")
        lines.append("")
    lines.append("---")
    lines.append("")

    # ============== 1. 本周概览（v1.5 沿用） ==============
    lines.append("## 🎯 本周概览")
    lines.append("")
    lines.append("| 话题板块 | 本周条数 | 日均 | 🔴 高优 | 🟡 中等 | 🟢 一般 | 核心主题词 |")
    lines.append("|---------|---------|------|--------|--------|-------|-----------|")
    for board in BOARD_ORDER:
        ws = agg["week_stats"][board]
        meta = BOARD_META[board]
        days = agg["days_with_data"] or 1
        avg = ws["total"] / days
        kws = "、".join(agg["week_keywords"].get(board, [])) or "—"
        lines.append(
            f"| {meta['emoji']} {meta['name']} | {ws['total']} | {avg:.1f} | "
            f"{ws['high']} | {ws['mid']} | {ws['low']} | {kws} |"
        )
    lines.append(f"| **合计** | **{total}** | **{total/7:.1f}** | **{total_high}** | **{total_mid}** | **{total_low}** | — |")
    lines.append("")

    # 一句话总结
    if total == 0:
        summary_line = f"{label} 全网 AI 资讯较少，已对接的源均无新增。"
    else:
        top3 = agg["top_signals"][:3]
        if top3:
            sig_str = "；".join(f"{d}（{c} 条）" for d, c in top3)
            summary_line = f"本周主线：{sig_str}。"
        else:
            summary_line = f"本周 {total} 条命中。"
    lines.append(f"> **本周一句话总结**：{summary_line}")
    lines.append("")
    lines.append("---")
    lines.append("")

    # ============== 2. 跨周对比（v2.2 新增） ==============
    lines.append("## 🔁 跨周对比")
    lines.append("")
    if cross is None:
        lines.append("> ⏭️ 已禁用跨周对比（--no-cross-week）")
    elif not cross.get("available"):
        lines.append(f"> ⏭️ {cross.get('reason', '无对比数据')}")
    else:
        total_delta = cross["total_delta"]
        total_pct = cross["total_pct"]
        pct_str = f"（{total_pct:+.1f}%）" if total_pct is not None else ""
        delta_emoji = "📈" if total_delta > 0 else ("📉" if total_delta < 0 else "➡️")
        prev_total = sum(cross["board_delta"][b]["prev"] for b in BOARD_ORDER)
        lines.append(
            f"- **总条数**：{delta_emoji} {total:+d}{pct_str}（本周 {total} / 上周 {prev_total}）"
        )
        # cross["high_delta"] = curr_high - prev_high
        high_delta = cross["high_delta"]
        prev_high = total_high - high_delta
        high_emoji = "📈" if high_delta > 0 else ("📉" if high_delta < 0 else "➡️")
        lines.append(
            f"- **高优条数（🔴）**：{high_emoji} {high_delta:+d}（本周 {total_high} / 上周 {prev_high}）"
        )
        # 板块条数对比表
        lines.append("")
        lines.append("| 话题板块 | 本周条数 | 上周条数 | 差值 |")
        lines.append("|---------|---------|---------|------|")
        for board in BOARD_ORDER:
            d = cross["board_delta"][board]
            meta = BOARD_META[board]
            delta_emoji_b = "📈" if d["delta"] > 0 else ("📉" if d["delta"] < 0 else "➡️")
            lines.append(
                f"| {meta['emoji']} {meta['name']} | {d['curr']} | {d['prev']} | {delta_emoji_b} {d['delta']:+d} |"
            )
        # 新增 0 源
        new_zeros = cross.get("zero_sources_delta", [])
        if new_zeros:
            lines.append("")
            lines.append(f"> ⚠️ **新增 0 源**：`{', '.join(new_zeros)}`（上周有命中，本周连续 0）")
    lines.append("")
    lines.append("---")
    lines.append("")

    # ============== 3. 板块热度图（v2.2 新增；全规则） ==============
    lines.append("## 🌡️ 板块热度图")
    lines.append("")
    lines.append("| 话题板块 | 本周条数 | 热度 | 主题词 |")
    lines.append("|---------|---------|------|--------|")
    for board in BOARD_ORDER:
        ws = agg["week_stats"][board]
        meta = BOARD_META[board]
        # 热度判断（v2.2 规则；议题 A B 方案：全规则，无 LLM）
        days_with = [s for s in summaries if s.exists and s.board_stats.get(board, {}).get("total", 0) > 0]
        if ws["total"] == 0:
            heat = "📉 静默"
        elif ws["total"] >= 15:
            heat = "📈 升温"
        elif ws["total"] >= 5:
            heat = "➡️ 平稳"
        else:
            heat = "📉 偏低"
        kws = "、".join(agg["week_keywords"].get(board, [])[:3]) or "—"
        lines.append(
            f"| {meta['emoji']} {meta['name']} | {ws['total']} | {heat} | {kws} |"
        )
    lines.append("")
    lines.append("---")
    lines.append("")

    # ============== 4. 周度 Top 10（v2.2 新增） ==============
    lines.append("## ⭐ 周度 Top 10（按 score 排序，跨板块）")
    lines.append("")
    if agg["weekly_top10"]:
        lines.append("| # | 板块 | 标题 | 来源 | 分数 | 评级 |")
        lines.append("|---|------|------|------|------|------|")
        for idx, it in enumerate(agg["weekly_top10"], 1):
            board = it.get("board", "")
            meta = BOARD_META.get(board, {"emoji": "·", "name": board})
            title = it.get("title", "")
            url = it.get("url", "")
            src = it.get("source", "")
            score = it.get("score", 0)
            priority = it.get("priority", "")
            priority_emoji = {"high": "🔴", "mid": "🟡", "low": "🟢"}.get(priority, "·")
            title_disp = f"[{title[:50]}{'…' if len(title) > 50 else ''}]({url})" if url else title[:50]
            lines.append(
                f"| {idx} | {meta['emoji']} {meta['name']} | {title_disp} | `{src}` | {score} | {priority_emoji} |"
            )
    else:
        lines.append("本周无高优条目。")
    lines.append("")
    lines.append("---")
    lines.append("")

    # ============== 5. 跨话题宏观观察（v1.5 沿用） ==============
    lines.append("## 🌐 跨话题宏观观察（Top 主线信号）")
    lines.append("")
    if agg["top_signals"]:
        for idx, (display, count) in enumerate(agg["top_signals"], 1):
            sources = sorted(agg["signal_sources"].get(display, []))
            src_str = "、".join(f"`{s}`" for s in sources) if sources else "—"
            lines.append(f"{idx}. **{display}**（本周累计 {count} 条 · 跨源 {len(sources)} 个：{src_str}）")
    else:
        lines.append("1. 本周无跨源主线信号。")
    lines.append("")
    lines.append("---")
    lines.append("")

    # ============== 6. 各板块趋势（v2.2 砍 LLM，全规则） ==============
    lines.append("## 📈 各板块趋势")
    lines.append("")
    for board in BOARD_ORDER:
        ws = agg["week_stats"][board]
        meta = BOARD_META[board]
        days_with = [s for s in summaries if s.exists and s.board_stats.get(board, {}).get("total", 0) > 0]
        # 热度判断（与板块热度图一致）
        if ws["total"] == 0:
            heat = "📉 静默"
        elif ws["total"] >= 15:
            heat = "📈 升温"
        elif ws["total"] >= 5:
            heat = "➡️ 平稳"
        else:
            heat = "📉 偏低"
        kws = "、".join(agg["week_keywords"].get(board, [])) or "—"
        lines.append(
            f"### {meta['emoji']} {meta['name']}（{heat}）\n"
            f"- **本周条数**：{ws['total']}（🔴 {ws['high']} / 🟡 {ws['mid']} / 🟢 {ws['low']}）\n"
            f"- **主题词**：{kws}\n"
            f"- **覆盖天数**：{len(days_with)} / 7"
        )
        lines.append("")
    lines.append("---")
    lines.append("")

    # ============== 7. 每日条数（v1.5 沿用） ==============
    lines.append("## 📅 每日条数（按板块）")
    lines.append("")
    lines.append("| 日期 | LLM | Agent | 数字人 | 行业 | 其他 | 合计 | 一句话 |")
    lines.append("|------|-----|-------|-------|------|------|------|--------|")
    for s in summaries:
        if not s.exists:
            lines.append(f"| {s.date} | — | — | — | — | — | — | （日报缺失）|")
            continue
        row = [f"{s.date}"]
        day_total = 0
        for board in BOARD_ORDER:
            n = s.board_stats.get(board, {}).get("total", 0)
            row.append(str(n) if n else "·")
            day_total += n
        row.append(str(day_total))
        ols = (s.one_line_summary or "")[:60] + ("…" if len(s.one_line_summary or "") > 60 else "")
        row.append(ols or "—")
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")
    lines.append("---")
    lines.append("")

    # ============== 8. 本周洞察（B 方案：白名单 4 类，0 LLM） ==============
    lines.append("## 🌟 本周洞察（白名单 4 类 · 100% 规则）")
    lines.append("")
    insights = build_insights_whitelist(agg, summaries)
    for i, ins in enumerate(insights, 1):
        lines.append(f"{i}. {ins}")
    lines.append("")
    lines.append("---")
    lines.append("")

    # ============== 9. 信息源覆盖（v2.2 重构） ==============
    lines.append("## 📎 信息源覆盖")
    lines.append("")
    if agg["source_days"]:
        lines.append("| 来源 | 命中天数 | 占比 | Top 10 提到次数 |")
        lines.append("|------|---------|------|---------------|")
        for src in sorted(agg["source_days"].keys(), key=lambda x: -len(agg["source_days"][x])):
            n = len(agg["source_days"][src])
            lines.append(
                f"| `{src}` | {n} / 7 | {n/7*100:.0f}% | {agg['source_mentions'][src]} |"
            )
    else:
        lines.append("本周无命中源。")
    if agg.get("weekly_zero_sources"):
        lines.append("")
        lines.append(
            f"> ⚠️ **本周 0 命中源**（{len(agg['weekly_zero_sources'])} 个）："
            f"`{', '.join(agg['weekly_zero_sources'])}`（连续 7 天无数据，建议排查抓取 / 评估是否弃用）"
        )
    lines.append("")

    # ============== 10. 元信息（v1.5 沿用 + v2.2 增强） ==============
    lines.append("## 📈 元信息")
    lines.append("")
    lines.append(f"- **周标识**：{label}")
    lines.append(f"- **汇总周期**：{full_date_range}")
    lines.append(f"- **日报覆盖**：{agg['days_with_data']} / 7 天")
    lines.append(f"- **总条数**：{total}（🔴 {total_high} / 🟡 {total_mid} / 🟢 {total_low}）")
    lines.append(f"- **数据源**：daily v2.0 JSON（schema 2.0）")
    lines.append(f"- **LLM 精炼**：0 次调用（议题 A B 方案：100% 规则渲染）")
    if elapsed_seconds > 0:
        lines.append(f"- **生成耗时**：{elapsed_seconds:.2f}s")
    lines.append("")

    # 底部 log
    lines.append(
        f"*汇总周期：{date_range} · 数据来源：{agg['days_with_data']} 天 daily v2.0 JSON · "
        f"🤖 LLM 精炼：0 次 · 报告生成：AI调研专家*"
    )
    lines.append("")

    return "\n".join(lines)


# ============== 入口 ==============

def main():
    parser = argparse.ArgumentParser(description="生成每周汇总 Markdown 报告 v2.2（议题 H：weekly 重做）")
    parser.add_argument("--week", help="ISO 周标识 (YYYY-Www)，默认 = 本周 Asia/Shanghai")
    parser.add_argument("--daily-json-root", default="data/published/daily",
                        help="daily v2.0 JSON 根目录（默认 data/published/daily）")
    parser.add_argument("--out-dir", default="每周汇总", help="输出目录")
    parser.add_argument("--dry-run", action="store_true", help="只打印到 stdout，不落盘")
    parser.add_argument("--no-cross-week", action="store_true",
                        help="跳过跨周对比（v2.2 默认开启；用此 flag 关闭）")
    parser.add_argument("--no-llm", action="store_true", help="兼容 v1.5 参数（v2.2 已默认 0 LLM）")
    args = parser.parse_args()

    if args.week:
        m = re.match(r"^(\d{4})-W(\d{1,2})$", args.week)
        if not m:
            print(f"[ERROR] --week 格式错误: {args.week} (应为 YYYY-Www)", file=sys.stderr)
            return 1
        year, week = int(m.group(1)), int(m.group(2))
    else:
        year, week = current_week()

    monday, sunday = week_dates(year, week)
    daily_json_root = (_REPO_ROOT / args.daily_json_root).resolve()
    out_dir = (_REPO_ROOT / args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    import time
    t0 = time.time()

    # 加载本周 7 天 daily v2.0 JSON
    summaries = load_week_daily_jsons(monday, sunday, daily_json_root)
    days_with_json = sum(1 for s in summaries if s.exists)
    days_with_data = sum(1 for s in summaries if s.exists and s.total_items > 0)
    print(f"[i] 本周 daily v2.0 JSON 覆盖：{days_with_json}/7 天，命中数据：{days_with_data} 天", file=sys.stderr)

    # 聚合本周（一次；供 cross + render 复用）
    agg = aggregate_week(summaries)

    # 加载上周 daily v2.0 JSON（跨周对比）
    cross: dict | None = None
    if not args.no_cross_week:
        prev_y, prev_w = prev_week(year, week)
        prev_monday, prev_sunday = week_dates(prev_y, prev_w)
        try:
            prev_summaries = load_week_daily_jsons(prev_monday, prev_sunday, daily_json_root)
            prev_agg = aggregate_week(prev_summaries)
            cross = cross_week_compare(agg, prev_agg)
            print(f"[i] 跨周对比：上周 {prev_y}-W{prev_w:02d}", file=sys.stderr)
        except Exception as e:
            cross = {"available": False, "reason": f"上周聚合失败: {e}"}
            print(f"[WARN] 跨周对比失败: {e}（降级为无对比数据）", file=sys.stderr)

    # 渲染
    md = render_weekly_report(year, week, monday, sunday, summaries, agg,
                              cross=cross,
                              elapsed_seconds=time.time() - t0)

    if args.dry_run:
        print(md)
        return 0

    out_path = out_dir / f"{week_label(year, week)}.md"
    out_path.write_text(md, encoding="utf-8")
    final_total = sum(s["total"] for s in agg["week_stats"].values())
    print(f"[OK] 写入 {out_path}  | 总条数 {final_total} | LLM 0 次 | {days_with_data}/7 天数据")
    return 0


if __name__ == "__main__":
    sys.exit(main())
