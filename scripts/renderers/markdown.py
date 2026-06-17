"""
renderers/markdown.py — daily JSON → markdown 字符串（完整内容）

用途：
- v2.0 daily 报告的 markdown 渲染器
- 复刻 generate_daily.py:render_daily_report() 的输出（但数据源从 daily JSON 来，不是从 groups / per_source）
- v1.4 兼容：已生成的 每日资讯/<date>.md 文件继续保留

设计：
- 纯函数：render(daily_json) -> str
- 不调 LLM（LLM 精炼在 daily JSON 里）
- 不读 md（md 是渲染产物之一）
- 复用 daily JSON 的 boards.top_items / main_signals.items / one_line_summary 等结构化字段
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo


TZ_SH = ZoneInfo("Asia/Shanghai")
BOARD_ORDER = ["LLM", "Agent", "数字人", "行业", "其他"]


def _priority_label(priority: str) -> str:
    return {"high": "🔴", "mid": "🟡", "low": "🟢"}.get(priority, "·")


def render(daily_json: dict) -> str:
    """daily v2.0 JSON → markdown 字符串"""
    if not daily_json:
        return ""

    date_iso = daily_json.get("report_date", "")
    meta = daily_json.get("meta", {})
    boards = daily_json.get("boards", {})
    main_signals = daily_json.get("main_signals", [])
    one_line = daily_json.get("one_line_summary", {}).get("text", "")
    llm_usage = daily_json.get("llm_usage", {})

    lines: list[str] = []
    # 标题
    lines.append(f"# 🤖 AI每日资讯 | {date_iso}（昨日）")
    lines.append("")
    lines.append(f"> **数据范围**：{date_iso} 00:00 ~ {date_iso} 23:59（昨日全天，不含当日）")
    lines.append("> **话题板块**：🧠 LLM 发展 · 💻 Agent 框架与工具 · 🧍 数字人 · 🏢 行业动态 · 📦 其他（可扩展）")
    lines.append("> **每版块条数**：Top 10（按\"重要度 + 时新性 + 影响力\"综合排序；不足 10 条则按实际条数展示）")
    lines.append("")
    lines.append("---")
    lines.append("")

    # 速览表
    lines.append("## 📊 昨日速览")
    lines.append("")
    lines.append("| 话题板块 | 条数 | 🔴 高优 | 🟡 中等 | 🟢 一般 | 主题词 |")
    lines.append("|---------|------|--------|--------|-------|--------|")
    total = meta.get("total_items", 0)
    high = meta.get("high_priority_count", 0)
    mid = meta.get("mid_priority_count", 0)
    low = meta.get("low_priority_count", 0)
    for board in BOARD_ORDER:
        b = boards.get(board, {})
        name = b.get("name", board)
        emoji = b.get("emoji", "📦")
        kws = "、".join(b.get("keywords", [])) or "—"
        lines.append(
            f"| {emoji} {name} | {b.get('total', 0)} | {b.get('high', 0)} | {b.get('mid', 0)} | {b.get('low', 0)} | {kws} |"
        )
    lines.append(f"| **合计** | **{total}** | **{high}** | **{mid}** | **{low}** | — |")
    lines.append("")

    # 主线信号
    if main_signals:
        lines.append("## 🔥 主线信号")
        lines.append("")
        for idx, sig in enumerate(main_signals, 1):
            sources_str = "、".join(f"`{s}`" for s in sig.get("sources", []))
            lines.append(
                f"{idx}. **{sig.get('display', '')}**（{sig.get('count', 0)} 条 · 涉及 {sources_str}）"
            )
            for item in sig.get("items", [])[:5]:
                pri = _priority_label(item.get("priority", "mid"))
                lines.append(
                    f"   - {pri} [{item.get('title', '')}]({item.get('url', '')}) — {item.get('source', '')}"
                )
        lines.append("")
        lines.append("---")
        lines.append("")

    # 一句话总结
    if one_line:
        lines.append(f"> **昨日一句话总结**：{one_line}")
        lines.append("")
        lines.append("---")
        lines.append("")

    # 各板块 Top 10
    for board in BOARD_ORDER:
        b = boards.get(board, {})
        emoji = b.get("emoji", "📦")
        name = b.get("name", board)
        top_items = b.get("top_items", [])
        lines.append(f"## {emoji} {name} | Top {len(top_items)}")
        lines.append("")
        if not top_items:
            lines.append("> 昨日无新增。")
            lines.append("")
            continue
        for idx, item in enumerate(top_items, 1):
            pri = _priority_label(item.get("priority", "mid"))
            title = item.get("title", "")
            url = item.get("url", "")
            source = item.get("source", "")
            summary = item.get("summary", "")
            tag_md = ""
            tags = item.get("matched_tags", [])
            if tags:
                tag_md = " " + " ".join(f"`#{t}`" for t in tags[:3])
            lines.append(
                f"{idx}. {pri} [{title}]({url}) — {source}{tag_md}"
            )
            if summary:
                lines.append(f"   - {summary[:200]}{'…' if len(summary) > 200 else ''}")
        lines.append("")
        lines.append("---")
        lines.append("")

    # 元信息
    lines.append("## 📈 元信息")
    lines.append("")
    rm = daily_json.get("render_meta", {})
    n_src = meta.get("n_sources_configured", 0)
    n_hit = meta.get("n_sources_hit", 0)
    hit_rate = meta.get("hit_rate", 0.0) * 100
    lines.append(f"- **数据源**：{n_src} 个配置 / {n_hit} 个命中 / 命中率 {hit_rate:.0f}%")
    lines.append(f"- **总条数**：{total}（🔴 {high} / 🟡 {mid} / 🟢 {low}）")
    lines.append(f"- **LLM 精炼**：{llm_usage.get('footer', '未启用')}")
    elapsed = rm.get("elapsed_seconds", 0.0)
    if elapsed > 0:
        lines.append(f"- **生成耗时**：{elapsed:.1f}s")
    lines.append(f"- **schema_version**：{daily_json.get('schema_version', '2.0')}")
    lines.append("")

    # 底部 log
    generated_at = daily_json.get("generated_at", "")
    lines.append(
        f"*采集时间：{generated_at} · 信息源数量：{n_src} · "
        f"🤖 LLM 精炼：{llm_usage.get('footer', '未启用')} · "
        f"报告生成：AI调研专家 · 渲染器：renderers/markdown.py*"
    )
    lines.append("")

    return "\n".join(lines)
