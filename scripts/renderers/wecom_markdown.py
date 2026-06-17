"""
renderers/wecom_markdown.py — daily JSON → 企微 markdown 消息（方案 B：简短摘要 + 飞书链接）

用途：
- v2.0 daily 报告的企微 markdown 渲染器
- **主人原话"企微仍旧先引用飞书的公开文档链接"** = 企微消息 = 简短摘要 + 飞书公开链接
- 字符数 ≤500（避免裁剪 / 避免日报残缺）
- 飞书 docx 是完整日报载体（无字符限制）

设计：
- 纯函数：render(daily_json) -> str
- 不调 LLM（LLM 精炼在 daily JSON 里）
- 不读 md（md 是渲染产物之一）
- 输出格式：企微 markdown（标题 + 一句话总结 + 飞书链接）

字符数预估（典型）：
- 标题 ~30 字符
- 一句话总结 ~50-80 字符
- 飞书链接 ~80 字符
- 格式字符 ~15 字符
- 总计 ~175-205 字符（远低于 500 上限）
"""

from __future__ import annotations


MAX_CHARS = 500  # 企微 markdown 上限 4096；方案 B 限 500 留足冗余


def render(daily_json: dict) -> str:
    """daily v2.0 JSON → 企微 markdown 消息（方案 B 简短摘要 + 飞书链接）

    参数：
      daily_json: v2.0 daily JSON；必须有 publish_record.doc_url

    返回：
      企微 markdown 字符串（≤500 字符）
    """
    if not daily_json:
        return ""

    date_iso = daily_json.get("report_date", "")
    one_line = daily_json.get("one_line_summary", {}).get("text", "")
    doc_url = daily_json.get("publish_record", {}).get("doc_url") or ""

    # 如果 doc_url 缺失（生成后未推送），用占位符
    if not doc_url:
        doc_url = f"（飞书 docx 链接待推送生成）"

    # 拼接简短摘要 + 飞书链接
    parts: list[str] = []
    parts.append(f"📰 **AI资讯日报** · {date_iso}")
    if one_line:
        parts.append(f"\n{one_line}")
    parts.append(f"\n👉 [完整日报]({doc_url})")
    out = "\n".join(parts)

    # 字符数截断（防御性）
    if len(out) > MAX_CHARS:
        # 截断一句话总结 + 保留飞书链接
        safe_summary = one_line[: MAX_CHARS - len(f"📰 **AI资讯日报** · {date_iso}\n\n👉 [完整日报]({doc_url})") - 10] + "…"
        parts = [
            f"📰 **AI资讯日报** · {date_iso}",
            f"\n{safe_summary}",
            f"\n👉 [完整日报]({doc_url})",
        ]
        out = "\n".join(parts)

    return out[:MAX_CHARS]
