"""
抓取 36Kr 综合资讯 (RSS)
板块: 创投资讯（中文综合，含 AI / 科技 / 创投）
源详情: 资讯源管理/已对接/创投_36Kr.md

抓取策略:
- RSS: https://36kr.com/feed （综合资讯, 30 条）
- 内容含 HTML, RSS 解析后 summary 已有完整正文
- 因 36Kr 内容含大量非 AI 文章（曼联/杨梅/茶饮等），按关键词做粗过滤
  保留: AI / 大模型 / 智能体 / 机器人 / 芯片 / 创投融资 等
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _utils import (
    add_date_args, get_logger, parse_rss, filter_window,
    resolve_dates, save_items, enrich_items,
)

SOURCE = "36kr"
FEED_URL = "https://36kr.com/feed"
LOG = get_logger(f"crawl_{SOURCE}")

# AI / 科技 / 创投 关键词（用于粗过滤；任何一条命中即保留）
# 关键词分组：AI 技术 / 模型 / 芯片 / 机器人 / 创投 / 厂商 / 资本
_KW_PATTERNS = [
    # 核心 AI
    r"AI", r"人工智能", r"大模型", r"LLM", r"GPT", r"Claude", r"豆包",
    r"通义", r"文心", r"盘古", r"混元", r"DeepSeek", r"Kimi",
    r"智能体", r"Agent", r"RAG", r"嵌入", r"推理", r"训练", r"微调",
    r"对齐", r"RLHF", r"Transformer", r"扩散", r"文生图", r"文生视频",
    r"多模态", r"Sora", r"Midjourney", r"Stable Diffusion",
    r"OpenAI", r"Anthropic", r"DeepMind", r"xAI",
    # 芯片 / 算力 / 硬件
    r"芯片", r"半导体", r"GPU", r"算力", r"英伟达", r"AMD",
    r"高通", r"英特尔", r"推理芯片", r"ASIC", r"端侧",
    # 机器人 / 自动驾驶
    r"具身", r"自动驾驶", r"Robotaxi", r"机器人", r"人形",
    # 厂商
    r"商汤", r"旷视", r"智谱", r"月之暗面", r"面壁", r"百川",
    r"百度", r"阿里", r"字节", r"腾讯", r"华为", r"小米", r"讯飞", r"寒武纪",
    # 创投 / 资本
    r"融资", r"创投", r"风投", r"独角兽", r"估值",
]
_KW_RE = re.compile("|".join(_KW_PATTERNS), re.IGNORECASE)


def is_ai_related(title: str, summary: str) -> bool:
    text = f"{title} {summary}"
    return bool(_KW_RE.search(text))


def main() -> int:
    parser = argparse.ArgumentParser(description="抓取 36Kr 综合资讯（中文创投/AI）")
    add_date_args(parser)
    parser.add_argument(
        "--no-filter", action="store_true",
        help="不做 AI 关键词过滤（保留全部条目）",
    )
    args = parser.parse_args()

    try:
        items = parse_rss(FEED_URL, source=SOURCE)
    except Exception as e:
        LOG.error(f"抓取失败: {e}")
        return 1

    LOG.info(f"RSS 解析: {len(items)} 条")

    for date_str, start, end in resolve_dates(args):
        in_win = filter_window(items, start, end)
        if not in_win:
            LOG.info(f"[{date_str}] 窗口内 0 条")
            save_items(SOURCE, date_str, in_win, meta={
                "feed": FEED_URL,
                "window": [start.isoformat(), end.isoformat()],
                "note": "no items in window",
            })
            continue

        # 关键词过滤
        if not args.no_filter:
            pre = len(in_win)
            in_win = [it for it in in_win if is_ai_related(it.title, it.summary)]
            LOG.info(f"[{date_str}] 关键词过滤: {pre} -> {len(in_win)} 条")

        if in_win and not getattr(args, "no_article", False):
            in_win = enrich_items(in_win, SOURCE, log=LOG, delay=0.5)
        path = save_items(SOURCE, date_str, in_win, meta={
            "feed": FEED_URL,
            "window": [start.isoformat(), end.isoformat()],
            "total_in_window": len(in_win),
            "ai_filter_applied": not args.no_filter,
        })
        LOG.info(f"[{date_str}] 窗口内 {len(in_win)} 条 -> {path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
