"""
每日资讯生成器
============

读 `data/raw/<source>/<date>.json` → 按话题板块归类 → 评分排序 → 渲染模板 → 落盘 `每日资讯/<date>.md`

用法：
    python -m scripts.generators.generate_daily --date 2026-06-12
    python -m scripts.generators.generate_daily --date 2026-06-12 --dry-run
    python -m scripts.generators.generate_daily  # 默认 = 昨天（Asia/Shanghai）
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# 允许 `python -m scripts.generators.generate_daily` 独立运行
_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent  # scripts/generators → AI资讯追踪
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "generators"))

from _utils_gen import (  # noqa: E402
    BOARD_META,
    BOARD_ORDER,
    ScoredItem,
    classify_board,
    clean_text,
    extract_keywords,
    fmt_clock,
    fmt_time_cn,
    generate_impact,
    parse_published,
    score_item,
)


# 板块输出顺序（从 _utils_gen 导入）
# BOARD_ORDER = ["LLM", "Agent", "数字人", "其他"]


# ============== 数据读取 ==============

def load_raw_items(date_iso: str, raw_root: Path) -> list[dict]:
    """读取 data/raw/*/date_iso.json 中所有非空 items"""
    items: list[dict] = []
    per_source: dict[str, int] = {}
    if not raw_root.exists():
        return items
    for src_dir in sorted(raw_root.iterdir()):
        if not src_dir.is_dir():
            continue
        f = src_dir / f"{date_iso}.json"
        if not f.exists():
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            print(f"[WARN] 读取失败: {f}: {e}", file=sys.stderr)
            continue
        for it in data.get("items") or []:
            items.append(it)
        per_source[src_dir.name] = data.get("count", 0)
    return items, per_source


# ============== 评分 + 归类 ==============

def build_scored_items(raw_items: list[dict], date_iso: str) -> list[ScoredItem]:
    """对原始 items 做清洗、评分、归类"""
    out: list[ScoredItem] = []
    for it in raw_items:
        title = clean_text(it.get("title", ""))
        if not title:
            continue
        summary = clean_text(it.get("summary", ""))
        url = it.get("url", "").strip()
        if not url:
            continue
        source = it.get("source", "")
        board = classify_board(source)
        published_dt = parse_published(it.get("published", ""), date_iso)
        score, grade, matched = score_item(it, date_iso)
        out.append(ScoredItem(
            source=source,
            board=board,
            title=title,
            url=url,
            published=published_dt.isoformat(),
            published_dt=published_dt,
            summary=summary,
            author=it.get("author"),
            tags=list(it.get("tags") or []),
            score=score,
            grade=grade,
            matched_tags=matched,
        ))
    return out


def group_by_board(items: list[ScoredItem]) -> dict[str, list[ScoredItem]]:
    """按板块分组 + 板块内排序：score desc, published_dt desc"""
    groups: dict[str, list[ScoredItem]] = defaultdict(list)
    for it in items:
        groups[it.board].append(it)
    for board in groups:
        groups[board].sort(key=lambda x: (-x.score, -x.published_dt.timestamp()))
        # 每个板块限 Top 10
        groups[board] = groups[board][:10]
    return groups


# ============== 渲染 ==============

def render_board_section(board: str, items: list[ScoredItem], date_iso: str) -> str:
    """渲染单个板块的 Markdown 段"""
    meta = BOARD_META.get(board, BOARD_META["其他"])
    emoji = meta["emoji"]
    name = meta["name"]
    impact_hint = meta["impact_hint"]

    lines: list[str] = []
    lines.append(f"## {emoji} {name} | Top {len(items)}")
    lines.append("")

    if not items:
        lines.append(f"> 昨日无新增。")
        lines.append("")
        return "\n".join(lines)

    # 头条：详细展开
    head = items[0]
    lines.append(f"### 1. {head.title}")
    lines.append(f"- **来源**：[{head.source}]({head.url}) ｜ **时间**：{fmt_time_cn(head.published_dt)}")
    lines.append(f"- **重要程度**：{head.grade} {'高' if head.grade == '🔴' else '中等' if head.grade == '🟡' else '一般'}")
    if head.summary:
        lines.append(f"- **核心内容**：{head.summary}")
    lines.append(f"- **影响分析**：{generate_impact(head.tags, head.title, head.summary)}（{impact_hint}）")
    if head.matched_tags:
        tag_md = " ".join(f"`#{t}`" for t in head.matched_tags)
        lines.append(f"- **标签**：{tag_md}")
    lines.append("")

    # 2-N：简版列表
    if len(items) > 1:
        lines.append(f"### 其余 {len(items) - 1} 条（简版）")
        lines.append("")
        for idx, it in enumerate(items[1:], start=2):
            tag_md = ""
            if it.matched_tags:
                tag_md = " " + " ".join(f"`#{t}`" for t in it.matched_tags[:3])
            short_summary = it.summary[:120] + ("…" if len(it.summary) > 120 else "")
            lines.append(
                f"{idx}. **{it.grade}** [{it.title}]({it.url}) — {it.source} · "
                f"{fmt_time_cn(it.published_dt)} — {short_summary}{tag_md}"
            )
        lines.append("")

    return "\n".join(lines)


def render_daily_report(
    date_iso: str,
    groups: dict[str, list[ScoredItem]],
    per_source: dict[str, int],
    fetched_at: str,
) -> str:
    """渲染完整每日资讯 Markdown"""
    total = sum(len(v) for v in groups.values())
    high = sum(1 for v in groups.values() for it in v if it.grade == "🔴")
    mid = sum(1 for v in groups.values() for it in v if it.grade == "🟡")
    low = sum(1 for v in groups.values() for it in v if it.grade == "🟢")

    # 提取主题词（按板块）
    board_keywords: dict[str, list[str]] = {}
    for board, items in groups.items():
        kws = extract_keywords(items, top_n=4)
        board_keywords[board] = kws

    lines: list[str] = []
    lines.append(f"# 🤖 AI每日资讯 | {date_iso}（昨日）")
    lines.append("")
    lines.append(f"> **数据范围**：{date_iso} 00:00 ~ {date_iso} 23:59（昨日全天，不含当日）")
    lines.append(f"> **话题板块**：🧠 LLM 发展 · 💻 编程 Agent · 🧍 数字人 · 📦 其他（可扩展）")
    lines.append(f"> **每版块条数**：Top 10（按\"重要度 + 时新性 + 影响力\"综合排序；不足 10 条则按实际条数展示）")
    lines.append("")
    lines.append("---")
    lines.append("")

    # 速览表
    lines.append("## 📊 昨日速览")
    lines.append("")
    lines.append("| 话题板块 | 条数 | 🔴 高优 | 🟡 中等 | 🟢 一般 | 主题词 |")
    lines.append("|---------|------|--------|--------|--------|-------|")
    total_high = total_mid = total_low = 0
    for board in BOARD_ORDER:
        items = groups.get(board, [])
        meta = BOARD_META.get(board, BOARD_META["其他"])
        emoji = meta["emoji"]
        name = meta["name"]
        h = sum(1 for it in items if it.grade == "🔴")
        m = sum(1 for it in items if it.grade == "🟡")
        l = sum(1 for it in items if it.grade == "🟢")
        total_high += h; total_mid += m; total_low += l
        kws = "、".join(board_keywords.get(board, [])) or "—"
        lines.append(f"| {emoji} {name} | {len(items)} | {h} | {m} | {l} | {kws} |")
    lines.append(f"| **合计** | **{total}** | **{total_high}** | **{total_mid}** | **{total_low}** | — |")
    lines.append("")

    # 一句话总结（基于实际数据动态生成）
    summary_line = build_one_line_summary(date_iso, groups, per_source)
    lines.append(f"> **昨日一句话总结**：{summary_line}")
    lines.append("")
    lines.append("---")
    lines.append("")

    # 各板块
    for board in BOARD_ORDER:
        items = groups.get(board, [])
        lines.append(render_board_section(board, items, date_iso))

    # 跨话题洞察（动态）
    lines.append("## 💡 跨话题洞察")
    lines.append("")
    insights = build_cross_insights(groups)
    if insights:
        for i, ins in enumerate(insights, 1):
            lines.append(f"{i}. {ins}")
    else:
        lines.append("1. 昨日数据较少，跨话题趋势暂不显著；建议扩大抓取源覆盖后重新评估。")
    lines.append("")
    lines.append("---")
    lines.append("")

    # 待跟进
    lines.append("## 📌 待跟进")
    lines.append("")
    todos = build_todos(groups)
    if todos:
        for t in todos:
            lines.append(f"- [ ] {t}")
    else:
        lines.append("- [ ] 暂无明确待跟进项。")
    lines.append("")
    lines.append("---")
    lines.append("")

    # 信息源记录（动态生成）
    lines.append("## 📎 信息源记录")
    lines.append("")
    lines.append("| 板块 | 抓取来源 | 命中条数 | 状态 |")
    lines.append("|------|---------|---------|------|")
    for board in BOARD_ORDER:
        meta = BOARD_META.get(board, BOARD_META["其他"])
        name = meta["name"]
        items = groups.get(board, [])
        # 找出该板块对应的源
        src_in_board = sorted({it.source for it in items})
        all_srcs = [s for s, b in [(s, classify_board(s)) for s in per_source.keys()] if b == board]
        if not src_in_board and not all_srcs:
            continue
        for s in (src_in_board or all_srcs):
            count = per_source.get(s, 0)
            mark = "✅" if count > 0 else "⚪"
            lines.append(f"| {name} | {s} | {count} | {mark} |")
    lines.append("")
    lines.append(f"*采集时间：{fmt_clock(datetime.fromisoformat(fetched_at).astimezone(ZoneInfo('Asia/Shanghai')))} · "
                 f"信息源数量：{len(per_source)} · 报告生成：AI调研专家*")
    lines.append("")

    return "\n".join(lines)


def build_one_line_summary(date_iso: str, groups: dict[str, list[ScoredItem]], per_source: dict[str, int]) -> str:
    """生成一句话总结"""
    total = sum(len(v) for v in groups.values())
    if total == 0:
        return f"{date_iso} 全网 AI 资讯较少，主要源（OpenAI/DeepMind/LangChain/Replicate/Runway）均无新增。"

    high_items = [it for v in groups.values() for it in v if it.grade == "🔴"]
    parts: list[str] = []
    if high_items:
        first = high_items[0]
        parts.append(f"重点关注 [{first.source}]({first.url}) 发布的「{first.title}」")
    # 板块覆盖
    boards_with_items = [b for b in BOARD_ORDER if groups.get(b)]
    if boards_with_items:
        board_names = "、".join(BOARD_META[b]["name"] for b in boards_with_items)
        parts.append(f"覆盖 {board_names}")
    parts.append(f"共 {total} 条命中")
    return "；".join(parts) + "。"


def build_cross_insights(groups: dict[str, list[ScoredItem]]) -> list[str]:
    """生成跨话题洞察"""
    insights: list[str] = []

    llm_items = groups.get("LLM", [])
    agent_items = groups.get("Agent", [])

    # LLM × Agent 交叉
    if llm_items and agent_items:
        insights.append("**LLM × 编程 Agent**：昨日 LLM 官方源（OpenAI）与 Agent 框架源（LangChain）均有动作，"
                        "提示基础模型能力正在向应用层（Agent 工具调用、沙箱隔离）继续渗透。")

    # 数字人 × 视频生成
    avatar_items = groups.get("数字人", [])
    avatar_video = any(
        any(re.search(r"video|视频|avatar|数字人|runway", it.title + it.summary, re.I) for it in avatar_items)
        for _ in [1]
    )
    if avatar_video and llm_items:
        insights.append("**LLM × 数字人**：数字人 / 视频生成类内容持续更新，与多模态基础模型演进形成正反馈。")

    # 行业宏观（基于是否有头部厂商动作）
    has_top = any(it.source in {"openai", "deepmind", "anthropic"} for it in llm_items)
    if has_top:
        insights.append("**行业宏观**：头部模型厂商持续输出（OpenAI Academy / 案例研究等），"
                        "AI 行业重心从「能力突破」逐步转向「企业落地与培训」。")

    # 风险提示
    risks = []
    for it in (llm_items + agent_items):
        blob = (it.title + it.summary).lower()
        if re.search(r"safety|安全|regulation|监管|泄露|leak|breach", blob):
            risks.append("存在安全/合规相关信号，需持续关注监管动态。")
            break
    if risks:
        insights.extend(risks)

    return insights


def build_todos(groups: dict[str, list[ScoredItem]]) -> list[str]:
    """生成待跟进事项"""
    todos: list[str] = []
    high_items = [it for it in (it for v in groups.values() for it in v) if it.grade == "🔴"]
    for it in high_items[:3]:
        todos.append(f"跟进 [{it.source}]「{it.title}」后续影响与生态反应")
    if not high_items:
        todos.append("昨日无高优条目，可考虑扩大抓取源覆盖（如 HF / GitHub Trending）")
    return todos


# ============== 入口 ==============

def main():
    parser = argparse.ArgumentParser(description="生成每日资讯 Markdown 报告")
    parser.add_argument("--date", help="目标日期 YYYY-MM-DD（默认 = 昨天 Asia/Shanghai）")
    parser.add_argument("--raw-root", default="data/raw", help="抓取数据根目录")
    parser.add_argument("--out-dir", default="每日资讯", help="输出目录")
    parser.add_argument("--dry-run", action="store_true", help="只打印到 stdout，不落盘")
    args = parser.parse_args()

    # 默认日期 = 昨天（CST）
    if args.date:
        date_iso = args.date
    else:
        cst_now = datetime.now(ZoneInfo("Asia/Shanghai"))
        yesterday = cst_now - timedelta(days=1)
        date_iso = yesterday.strftime("%Y-%m-%d")

    raw_root = (_REPO_ROOT / args.raw_root).resolve()
    out_dir = (_REPO_ROOT / args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    if not raw_root.exists():
        print(f"[ERROR] 抓取数据根目录不存在: {raw_root}", file=sys.stderr)
        return 1

    # 加载 + 评分 + 分组
    raw_items, per_source = load_raw_items(date_iso, raw_root)
    if not raw_items:
        print(f"[WARN] {date_iso} 窗口内无任何抓取数据（请确认已运行抓取脚本）", file=sys.stderr)
    scored = build_scored_items(raw_items, date_iso)
    groups = group_by_board(scored)

    fetched_at = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
    md = render_daily_report(date_iso, groups, per_source, fetched_at)

    if args.dry_run:
        print(md)
        return 0

    out_path = out_dir / f"{date_iso}.md"
    out_path.write_text(md, encoding="utf-8")
    total = sum(len(v) for v in groups.values())
    print(f"[OK] 写入 {out_path}  | 板块 {sum(1 for v in groups.values() if v)} 个 / 总条数 {total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
