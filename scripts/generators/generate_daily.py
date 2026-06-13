"""
每日资讯生成器
============

读 `data/raw/<source>/<date>.json` → 按话题板块归类 → 评分排序 → 渲染模板 → 落盘 `每日资讯/<date>.md`

用法：
    python -m scripts.generators.generate_daily --date 2026-06-12
    python -m scripts.generators.generate_daily --date 2026-06-12 --dry-run
    python -m scripts.generators.generate_daily                 # 默认 = 昨天（Asia/Shanghai）
    python -m scripts.generators.generate_daily --no-llm        # 强制关 LLM
    python -m scripts.generators.generate_daily --llm openai    # 临时切 provider
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
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
    extract_main_signals,
    fmt_clock,
    fmt_time_cn,
    generate_impact,
    parse_published,
    score_item,
)
from _llm import LLMClient, LLMError, load_llm_client  # noqa: E402


# ============== LLM Prompt 模板 ==============

PROMPT_SUMMARIZE_SYSTEM = (
    "你是一个 AI 行业分析师，擅长用 1-2 句中文精炼一段技术/产品文章的核心内容。"
    "要求：① 直接说事实，不写'本文/作者/该文章'开头 ② 不超过 80 字 ③ 句末可加句号"
)

PROMPT_SUMMARIZE_USER_TMPL = (
    "请基于以下原文，写 1-2 句中文摘要。\n"
    "原文：\n{article}\n"
    "输出："
)

PROMPT_IMPACT_SYSTEM_TMPL = (
    "你是一个 AI 行业分析师，擅长用 1-2 句中文分析某条 AI 资讯对行业的影响。"
    "分析角度：{impact_hint}。"
    "要求：① 具体到'对谁/什么场景/产生什么影响' ② 不超过 100 字 ③ 句末加句号"
)

PROMPT_IMPACT_USER_TMPL = (
    "摘要：{summary}\n"
    "原文（已截前 3000 字）：\n{article}\n"
    "输出："
)

# 简版 P0 改：每个非头条条目 1-2 句，含 1-2 个量化数据点
PROMPT_BRIEF_SYSTEM = (
    "你是一个 AI 资讯编辑，需要把一条 RSS 摘要压缩成 1-2 句中文简版。"
    "要求：① 总字数 ≤ 80 字 ② 必须保留 1-2 个量化数据点（如有数字、百分比、用户数）"
    " ③ 直接陈述事实 ④ 不用'OpenAI 宣布/据悉/本文/作者'等套话 ⑤ 句末加句号"
)

PROMPT_BRIEF_USER_TMPL = (
    "标题：{title}\n"
    "摘要：{summary}\n"
    "标签：{tags}\n"
    "请输出 1-2 句简版："
)

PROMPT_ONE_LINE_SYSTEM = (
    "你是一个 AI 行业分析师，擅长用 1 句中文总结今日 AI 资讯全貌。"
    "要求：① 60 字以内 ② 突出主线/重要动态（哪个领域/什么方向） ③ 不写'今日/据悉'开头"
)

PROMPT_ONE_LINE_USER_TMPL = (
    "今日 AI 资讯概览：\n{context}\n"
    "输出："
)

# 跨话题洞察 P3 改：先列 5 条候选 → 维度多样 + 主语不同 + 压缩到 2-3 条
PROMPT_INSIGHTS_SYSTEM = (
    "你是一个 AI 行业分析师，擅长从多板块 AI 资讯中提炼跨话题洞察。"
    "硬性规则：\n"
    "① 必须先列 5 条候选洞察（覆盖不同维度：产品 / 技术 / 趋势 / 行业 / 资本）\n"
    "② 再去重压缩到 2-3 条最终洞察\n"
    "③ 最终每条 30-50 字\n"
    "④ 禁止 2 条洞察用相同主语开头（如不能 2 条都以'OpenAI'开头）\n"
    "⑤ 至少 1 条必须真正'跨话题'（同时涉及 2 个板块）\n"
    "⑥ 禁词：'本文'、'据悉'、'今日'、'作者'、'该文章'"
)

PROMPT_INSIGHTS_USER_TMPL = (
    "各板块 Top 资讯：\n{context}\n"
    "请按规则输出 2-3 条最终洞察（每条 1 行，编号）："
)

# 待跟进 P6 改：生成"主语+动词+具体动作"的 action 清单
PROMPT_FOLLOWUP_SYSTEM = (
    "你是一个 AI 行业分析师，需要基于今日 AI 资讯生成 2-3 条读者可执行的 action。"
    "硬性规则：\n"
    "① 每条都是「主语（读者/团队/某角色） + 动词（做什么） + 具体动作」的祈使句\n"
    "② 动作必须可立即执行（不是'跟进 XX'这种空话）\n"
    "③ 字数 25-50 字\n"
    "④ 输出不带 markdown 编号"
)

PROMPT_FOLLOWUP_USER_TMPL = (
    "今日 Top 资讯：\n{context}\n"
    "请输出 2-3 条可执行 action（每条 1 行，祈使句）："
)


# ============== LLM 调用包装（含降级 + 成本统计） ==============

class LLMUsage:
    """单次报告生成的 LLM 用量累计"""

    def __init__(self) -> None:
        self.calls: int = 0
        self.failed: int = 0
        self.cache_hits: int = 0
        self.last_provider: str = ""
        self.last_model: str = ""

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
    """调一次 LLM；未配置 / 失败均返 None（调用方降级到规则）

    注：缓存命中算成功（不重复计数 cache_hits 由 chat() 内部统计）
    """
    if client is None:
        usage.add_fail()
        return None
    try:
        # 探测缓存命中（在 add_call 前）
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


def llm_summarize_article(client: LLMClient | None, article: str,
                          usage: LLMUsage) -> str | None:
    """摘要（核心内容）— 给定原文，返 1-2 句中文"""
    article = (article or "")[:6000]
    if not article.strip():
        return None
    return _safe_llm(usage, client, PROMPT_SUMMARIZE_SYSTEM,
                     PROMPT_SUMMARIZE_USER_TMPL.format(article=article))


def llm_impact(client: LLMClient | None, summary: str, article: str,
               impact_hint: str, usage: LLMUsage) -> str | None:
    """影响分析 — 1-2 句"""
    article = (article or "")[:3000]
    sys_msg = PROMPT_IMPACT_SYSTEM_TMPL.format(impact_hint=impact_hint)
    user_msg = PROMPT_IMPACT_USER_TMPL.format(summary=summary or "（无摘要）", article=article)
    return _safe_llm(usage, client, sys_msg, user_msg)


def llm_brief(client: LLMClient | None, title: str, summary: str,
              tags: list[str], usage: LLMUsage) -> str | None:
    """简版 — 1-2 句中文，含 1-2 个量化数据点"""
    user_msg = PROMPT_BRIEF_USER_TMPL.format(
        title=title or "（无标题）",
        summary=summary or "（无摘要）",
        tags="、".join(tags or []) or "（无标签）",
    )
    return _safe_llm(usage, client, PROMPT_BRIEF_SYSTEM, user_msg)


def llm_one_line_summary(client: LLMClient | None, context: str,
                         usage: LLMUsage) -> str | None:
    """一句话总结"""
    return _safe_llm(usage, client, PROMPT_ONE_LINE_SYSTEM,
                     PROMPT_ONE_LINE_USER_TMPL.format(context=context))


def llm_cross_insights(client: LLMClient | None, context: str,
                       usage: LLMUsage) -> list[str] | None:
    """跨话题洞察 — 返 list[str]（每条 1 行）"""
    raw = _safe_llm(usage, client, PROMPT_INSIGHTS_SYSTEM,
                    PROMPT_INSIGHTS_USER_TMPL.format(context=context))
    if not raw:
        return None
    # 按行拆；去 "1. " / "1、" 编号；丢弃"候选 1/2/3/4/5"中间过程（如有）
    lines: list[str] = []
    for line in raw.splitlines():
        s = re.sub(r"^\s*\d+[\.、]\s*", "", line).strip()
        if s and not s.startswith("候选") and "→" not in s[:5]:
            lines.append(s)
    return lines[:3] if lines else None


def llm_followup_actions(client: LLMClient | None, context: str,
                         usage: LLMUsage) -> list[str] | None:
    """待跟进 action 清单 — 返 list[str]（每条 1 行，祈使句）"""
    raw = _safe_llm(usage, client, PROMPT_FOLLOWUP_SYSTEM,
                    PROMPT_FOLLOWUP_USER_TMPL.format(context=context))
    if not raw:
        return None
    lines: list[str] = []
    for line in raw.splitlines():
        s = re.sub(r"^\s*[\-\*]?\s*\d+[\.、]?\s*", "", line).strip()
        if s and len(s) >= 8:  # 过滤过短的无意义行
            lines.append(s)
    return lines[:3] if lines else None


# ============== 数据读取 ==============

def load_raw_items(date_iso: str, raw_root: Path) -> tuple[list[dict], dict[str, int]]:
    """读取 data/raw/*/date_iso.json 中所有非空 items"""
    items: list[dict] = []
    per_source: dict[str, int] = {}
    if not raw_root.exists():
        return items, per_source
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
            extra=dict(it.get("extra") or {}),
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


# ============== 全文读取（渲染折叠区） ==============

def _read_article_path(it: ScoredItem, repo_root: Path) -> Path | None:
    """从 item.extra.article_path 解析成绝对 Path（文件存在时返回）"""
    extra = it.extra if isinstance(it.extra, dict) else {}
    p = extra.get("article_path")
    if not p:
        return None
    full = repo_root / p
    return full if full.exists() else None


def _read_article_text(it: ScoredItem, repo_root: Path, max_chars: int = 6000) -> str:
    """读取 article_path 对应的纯文本（去掉 frontmatter 注释头）；文件缺失返空"""
    p = _read_article_path(it, repo_root)
    if not p:
        return ""
    try:
        raw = p.read_text(encoding="utf-8")
    except Exception:
        return ""
    # 去掉 frontmatter 注释头
    raw = re.sub(r"^<!--.*?-->\s*\n", "", raw, count=1, flags=re.DOTALL)
    return raw[:max_chars]


def _read_article_block(it: ScoredItem, repo_root: Path, max_chars: int = 4000) -> str | None:
    """读取 article_path 对应的 markdown 文件，渲染为 <details> 折叠块"""
    p = _read_article_path(it, repo_root)
    if not p:
        return None
    try:
        raw = p.read_text(encoding="utf-8")
    except Exception:
        return None
    # 去掉 frontmatter 注释头
    raw = re.sub(r"^<!--.*?-->\s*\n", "", raw, count=1, flags=re.DOTALL)
    if len(raw) > max_chars:
        raw = raw[:max_chars] + "\n\n*[... 已截断，点击顶部链接看全文]*"
    rel = p.relative_to(repo_root)
    return (
        f"<details>\n"
        f"<summary>📄 展开原文摘录（[GitHub 原文]({rel})）</summary>\n\n"
        f"{raw}\n"
        f"\n</details>"
    )


# ============== 渲染 ==============

def render_board_section(board: str, items: list[ScoredItem], date_iso: str,
                        repo_root: Path, llm: LLMClient | None,
                        usage: LLMUsage) -> str:
    """渲染单个板块的 Markdown 段"""
    meta = BOARD_META.get(board, BOARD_META["其他"])
    emoji = meta["emoji"]
    name = meta["name"]
    impact_hint = meta["impact_hint"]

    lines: list[str] = []
    lines.append(f"## {emoji} {name} | Top {len(items)}")
    lines.append("")

    if not items:
        lines.append("> 昨日无新增。")
        lines.append("")
        return "\n".join(lines)

    # 头条：详细展开
    head = items[0]
    article_text = _read_article_text(head, repo_root)

    # 核心内容：LLM 摘要（有原文时）> summary 截断
    head_summary = head.summary
    if llm and article_text:
        llm_sum = llm_summarize_article(llm, article_text, usage)
        if llm_sum:
            head_summary = llm_sum

    # 影响分析：LLM（有原文时）> 规则模板
    head_impact = generate_impact(head.matched_tags, head.title, head.summary)
    if llm and article_text:
        llm_imp = llm_impact(llm, head_summary or head.summary, article_text, impact_hint, usage)
        if llm_imp:
            head_impact = llm_imp

    lines.append(f"### 1. {head.title}")
    lines.append(f"- **来源**：[{head.source}]({head.url}) ｜ **时间**：{fmt_time_cn(head.published_dt)}")
    lines.append(f"- **重要程度**：{head.grade} {'高' if head.grade == '🔴' else '中等' if head.grade == '🟡' else '一般'}")
    if head_summary:
        lines.append(f"- **核心内容**：{head_summary}")
    lines.append(f"- **影响分析**：{head_impact}（{impact_hint}）")
    if head.matched_tags:
        tag_md = " ".join(f"`#{t}`" for t in head.matched_tags)
        lines.append(f"- **标签**：{tag_md}")
    lines.append("")

    # 头条：折叠全文（如果 article_path 存在且文件可读）
    article_block = _read_article_block(head, repo_root)
    if article_block:
        lines.append(article_block)
        lines.append("")

    # 2-N：简版列表（P0 改：LLM 改写 1-2 句）
    if len(items) > 1:
        lines.append(f"### 其余 {len(items) - 1} 条（简版）")
        lines.append("")
        for idx, it in enumerate(items[1:], start=2):
            tag_md = ""
            if it.matched_tags:
                tag_md = " " + " ".join(f"`#{t}`" for t in it.matched_tags[:3])

            # 简版内容：LLM 改写 1-2 句（含量化数据）> RSS summary 截断 100 字
            short_text = ""
            if llm:
                llm_b = llm_brief(llm, it.title, it.summary, it.tags, usage)
                if llm_b:
                    short_text = llm_b
            if not short_text:
                short_text = it.summary[:120] + ("…" if len(it.summary) > 120 else "")
            article_marker = " 📄" if _read_article_path(it, repo_root) else ""
            lines.append(
                f"{idx}. **{it.grade}** [{it.title}]({it.url}) — {it.source} · "
                f"{fmt_time_cn(it.published_dt)} — {short_text}{tag_md}{article_marker}"
            )
        lines.append("")

    return "\n".join(lines)


def render_main_signals(main_signals: list[dict]) -> str:
    """渲染主线信号段"""
    if not main_signals:
        return ""
    lines: list[str] = ["## 🔥 主线信号", ""]
    for idx, sig in enumerate(main_signals, 1):
        primary = sig["primary"]
        src_md = "、".join(f"`{s}`" for s in sig["sources"])
        lines.append(
            f"{idx}. **{sig['display']}**（{sig['count']} 条 · 涉及 {src_md}）"
        )
        # 头条链接
        lines.append(
            f"   - 头条：[{primary.title}]({primary.url}) — {primary.source} · {fmt_time_cn(primary.published_dt)}"
        )
        # 关联条目
        others = [it for it in sig["items"] if it is not primary]
        for oth in others:
            lines.append(f"   - 关联：[{oth.title}]({oth.url}) — {oth.source} · {fmt_time_cn(oth.published_dt)}")
    lines.append("")
    return "\n".join(lines)


def _build_one_line_context(date_iso: str, groups: dict[str, list[ScoredItem]],
                           main_signals: list[dict]) -> str:
    """给 LLM 准备一句话总结的上下文"""
    parts: list[str] = []
    if main_signals:
        for sig in main_signals[:2]:
            primary = sig["primary"]
            parts.append(f"- 主线：{sig['display']}（{sig['count']} 条）— 头条：{primary.title}")
    for board in BOARD_ORDER:
        items = groups.get(board, [])
        if items:
            meta = BOARD_META.get(board, BOARD_META["其他"])
            top1 = items[0]
            parts.append(f"- {meta['name']}：{top1.title}（{top1.source}）")
    return "\n".join(parts) if parts else f"{date_iso} 无新增"


def _build_insights_context(groups: dict[str, list[ScoredItem]]) -> str:
    """给 LLM 准备跨话题洞察的上下文"""
    parts: list[str] = []
    for board in BOARD_ORDER:
        items = groups.get(board, [])
        if not items:
            continue
        meta = BOARD_META.get(board, BOARD_META["其他"])
        parts.append(f"【{meta['name']}】")
        for it in items[:3]:
            parts.append(f"- {it.title}（{it.source}）— {it.summary[:80]}")
    return "\n".join(parts) if parts else "（无数据）"


def _build_followup_context(groups: dict[str, list[ScoredItem]]) -> str:
    """给 LLM 准备待跟进 action 的上下文（只取头部前 5 条）"""
    parts: list[str] = []
    for board in BOARD_ORDER:
        items = groups.get(board, [])
        if not items:
            continue
        meta = BOARD_META.get(board, BOARD_META["其他"])
        for it in items[:3]:
            parts.append(f"- [{meta['name']}] {it.title}（{it.source}）— {it.summary[:100]}")
    return "\n".join(parts[:8]) if parts else "（无数据）"


def _calc_grade_distribution(groups: dict[str, list[ScoredItem]]) -> tuple[int, int, int, int]:
    """计算总条数 / 🔴/🟡/🟢 分布"""
    total = 0
    h = m = l = 0
    for v in groups.values():
        for it in v:
            total += 1
            if it.grade == "🔴":
                h += 1
            elif it.grade == "🟡":
                m += 1
            else:
                l += 1
    return total, h, m, l


def render_daily_report(
    date_iso: str,
    groups: dict[str, list[ScoredItem]],
    per_source: dict[str, int],
    fetched_at: str,
    repo_root: Path,
    llm: LLMClient | None = None,
    usage: LLMUsage | None = None,
    elapsed_seconds: float = 0.0,
) -> str:
    """渲染完整每日资讯 Markdown"""
    total, total_high, total_mid, total_low = _calc_grade_distribution(groups)

    # 提取主题词（按板块）
    board_keywords: dict[str, list[str]] = {}
    for board, items in groups.items():
        kws = extract_keywords(items, top_n=4)
        board_keywords[board] = kws

    # 主线信号（跨板块聚合）
    all_items = [it for v in groups.values() for it in v]
    main_signals = extract_main_signals(all_items, top_n=3, min_items=2)

    if usage is None:
        usage = LLMUsage()

    lines: list[str] = []
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
    lines.append("|---------|------|--------|--------|-------|")
    for board in BOARD_ORDER:
        items = groups.get(board, [])
        meta = BOARD_META.get(board, BOARD_META["其他"])
        emoji = meta["emoji"]
        name = meta["name"]
        h = sum(1 for it in items if it.grade == "🔴")
        m = sum(1 for it in items if it.grade == "🟡")
        l = sum(1 for it in items if it.grade == "🟢")
        kws = "、".join(board_keywords.get(board, [])) or "—"
        lines.append(f"| {emoji} {name} | {len(items)} | {h} | {m} | {l} | {kws} |")
    lines.append(f"| **合计** | **{total}** | **{total_high}** | **{total_mid}** | **{total_low}** | — |")
    lines.append("")

    # 主线信号段
    if main_signals:
        lines.append(render_main_signals(main_signals))
        lines.append("---")
        lines.append("")

    # 一句话总结（LLM 优先，规则降级）
    if total == 0:
        summary_line = f"{date_iso} 全网 AI 资讯较少，已对接的源均无新增。"
    else:
        summary_line = None
        if llm:
            ctx = _build_one_line_context(date_iso, groups, main_signals)
            llm_sum = llm_one_line_summary(llm, ctx, usage)
            if llm_sum:
                summary_line = llm_sum.rstrip("。") + "。"
        if summary_line is None:
            summary_line = build_one_line_summary(date_iso, groups, per_source, main_signals)
    lines.append(f"> **昨日一句话总结**：{summary_line}")
    lines.append("")
    lines.append("---")
    lines.append("")

    # 各板块
    for board in BOARD_ORDER:
        items = groups.get(board, [])
        lines.append(render_board_section(board, items, date_iso, repo_root, llm, usage))

    # 跨话题洞察（LLM 优先，规则降级）
    lines.append("## 💡 跨话题洞察")
    lines.append("")
    insights: list[str] | None = None
    if llm and total > 0:
        ctx = _build_insights_context(groups)
        insights = llm_cross_insights(llm, ctx, usage)
    if not insights:
        insights = build_cross_insights(groups)
    if insights:
        for i, ins in enumerate(insights, 1):
            lines.append(f"{i}. {ins}")
    else:
        lines.append("1. 昨日数据较少，跨话题趋势暂不显著；建议扩大抓取源覆盖后重新评估。")
    lines.append("")
    lines.append("---")
    lines.append("")

    # 待跟进（P6 改：LLM action 化 > 规则降级）
    lines.append("## 📌 待跟进")
    lines.append("")
    todos: list[str] | None = None
    if llm and total > 0:
        ctx = _build_followup_context(groups)
        todos = llm_followup_actions(llm, ctx, usage)
    if not todos:
        todos = build_todos(groups)
    if todos:
        for t in todos:
            lines.append(f"- [ ] {t}")
    else:
        lines.append("- [ ] 暂无明确待跟进项。")
    lines.append("")
    lines.append("---")
    lines.append("")

    # 信息源记录
    lines.append("## 📎 信息源记录")
    lines.append("")
    lines.append("| 板块 | 抓取来源 | 命中条数 | 状态 |")
    lines.append("|------|---------|---------|------|")
    for board in BOARD_ORDER:
        meta = BOARD_META.get(board, BOARD_META["其他"])
        name = meta["name"]
        items = groups.get(board, [])
        src_in_board = sorted({it.source for it in items})
        all_srcs = [s for s in per_source.keys() if classify_board(s) == board]
        if not src_in_board and not all_srcs:
            continue
        for s in (src_in_board or all_srcs):
            count = per_source.get(s, 0)
            mark = "✅" if count > 0 else "⚪"
            lines.append(f"| {name} | {s} | {count} | {mark} |")
    lines.append("")

    # 📈 元信息（P11 新增）
    lines.append("## 📈 元信息")
    lines.append("")
    n_sources_configured = len(per_source)
    n_sources_hit = sum(1 for v in per_source.values() if v > 0)
    hit_rate = (n_sources_hit / n_sources_configured * 100) if n_sources_configured else 0
    lines.append(f"- **数据源**：{n_sources_configured} 个配置 / {n_sources_hit} 个命中 / 命中率 {hit_rate:.0f}%")
    lines.append(f"- **总条数**：{total}（🔴 {total_high} / 🟡 {total_mid} / 🟢 {total_low}）")
    lines.append(f"- **LLM 精炼**：{usage.to_footer()}")
    if elapsed_seconds > 0:
        lines.append(f"- **生成耗时**：{elapsed_seconds:.1f}s")
    lines.append("")

    # 底部 log
    lines.append(
        f"*采集时间：{fmt_clock(datetime.fromisoformat(fetched_at).astimezone(ZoneInfo('Asia/Shanghai')))} · "
        f"信息源数量：{len(per_source)} · "
        f"🤖 LLM 精炼：{usage.to_footer()} · "
        f"报告生成：AI调研专家*"
    )
    lines.append("")

    return "\n".join(lines)


def build_one_line_summary(date_iso: str, groups: dict[str, list[ScoredItem]],
                          per_source: dict[str, int],
                          main_signals: list[dict] | None = None) -> str:
    """规则版一句话总结（LLM 不可用时的降级方案）"""
    total = sum(len(v) for v in groups.values())
    if total == 0:
        return f"{date_iso} 全网 AI 资讯较少，已对接的源均无新增。"

    parts: list[str] = []
    if main_signals:
        first_sig = main_signals[0]
        parts.append(f"**主线**：{first_sig['display']}（{first_sig['count']} 条）")
    high_items = [it for v in groups.values() for it in v if it.grade == "🔴"]
    if high_items and not main_signals:
        first = high_items[0]
        parts.append(f"重点关注 [{first.source}]({first.url}) 发布的「{first.title}」")
    boards_with_items = [b for b in BOARD_ORDER if groups.get(b)]
    if boards_with_items:
        board_names = "、".join(BOARD_META[b]["name"] for b in boards_with_items)
        parts.append(f"覆盖 {board_names}")
    parts.append(f"共 {total} 条命中")
    return "；".join(parts) + "。"


def build_cross_insights(groups: dict[str, list[ScoredItem]]) -> list[str]:
    """规则版跨话题洞察（LLM 不可用时的降级方案）"""
    insights: list[str] = []

    llm_items = groups.get("LLM", [])
    agent_items = groups.get("Agent", [])

    if llm_items and agent_items:
        insights.append("**LLM × 编程 Agent**：昨日 LLM 官方源（OpenAI）与 Agent 框架源（LangChain）均有动作，"
                        "提示基础模型能力正在向应用层（Agent 工具调用、沙箱隔离）继续渗透。")

    avatar_items = groups.get("数字人", [])
    avatar_video = any(
        any(re.search(r"video|视频|avatar|数字人|runway", it.title + it.summary, re.I) for it in avatar_items)
        for _ in [1]
    )
    if avatar_video and llm_items:
        insights.append("**LLM × 数字人**：数字人 / 视频生成类内容持续更新，与多模态基础模型演进形成正反馈。")

    has_top = any(it.source in {"openai", "deepmind", "anthropic"} for it in llm_items)
    if has_top:
        insights.append("**行业宏观**：头部模型厂商持续输出（OpenAI Academy / 案例研究等），"
                        "AI 行业重心从「能力突破」逐步转向「企业落地与培训」。")

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
    """规则版待跟进事项（LLM 不可用时的降级方案）"""
    todos: list[str] = []
    high_items = [it for it in (it for v in groups.values() for it in v) if it.grade == "🔴"]
    for it in high_items[:3]:
        todos.append(f"读完 [{it.source}]({it.url})「{it.title}」全文，整理 3 条要点分享给团队")
    if not high_items:
        todos.append("评估是否需补充抓取源（如 36Kr / 量子位 / 机器之心）以提升日报覆盖")
        todos.append("抽样近 3 天日报，对比 LLM 精炼版 vs 模板版，评估是否需要调整 prompt")
    return todos


# ============== 入口 ==============

def main():
    parser = argparse.ArgumentParser(description="生成每日资讯 Markdown 报告")
    parser.add_argument("--date", help="目标日期 YYYY-MM-DD（默认 = 昨天 Asia/Shanghai）")
    parser.add_argument("--raw-root", default="data/raw", help="抓取数据根目录")
    parser.add_argument("--out-dir", default="每日资讯", help="输出目录")
    parser.add_argument("--dry-run", action="store_true", help="只打印到 stdout，不落盘")
    parser.add_argument("--llm", default=None,
                        help="临时指定 LLM provider（覆盖 .secrets / 环境变量）")
    parser.add_argument("--no-llm", action="store_true",
                        help="强制关闭 LLM 精炼（走规则降级）")
    args = parser.parse_args()

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

    # 加载 LLM 客户端（按 --no-llm / --llm 开关处理）
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

    t0 = time.time()
    raw_items, per_source = load_raw_items(date_iso, raw_root)
    if not raw_items:
        print(f"[WARN] {date_iso} 窗口内无任何抓取数据（请确认已运行抓取脚本）", file=sys.stderr)
    scored = build_scored_items(raw_items, date_iso)
    groups = group_by_board(scored)

    fetched_at = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
    md = render_daily_report(date_iso, groups, per_source, fetched_at, _REPO_ROOT,
                             llm=llm, usage=usage, elapsed_seconds=time.time() - t0)

    if args.dry_run:
        print(md)
        return 0

    out_path = out_dir / f"{date_iso}.md"
    out_path.write_text(md, encoding="utf-8")
    total = sum(len(v) for v in groups.values())
    print(f"[OK] 写入 {out_path}  | 板块 {sum(1 for v in groups.values() if v)} 个 / 总条数 {total} | {usage.to_footer()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
