"""
每周汇总生成器
==============

读 `每日资讯/<date>.md` × 7 天 → 解析速览表 + 主线信号 + 一句话总结 →
聚合 → 渲染 → 落盘 `每周汇总/YYYY-WXX.md`

设计原则：
- 数据从"每日 md"读，不重新跑评分（避免和日报矛盾）
- LLM 增强 + 规则降级双路径
- 支持不全周（< 7 天）也能生成（标 WIP）

用法：
    python3 scripts/generators/generate_weekly.py --week 2026-W24
    python3 scripts/generators/generate_weekly.py                    # 默认 = 本周（周一~周日，Asia/Shanghai）
    python3 scripts/generators/generate_weekly.py --week 2026-W24 --dry-run
    python3 scripts/generators/generate_weekly.py --week 2026-W24 --no-llm
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
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
from _llm import LLMClient, LLMError, load_llm_client  # noqa: E402


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


# ============== LLM Prompt ==============

PROMPT_WEEKLY_TREND_SYSTEM = (
    "你是一个 AI 行业分析师，需要根据本周 7 天 AI 资讯，"
    "分析各话题板块的趋势与关键事件。"
    "硬性规则：\n"
    "① 输出按板块分块（5 个板块各一段）\n"
    "② 每段 2-3 句（≤ 80 字/句）\n"
    "③ 段首标热度：📈 上升 / ➡️ 平稳 / 📉 下降\n"
    "④ 每段至少提 1 个本周关键事件（带日期）\n"
    "⑤ 禁词：'本周/据悉/该文章'\n"
    "⑥ 板块名固定：LLM 发展 / Agent 框架与工具 / 数字人 / 行业动态 / 其他"
)

PROMPT_WEEKLY_TREND_USER_TMPL = (
    "本周（{date_range}）各板块数据：\n{context}\n"
    "请按规则输出各板块趋势："
)

PROMPT_WEEKLY_INSIGHTS_SYSTEM = (
    "你是一个 AI 行业分析师，需要从本周 7 天 AI 资讯中提炼 3-5 条跨话题宏观洞察。"
    "硬性规则：\n"
    "① 至少 2 条必须真正'跨话题'（同时涉及 2 个板块）\n"
    "② 覆盖维度多样：资本 / 监管 / 技术 / 商业化 / 风险\n"
    "③ **每条单行输出**，格式：`【主题】 内容描述。`（不要换行分标题和描述）\n"
    "④ 每条 40-80 字\n"
    "⑤ 禁词：'本周/据悉/该文章/我'"
)

PROMPT_WEEKLY_INSIGHTS_USER_TMPL = (
    "本周汇总：\n{context}\n"
    "请输出 3-5 条洞察："
)


# ============== LLM Usage ==============

class LLMUsage:
    def __init__(self) -> None:
        self.calls = 0
        self.failed = 0
        self.cache_hits = 0
        self.last_provider = ""
        self.last_model = ""

    def add_call(self, provider: str, model: str) -> None:
        self.calls += 1
        self.last_provider = provider
        self.last_model = model

    def add_fail(self) -> None:
        self.failed += 1

    def to_footer(self) -> str:
        if self.calls == 0 and self.failed == 0:
            return "未启用 LLM（无 .secrets 配置或 --no-llm）"
        base = f"{self.calls} 次调用"
        if self.cache_hits:
            base += f"（{self.cache_hits} 次缓存命中）"
        if self.failed:
            base += f"，{self.failed} 次降级到规则渲染"
        return f"{base} · {self.last_provider}/{self.last_model}"


def _safe_llm(usage: LLMUsage, client: LLMClient | None,
              system: str, user: str) -> str | None:
    if client is None:
        usage.add_fail()
        return None
    try:
        cache_key = client._cache_key(system, user)
        was_cached = client._cache_get(cache_key) is not None
        usage.add_call(client.provider, client.model)
        if was_cached:
            usage.cache_hits += 1
        return client.chat(system, user)
    except LLMError as e:
        print(f"[WARN] LLM 调用失败，降级到规则: {e}", file=sys.stderr)
        usage.add_fail()
        return None


# ============== 每日 md 解析 ==============

# 速览表行：| 🧠 LLM 发展 | 6 | 0 | 6 | 0 | agent、安全 |  (允许 **加粗** 和 — 兜底)
SPEEDRUN_ROW_RE = re.compile(
    r"\|\s*([^|]+?)\s*\|\s*(?:\*\*)?\s*(\d+)\s*(?:\*\*)?\s*\|\s*(?:\*\*)?\s*(\d+)\s*(?:\*\*)?\s*\|\s*(?:\*\*)?\s*(\d+)\s*(?:\*\*)?\s*\|\s*(?:\*\*)?\s*(\d+)\s*(?:\*\*)?\s*\|"
)

# 主线信号行：1. **🚀 产品发布**（4 条 · 涉及 `36kr`、`jiqizhixin`）
SIGNAL_ROW_RE = re.compile(r"^\s*(\d+)\.\s+\*\*([^*]+?)\*\*")

# 板块名 → emoji 映射（用于速览表匹配）
BOARD_EMOJI: dict[str, str] = {b: m["emoji"] for b, m in BOARD_META.items()}


@dataclass
class DailySummary:
    """从每日 md 解析的当日概要（不解析每条详情）"""
    date: str                       # YYYY-MM-DD
    file_path: Path                 # 源 md 路径
    exists: bool                    # md 是否存在
    board_stats: dict[str, dict]    # {board: {total, high, mid, low, keywords}}
    main_signals: list[dict]        # [{display, count, sources, first_item}, ...]
    one_line_summary: str           # 当日一句话总结
    total_items: int                # 当日总条数


def _parse_iso_daily(md_text: str, date_iso: str, file_path: Path) -> DailySummary:
    """解析每日 md → DailySummary"""
    board_stats: dict[str, dict] = {b: {"total": 0, "high": 0, "mid": 0, "low": 0, "keywords": []}
                                    for b in BOARD_ORDER}
    main_signals: list[dict] = []
    one_line_summary = ""
    total_items = 0

    lines = md_text.splitlines()
    section: str | None = None  # "speedrun" / "signals" / "summary" / None

    for line in lines:
        # 章节切换
        if "## 📊 昨日速览" in line:
            section = "speedrun"
            continue
        if "## 🔥 主线信号" in line:
            section = "signals"
            continue
        if "## 💡" in line or "## 📌" in line or "## 📎" in line or "## 📈" in line:
            section = None
            continue
        if "## " in line and section is not None and "## " + ("📊" if section == "speedrun" else "🔥") not in line:
            # 进入新章节
            section = None
        if line.startswith("---"):
            continue

        # 一句话总结（blockquote）
        sm = re.match(r">\s*\*\*昨日一句话总结\*\*[：:](.*)", line.strip())
        if sm:
            one_line_summary = sm.group(1).strip()
            continue

        if section == "speedrun":
            m = SPEEDRUN_ROW_RE.match(line)
            if not m:
                continue
            label = m.group(1).strip()
            n_total, n_high, n_mid, n_low = map(int, m.groups()[1:5])
            # 合计行：提取 total_items
            if "合计" in label:
                total_items = n_total
                continue
            # label 含 emoji + 板块名
            matched_board = None
            for board, emoji in BOARD_EMOJI.items():
                if emoji in label:
                    matched_board = board
                    break
            if matched_board is None:
                continue
            board_stats[matched_board]["total"] = n_total
            board_stats[matched_board]["high"] = n_high
            board_stats[matched_board]["mid"] = n_mid
            board_stats[matched_board]["low"] = n_low
            # 主题词在第 6 列
            parts = line.split("|")
            if len(parts) >= 7:
                kws = parts[6].strip()
                if kws and kws != "—":
                    board_stats[matched_board]["keywords"] = [k.strip() for k in kws.split("、") if k.strip()]

        elif section == "signals":
            sm = SIGNAL_ROW_RE.match(line)
            if sm:
                display = sm.group(2).strip()
                # 抓 count + sources
                count_m = re.search(r"（(\d+)\s*条", line)
                count = int(count_m.group(1)) if count_m else 0
                srcs_m = re.search(r"涉及\s+(.+?)\s*）", line)
                sources = []
                if srcs_m:
                    sources = [s.strip("`") for s in srcs_m.group(1).split("、") if s.strip()]
                main_signals.append({
                    "display": display,
                    "count": count,
                    "sources": sources,
                })

    # 找主线条目（每个信号的第一条关联，line 后几行）
    # 简化：只记 display/count/sources，不抓具体 URL（避免二次解析）

    return DailySummary(
        date=date_iso,
        file_path=file_path,
        exists=True,
        board_stats=board_stats,
        main_signals=main_signals,
        one_line_summary=one_line_summary,
        total_items=total_items,
    )


def load_week_summaries(monday: date, sunday: date,
                        daily_root: Path) -> list[DailySummary]:
    """读 7 天的每日 md（不存在的标 exists=False）"""
    out: list[DailySummary] = []
    cur = monday
    while cur <= sunday:
        iso = cur.isoformat()
        fp = daily_root / f"{iso}.md"
        if not fp.exists():
            out.append(DailySummary(
                date=iso, file_path=fp, exists=False,
                board_stats={b: {"total": 0, "high": 0, "mid": 0, "low": 0, "keywords": []} for b in BOARD_ORDER},
                main_signals=[], one_line_summary="", total_items=0,
            ))
        else:
            try:
                text = fp.read_text(encoding="utf-8")
                out.append(_parse_iso_daily(text, iso, fp))
            except Exception as e:
                print(f"[WARN] 解析失败: {fp}: {e}", file=sys.stderr)
                out.append(DailySummary(
                    date=iso, file_path=fp, exists=False,
                    board_stats={b: {"total": 0, "high": 0, "mid": 0, "low": 0, "keywords": []} for b in BOARD_ORDER},
                    main_signals=[], one_line_summary="", total_items=0,
                ))
        cur += timedelta(days=1)
    return out


# ============== 聚合 ==============

def aggregate_week(summaries: list[DailySummary]) -> dict:
    """聚合 7 天数据 → 周度统计"""
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
            signal_counter[sig["display"]] += sig["count"]
            for src in sig["sources"]:
                signal_sources[sig["display"]].add(src)
    top_signals = signal_counter.most_common(5)

    # 抓取覆盖（哪些日期存在日报）
    days_with_data = sum(1 for s in summaries if s.exists and s.total_items > 0)
    days_missing = 7 - days_with_data

    return {
        "week_stats": week_stats,
        "week_keywords": week_keywords,
        "top_signals": top_signals,
        "signal_sources": signal_sources,
        "days_with_data": days_with_data,
        "days_missing": days_missing,
    }


# ============== LLM 增强 ==============

def _build_trend_context(agg: dict, summaries: list[DailySummary]) -> str:
    """给 LLM 的本周趋势上下文"""
    parts: list[str] = []
    for board in BOARD_ORDER:
        ws = agg["week_stats"][board]
        meta = BOARD_META[board]
        parts.append(f"【{meta['name']}】共 {ws['total']} 条（🔴{ws['high']} / 🟡{ws['mid']} / 🟢{ws['low']}）")
        # 主题词
        kws = agg["week_keywords"].get(board, [])
        if kws:
            parts.append(f"  主题词：{', '.join(kws)}")
        # 该板块每日条数
        daily_lines = []
        for s in summaries:
            if s.exists:
                n = s.board_stats.get(board, {}).get("total", 0)
                daily_lines.append(f"{s.date[-5:]}={n}")
        if daily_lines:
            parts.append(f"  每日条数：{', '.join(daily_lines)}")
    return "\n".join(parts)


def _build_insights_context(agg: dict) -> str:
    """给 LLM 的本周洞察上下文"""
    parts: list[str] = []
    parts.append("本周累计主线信号 Top 5：")
    for display, count in agg["top_signals"]:
        sources = sorted(agg["signal_sources"].get(display, []))
        parts.append(f"- {display}（{count} 条，跨 {len(sources)} 个源：{', '.join(sources)}）")
    parts.append("\n本周各板块主题词：")
    for board in BOARD_ORDER:
        kws = agg["week_keywords"].get(board, [])
        if kws:
            parts.append(f"- {BOARD_META[board]['name']}：{', '.join(kws)}")
    return "\n".join(parts)


def llm_weekly_trend(client: LLMClient | None, context: str,
                     date_range: str, usage: LLMUsage) -> str | None:
    raw = _safe_llm(usage, client, PROMPT_WEEKLY_TREND_SYSTEM,
                    PROMPT_WEEKLY_TREND_USER_TMPL.format(date_range=date_range, context=context))
    return raw


def llm_weekly_insights(client: LLMClient | None, context: str,
                        usage: LLMUsage) -> list[str] | None:
    raw = _safe_llm(usage, client, PROMPT_WEEKLY_INSIGHTS_SYSTEM,
                    PROMPT_WEEKLY_INSIGHTS_USER_TMPL.format(context=context))
    if not raw:
        return None
    lines: list[str] = []
    for line in raw.splitlines():
        s = re.sub(r"^\s*[\-\*]?\s*\d+[\.、]?\s*", "", line).strip()
        if s and len(s) >= 15:
            lines.append(s)
    return lines[:5] if lines else None


# ============== 降级 ==============

def build_trend_fallback(agg: dict, summaries: list[DailySummary]) -> str:
    """规则版趋势（LLM 不可用时）"""
    parts: list[str] = []
    for board in BOARD_ORDER:
        ws = agg["week_stats"][board]
        meta = BOARD_META[board]
        # 热度判断
        days_with = [s for s in summaries if s.exists and s.board_stats.get(board, {}).get("total", 0) > 0]
        if ws["total"] >= 10:
            heat = "📈 上升"
        elif ws["total"] == 0:
            heat = "📉 下降"
        else:
            heat = "➡️ 平稳"
        kws = agg["week_keywords"].get(board, [])
        kw_str = "、".join(kws[:3]) if kws else "—"
        parts.append(
            f"### {meta['emoji']} {meta['name']}（{heat}）\n"
            f"- **本周条数**：{ws['total']}（🔴 {ws['high']} / 🟡 {ws['mid']} / 🟢 {ws['low']}）\n"
            f"- **主题词**：{kw_str}\n"
            f"- **覆盖天数**：{len(days_with)} / 7"
        )
    return "\n\n".join(parts)


def build_insights_fallback(agg: dict) -> list[str]:
    """规则版洞察（LLM 不可用时）"""
    insights: list[str] = []
    top = agg["top_signals"]
    if top:
        first_display, first_count = top[0]
        n_sources = len(agg["signal_sources"].get(first_display, []))
        if n_sources >= 3:
            insights.append(
                f"**跨源聚合**：{first_display} 本周累计 {first_count} 条，"
                f"跨 {n_sources} 个独立源（{', '.join(sorted(agg['signal_sources'][first_display]))}）确认，"
                f"提示该方向已是行业级共识而非单点噪音。"
            )
        elif n_sources >= 2:
            insights.append(
                f"**双源确认**：{first_display} 本周累计 {first_count} 条，"
                f"跨 {n_sources} 个源（{', '.join(sorted(agg['signal_sources'][first_display]))}）"
                f"被独立报道，信号强度较高。"
            )

    # 监管 / 资本类信号识别
    capital_signals = [(d, c) for d, c in top if "💰" in d or "资本" in d or "融资" in d]
    reg_signals = [(d, c) for d, c in top if "⚖️" in d or "监管" in d]
    if capital_signals:
        d, c = capital_signals[0]
        insights.append(f"**资本节奏**：{d} 本周累计 {c} 条，提示 AI 行业资本流动仍在加速。")
    if reg_signals:
        d, c = reg_signals[0]
        insights.append(f"**监管动态**：{d} 本周累计 {c} 条，监管侧动作需持续追踪。")

    # 板块空缺
    empty_boards = [b for b in BOARD_ORDER if agg["week_stats"][b]["total"] == 0]
    if empty_boards:
        names = "、".join(BOARD_META[b]["name"] for b in empty_boards)
        insights.append(f"**覆盖缺口**：本周 {names} 板块无新增，可能需评估是否补充对应源。")

    return insights[:5] if insights else ["本周数据较少，跨话题趋势暂不显著。"]


# ============== 渲染 ==============

def render_weekly_report(
    year: int, week: int, monday: date, sunday: date,
    summaries: list[DailySummary], agg: dict,
    llm: LLMClient | None, usage: LLMUsage,
    elapsed_seconds: float = 0.0,
) -> str:
    """渲染周汇总 Markdown"""
    label = week_label(year, week)
    date_range = f"{monday.strftime('%m.%d')} - {sunday.strftime('%m.%d')}"
    full_date_range = f"{monday.isoformat()} ~ {sunday.isoformat()}"
    total = sum(s["total"] for s in agg["week_stats"].values())
    total_high = sum(s["high"] for s in agg["week_stats"].values())
    total_mid = sum(s["mid"] for s in agg["week_stats"].values())
    total_low = sum(s["low"] for s in agg["week_stats"].values())

    lines: list[str] = []
    lines.append(f"# 📊 AI每周资讯汇总 | {year} 年第 {week} 周（{date_range}）")
    lines.append("")
    lines.append(f"> **汇总周期**：本周（{full_date_range}）")
    lines.append(f"> **生成时间**：{datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M')}（Asia/Shanghai）")
    lines.append("> **话题板块**：🧠 LLM 发展 · 💻 Agent 框架与工具 · 🧍 数字人 · 🏢 行业动态 · 📦 其他（可扩展）")
    lines.append("> **每版块条数**：周度 Top 10（按\"重要度 + 时新性\"排序；不足按实际）")
    lines.append("")

    # WIP 提示
    if agg["days_with_data"] < 7:
        lines.append(f"> ⚠️ **WIP**：本周 {agg['days_with_data']} 天数据可用，{agg['days_missing']} 天日报缺失（系统未生成或 0 命中）")
        lines.append("")

    lines.append("---")
    lines.append("")

    # 速览表
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
        # 规则版：取当天累计主线信号 Top 3
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

    # 跨话题宏观观察
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

    # 各板块趋势（LLM 优先，规则降级）
    lines.append("## 📈 各板块趋势")
    lines.append("")
    trend_text: str | None = None
    if llm and total > 0:
        ctx = _build_trend_context(agg, summaries)
        trend_text = llm_weekly_trend(llm, ctx, date_range, usage)
    if not trend_text:
        trend_text = build_trend_fallback(agg, summaries)
    lines.append(trend_text)
    lines.append("")
    lines.append("---")
    lines.append("")

    # 各板块每日条数小表
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
        ols = s.one_line_summary[:60] + ("…" if len(s.one_line_summary) > 60 else "")
        row.append(ols or "—")
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")
    lines.append("---")
    lines.append("")

    # 本周洞察
    lines.append("## 🌟 本周洞察")
    lines.append("")
    insights: list[str] | None = None
    if llm and total > 0:
        ctx = _build_insights_context(agg)
        insights = llm_weekly_insights(llm, ctx, usage)
    if not insights:
        insights = build_insights_fallback(agg)
    for i, ins in enumerate(insights, 1):
        lines.append(f"{i}. {ins}")
    lines.append("")
    lines.append("---")
    lines.append("")

    # 下周关注清单
    lines.append("## 🔮 下周关注清单")
    lines.append("")
    todos: list[str] = []
    # 基于本周主线信号 Top 3 推下周
    for display, count in agg["top_signals"][:3]:
        todos.append(f"持续追踪 **{display}**（本周 {count} 条），关注是否有新进展或主线延续")
    todos.append("对比本周与下周板块条数变化，识别趋势拐点（行业 / 监管 / 资本方向）")
    todos.append("抽样本周高优条目（🔴）做全文精读，更新对核心玩家的判断")
    for t in todos:
        lines.append(f"- [ ] {t}")
    lines.append("")
    lines.append("---")
    lines.append("")

    # 信息源覆盖
    lines.append("## 📎 信息源覆盖")
    lines.append("")
    # 7 天每个源被命中的天数（按日期去重）+ 主线条目里被提到的次数
    source_days: dict[str, set[str]] = defaultdict(set)  # src -> {date_iso}
    source_mentions: dict[str, int] = defaultdict(int)
    for s in summaries:
        if not s.exists:
            continue
        for sig in s.main_signals:
            for src in sig["sources"]:
                source_mentions[src] += 1
                source_days[src].add(s.date)
    if source_days:
        lines.append("| 来源 | 命中天数 | 占比 | 主线条目提到次数 |")
        lines.append("|------|---------|------|---------------|")
        for src in sorted(source_days.keys(), key=lambda x: -len(source_days[x])):
            n = len(source_days[src])
            lines.append(f"| `{src}` | {n} / 7 | {n/7*100:.0f}% | {source_mentions[src]} |")
    else:
        lines.append("本周无命中源。")
    lines.append("")

    # 元信息
    lines.append("## 📈 元信息")
    lines.append("")
    lines.append(f"- **周标识**：{label}")
    lines.append(f"- **汇总周期**：{full_date_range}")
    lines.append(f"- **日报覆盖**：{agg['days_with_data']} / 7 天")
    lines.append(f"- **总条数**：{total}（🔴 {total_high} / 🟡 {total_mid} / 🟢 {total_low}）")
    lines.append(f"- **LLM 精炼**：{usage.to_footer()}")
    if elapsed_seconds > 0:
        lines.append(f"- **生成耗时**：{elapsed_seconds:.1f}s")
    lines.append("")

    # 底部 log
    lines.append(
        f"*汇总周期：{date_range} · 数据来源：{agg['days_with_data']} 天日报 · "
        f"🤖 LLM 精炼：{usage.to_footer()} · 报告生成：AI调研专家*"
    )
    lines.append("")

    return "\n".join(lines)


# ============== 入口 ==============

def main():
    parser = argparse.ArgumentParser(description="生成每周汇总 Markdown 报告")
    parser.add_argument("--week", help="ISO 周标识 (YYYY-Www)，默认 = 本周 Asia/Shanghai")
    parser.add_argument("--daily-dir", default="每日资讯", help="每日 md 目录")
    parser.add_argument("--out-dir", default="每周汇总", help="输出目录")
    parser.add_argument("--dry-run", action="store_true", help="只打印到 stdout，不落盘")
    parser.add_argument("--llm", default=None, help="临时指定 LLM provider")
    parser.add_argument("--no-llm", action="store_true", help="强制关闭 LLM 精炼")
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
    daily_root = (_REPO_ROOT / args.daily_dir).resolve()
    out_dir = (_REPO_ROOT / args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    # 加载 LLM
    usage = LLMUsage()
    if args.no_llm:
        llm: LLMClient | None = None
        print("[i] --no-llm 已指定，跳过 LLM 加载", file=sys.stderr)
    else:
        llm = load_llm_client(provider=args.llm)
        if llm is None:
            print("[i] 未配置 LLM（无 .secrets 或字段缺失），全部走规则渲染", file=sys.stderr)
        else:
            print(f"[i] LLM 客户端已加载: {llm!r}", file=sys.stderr)

    import time
    t0 = time.time()

    # 加载 + 聚合
    summaries = load_week_summaries(monday, sunday, daily_root)
    days_with_md = sum(1 for s in summaries if s.exists)
    days_with_data = sum(1 for s in summaries if s.exists and s.total_items > 0)
    print(f"[i] 本周日报覆盖：{days_with_md}/7 天，命中数据：{days_with_data} 天", file=sys.stderr)

    agg = aggregate_week(summaries)

    # 渲染
    md = render_weekly_report(year, week, monday, sunday, summaries, agg,
                              llm=llm, usage=usage,
                              elapsed_seconds=time.time() - t0)

    if args.dry_run:
        print(md)
        return 0

    out_path = out_dir / f"{week_label(year, week)}.md"
    out_path.write_text(md, encoding="utf-8")
    total = sum(s["total"] for s in agg["week_stats"].values())
    print(f"[OK] 写入 {out_path}  | 总条数 {total} | {usage.to_footer()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
