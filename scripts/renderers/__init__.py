"""
daily v2.0 渲染器集合
====================

每个渲染器 = 纯函数 render(daily_json) -> str
- 不调 LLM（LLM 精炼已在 daily JSON 里）
- 不读 md（md 是渲染产物之一）
- 按渠道需求裁剪（wecom_markdown ≤500 字符；feishu_docx 完整；markdown 完整）

约定：
- daily_json: dict，符合 schema_version="2.0" 规范
  （含 schema_version / report_date / meta / boards / main_signals / one_line_summary / llm_usage / render_meta / publish_record 等字段）
"""

from .markdown import render as render_markdown
from .feishu_docx import render as render_feishu_docx
from .wecom_markdown import render as render_wecom_markdown

__all__ = [
    "render_markdown",
    "render_feishu_docx",
    "render_wecom_markdown",
]
