"""
每日资讯生成器 — 共用工具
=================

职责：
1. 源 → 话题板块映射
2. 评分规则（重要度 + 时新性 + 影响力）
3. 评级规则（🔴/🟡/🟢）
4. 标签 / 标题 / 摘要 清洗（HTML entity、空白等）
5. 影响分析生成器（基于关键词的轻量模板化判断）

设计原则：
- 无第三方依赖（只用标准库 + 已装的 html）
- 可独立 import，也可被 generate_daily.py 调用
- 评分/评级规则集中在此，方便后续微调
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable
from zoneinfo import ZoneInfo


# ============== 源 → 话题板块映射 ==============
# 与「资讯源管理/源清单.md」保持一致
SOURCE_TO_BOARD: dict[str, str] = {
    # LLM 发展
    "openai":      "LLM",
    "deepmind":    "LLM",
    "huggingface": "LLM",
    "anthropic":   "LLM",
    # 编程 Agent
    "langchain":        "Agent",
    "github_trending":  "Agent",
    "cursor":           "Agent",
    "devin":            "Agent",
    # 数字人
    "replicate": "数字人",
    "runway":    "数字人",
    "heygen":    "数字人",
    "synthesia": "数字人",
    # 其他（兜底）
}

# 源权重（用于评分），分数越高越靠前
SOURCE_WEIGHT: dict[str, int] = {
    "openai":      30,   # 头部官方
    "deepmind":    30,
    "anthropic":   28,
    "langchain":   18,   # Agent 框架头部
    "github_trending": 15,
    "huggingface": 15,
    "replicate":   10,
    "runway":      10,
    "heygen":      8,
    "synthesia":   8,
    "cursor":      12,
    "devin":       12,
}

# 板块中文标签 + emoji
BOARD_META: dict[str, dict] = {
    "LLM":   {"name": "LLM 发展",   "emoji": "🧠", "impact_hint": "对基础模型 / 行业格局 / 监管的影响"},
    "Agent": {"name": "编程 Agent", "emoji": "💻", "impact_hint": "对开发者工具链 / IDE / 企业研发流程的影响"},
    "数字人": {"name": "数字人",     "emoji": "🧍", "impact_hint": "对数字人产品 / 内容创作 / 商业化的影响"},
    "其他":  {"name": "其他",       "emoji": "📦", "impact_hint": "对行业其他领域的影响"},
}

# 板块输出顺序（生成器消费此顺序渲染）
BOARD_ORDER: list[str] = ["LLM", "Agent", "数字人", "其他"]

# ============== 评分 ==============

# 关键词 → 加分（用于"影响力"维度）
# 大小写不敏感；标签、标题、summary 中出现即加分
# 注意：用 \b 词边界只包围纯单词，避免 \bintroduc\b 不匹配 "introduces" 这类
KEYWORD_BOOST: list[tuple[str, int, str]] = [
    # 高权重
    (r"\bGPT[- ]?[345]\b",      25, "GPT"),
    (r"\bGPT[- ]?4o\b",         18, "GPT-4o"),
    (r"\bClaude\b",             15, "Claude"),
    (r"\bGemini\b",             15, "Gemini"),
    (r"\bLlama\b",              12, "Llama"),
    (r"\bSora\b",               20, "Sora"),
    (r"\bveo\b",                12, "Veo"),
    (r"\bimagen\b",             12, "Imagen"),
    (r"\brelease\b|\blaunch\b|\bintroduc\w*|发布|上线|推出|\bavailable\b", 15, "release"),
    (r"\bbenchmark\b|SWE[- ]?bench|\bMMLU\b|\bHELM\b|\bHumanEval\b",      12, "benchmark"),
    (r"open[- ]?source|开源",                                          12, "开源"),
    (r"\bagents?\b|AI\s+agent|代理",                                  18, "agent"),
    (r"\bsandbox\w*|沙箱",                                              8,  "sandbox"),
    (r"\bRAG\b|retrieval[- ]?augmented",                              10, "RAG"),
    (r"fine[- ]?tun|微调",                                             8,  "微调"),
    (r"\breasoning\b|推理|chain[- ]?of[- ]?thought|step[- ]?by[- ]?step", 12, "推理"),
    (r"\bmultimodal\b|多模态",                                        12, "多模态"),
    (r"video\s*gen|视频生成|text[- ]?to[- ]?video",                  18, "视频生成"),
    (r"\bavatar\b|digital\s*human|数字人|虚拟主播",                   18, "数字人"),
    (r"training\s*data|训练数据|数据泄露|databreach",                 10, "训练数据"),
    (r"\bsafety\b|\bsecurity\b|安全|alignment|对齐",                  10, "安全"),
    (r"\bregulation\b|监管|政策|法律",                                12, "监管"),
    (r"\bacquisition\b|并购|融资|funding|\braised\b",                 15, "资本"),
    (r"\bpodcast\b|播客|\bepisode\b",                                  3,  "播客"),
    (r"\bcourse\w*|\bacademy\b|教程|培训|学习",                       5,  "培训"),
    (r"personali[sz]ation|个性化|定制",                                6,  "个性化"),
]


@dataclass
class ScoredItem:
    """评分后的 item 视图（不再带 source 冗余字段）"""
    source: str
    board: str
    title: str
    url: str
    published: str           # ISO 字符串
    published_dt: datetime   # 解析后的 datetime（含时区）
    summary: str
    author: str | None
    tags: list[str]
    score: int
    grade: str               # 🔴/🟡/🟢
    matched_tags: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)   # 透传 item.extra（如 article_path）


# ============== 清洗 ==============

_WHITESPACE = re.compile(r"\s+")


def clean_text(s: str | None, max_len: int | None = None) -> str:
    """清洗：HTML entity → 字符；折叠空白；可选截断"""
    if not s:
        return ""
    s = html.unescape(s)
    s = _WHITESPACE.sub(" ", s).strip()
    if max_len and len(s) > max_len:
        s = s[: max_len - 1] + "…"
    return s


def parse_published(published: str, fallback_date: str) -> datetime:
    """解析 published 字符串；解析失败回退到 fallback_date 当天 12:00"""
    if not published:
        return datetime.fromisoformat(fallback_date + "T12:00:00+08:00")
    try:
        # 处理 "Z" 后缀
        s = published.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        return dt
    except (ValueError, TypeError):
        return datetime.fromisoformat(fallback_date + "T12:00:00+08:00")


# ============== 评分 / 评级 ==============

def score_item(item: dict, date_iso: str) -> tuple[int, str, list[str]]:
    """
    对单条 item 评分。

    评分维度：
    - 源权重（0-30）
    - 关键词命中（按规则累加）
    - summary 长度（小奖励：内容更厚）

    Returns: (score, grade, matched_tag_list)
    """
    src = item.get("source", "")
    title = clean_text(item.get("title", ""))
    summary = clean_text(item.get("summary", ""))
    tags = item.get("tags") or []
    text_blob = " ".join([title, summary, " ".join(tags)]).lower()

    score = SOURCE_WEIGHT.get(src, 5)
    matched: list[str] = []

    for pattern, bonus, label in KEYWORD_BOOST:
        if re.search(pattern, text_blob, re.IGNORECASE):
            score += bonus
            if label not in matched:
                matched.append(label)

    # 内容厚度奖励
    if len(summary) > 200:
        score += 5
    if len(summary) > 400:
        score += 5

    # 标签数量奖励
    if len(tags) >= 2:
        score += 3

    # 评级
    if score >= 60:
        grade = "🔴"
    elif score >= 30:
        grade = "🟡"
    else:
        grade = "🟢"

    return score, grade, matched


# ============== 影响分析生成器 ==============

IMPACT_TEMPLATES: list[tuple[str, str, str]] = [
    # (pattern, matched_label, template_text) — text 不带末尾"。"，由 generate_impact 统一加
    (r"\bGPT\b|Claude|Gemini|Llama|Sora|veo|imagen",
     "基础模型",
     "对头部基础模型竞争格局的影响"),
    (r"\brelease\b|\blaunch\b|\bintroduc\w*|发布|上线|推出|\bavailable\b",
     "产品发布",
     "产品级动作，可能影响后续生态（API 定价、可用性、第三方集成）"),
    (r"open[- ]?source|开源",
     "开源",
     "开源会进一步降低中小团队接入门槛，加速二次创新与生态扩散"),
    (r"\bagents?\b|AI\s+agent|代理",
     "agent",
     "推动 AI Agent 工程化路径（sandbox、tool-use、multi-step planning）走向成熟"),
    (r"\bbenchmark\b|SWE[- ]?bench|\bMMLU\b|\bHumanEval\b",
     "benchmark",
     "为行业提供新的能力基线，间接影响下游选型与采购决策"),
    (r"\bsandbox\w*|沙箱",
     "sandbox",
     "推动 Agent 沙箱生态与安全标准化（microVM、权限边界、审计）"),
    (r"\bRAG\b|retrieval",
     "RAG",
     "对企业知识库 / 私有数据场景的落地路径有直接价值"),
    (r"video\s*gen|视频生成|text[- ]?to[- ]?video",
     "视频生成",
     "对视频创作 / 广告 / 短剧等内容生产链的影响"),
    (r"\bavatar\b|digital\s*human|数字人|虚拟主播",
     "数字人",
     "对数字人产品 / 内容创作 / 商业化节奏的推动"),
    (r"\bsafety\b|\bsecurity\b|安全|alignment|对齐|\bregulation\b|监管",
     "安全/监管",
     "提示行业关注安全与合规，监管侧的信号需持续追踪"),
    (r"\bacquisition\b|融资|funding|\braised\b|并购",
     "资本",
     "资本侧动作，提示行业资源整合或新方向布局"),
    (r"\bcourse\w*|\bacademy\b|教程|培训",
     "培训",
     "推动企业 AI 培训与落地路径，影响组织内部采纳速度"),
    (r"personali[sz]ation|个性化",
     "个性化",
     "推动 AI 应用的个性化能力，影响 ToC 产品差异化空间"),
    (r"\breasoning\b|推理",
     "推理",
     "对长链路推理 / 复杂任务自动化能力有正向推动"),
    (r"\bmultimodal\b|多模态",
     "多模态",
     "推动多模态原生模型发展，影响 UI / UX 与下游应用形态"),
    (r"training\s*data|训练数据|数据泄露|databreach",
     "训练数据",
     "提示数据合规 / 版权风险，是合规层面需关注的信号"),
]


def generate_impact(tags: list[str], title: str, summary: str) -> str:
    """基于标签 / 标题 / 摘要关键词生成简短影响分析（1-2 条）"""
    blob = " ".join([title or "", summary or "", " ".join(tags or [])])
    lines: list[str] = []
    seen_labels: set[str] = set()
    for pattern, label, text in IMPACT_TEMPLATES:
        if label in seen_labels:
            continue
        if re.search(pattern, blob, re.IGNORECASE):
            lines.append(text)
            seen_labels.add(label)
        if len(lines) >= 2:  # 最多 2 条
            break
    if not lines:
        return "需结合行业上下文进一步判断。"
    return "；".join(lines) + "。"


# ============== 主题词提取 ==============

def extract_keywords(items: list[ScoredItem], top_n: int = 4) -> list[str]:
    """从板块内所有 item 的 matched_tags 统计出现最多的前 N 个关键词"""
    counter: dict[str, int] = {}
    for it in items:
        for tag in it.matched_tags:
            counter[tag] = counter.get(tag, 0) + 1
    sorted_kw = sorted(counter.items(), key=lambda x: (-x[1], x[0]))
    return [k for k, _ in sorted_kw[:top_n]]


# ============== 主线信号聚合 ==============

# 跨板块权重更高的"主线级"标签
SIGNAL_LEVEL_TAGS = {
    "capital-acq", "capital-fund", "capital-valuation",  # 资本
    "release-GA", "release",                            # 重大产品
    "开源", "GPT", "Claude", "Gemini", "Llama", "Sora", # 模型
    "enterprise",                                       # 企业落地
    "监管", "安全",                                     # 监管/安全
    "agent",                                            # Agent 工程化
}

# 主题信号显示名（用于报告）
SIGNAL_DISPLAY = {
    "capital-acq":      "💰 行业并购",
    "capital-fund":     "💰 行业融资",
    "capital-valuation":"💰 估值/上市",
    "release-GA":       "🚀 重大发布（GA）",
    "release":          "🚀 产品发布",
    "开源":             "🧩 开源动作",
    "GPT":              "🧠 GPT 相关",
    "Claude":           "🧠 Claude 相关",
    "Gemini":           "🧠 Gemini 相关",
    "Llama":            "🧠 Llama 相关",
    "Sora":             "🎬 Sora / 视频",
    "enterprise":       "🏢 企业落地",
    "监管":             "⚖️ 监管/合规",
    "安全":             "🛡️ 安全/对齐",
    "agent":            "🤖 Agent 工程化",
}


def extract_main_signals(items: list[ScoredItem], top_n: int = 3,
                         min_items: int = 2) -> list[dict]:
    """从全量 items 中聚合出"主线信号"。

    聚合规则：
    1. 每条 item 取其最高权重主线标签
    2. 同一主线标签的 item 聚为一组
    3. 输出按"组内 items 数量 × 评分总和"降序，取 top N
    4. 单组少于 min_items 跳过（避免偶然信号当成主线）

    返回: [{tag, display, count, sources, items, primary, all_titles}, ...]
    """
    # 每条 item 的"主线标签"
    primary_tag: dict[int, str | None] = {}
    for it in items:
        cands = [t for t in it.matched_tags if t in SIGNAL_LEVEL_TAGS]
        if cands:
            # 选最相关的（这里直接取第一个；后续可按权重排序）
            primary_tag[id(it)] = cands[0]
        else:
            primary_tag[id(it)] = None

    # 分组
    groups: dict[str, list[ScoredItem]] = {}
    for it in items:
        tag = primary_tag.get(id(it))
        if not tag:
            continue
        groups.setdefault(tag, []).append(it)

    # 排序 + 过滤
    ranked: list[dict] = []
    for tag, group in groups.items():
        if len(group) < min_items:
            continue
        # 组内按 score 排序
        group_sorted = sorted(group, key=lambda x: (-x.score, -x.published_dt.timestamp()))
        ranked.append({
            "tag": tag,
            "display": SIGNAL_DISPLAY.get(tag, f"🏷 {tag}"),
            "count": len(group),
            "sources": sorted({it.source for it in group}),
            "items": group_sorted,
            "primary": group_sorted[0],
            "all_titles": [it.title for it in group_sorted],
        })

    # 排序：组内数量 desc, 组内总分 desc
    ranked.sort(key=lambda g: (-g["count"], -sum(it.score for it in g["items"])))
    return ranked[:top_n]


# ============== 板块归类 ==============

def classify_board(source: str) -> str:
    return SOURCE_TO_BOARD.get(source, "其他")


# ============== 时间格式化 ==============

def fmt_time_cn(dt: datetime) -> str:
    """格式化为 MM.DD HH:MM（Asia/Shanghai）"""
    cst = dt.astimezone(ZoneInfo("Asia/Shanghai"))
    return f"{cst.month:02d}.{cst.day:02d} {cst.hour:02d}:{cst.minute:02d}"


def fmt_clock(dt: datetime) -> str:
    """HH:MM 格式"""
    return f"{dt.hour:02d}:{dt.minute:02d}"
