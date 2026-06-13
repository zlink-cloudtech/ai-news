"""
一键跑全部首期 7 个抓取源
按 RSSHub 是否部署决定是否跑 github_trending

用法:
    python3 scripts/crawlers/run_all.py                # 跑除 github_trending 外的全部
    python3 scripts/crawlers/run_all.py --with-github  # 加上 github_trending（需 RSSHub）
    python3 scripts/crawlers/run_all.py --date 2026-06-12
    python3 scripts/crawlers/run_all.py --days 3
"""
from __future__ import annotations

import argparse
import importlib
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _utils import get_logger

LOG = get_logger("crawl_all")

# 首期 8 源（5 英文 + 3 中文），按"默认/网络受限/可选"分类
DEFAULT_SOURCES = [
    "crawl_openai",
    "crawl_deepmind",
    "crawl_langchain",
    "crawl_replicate",
    "crawl_runway",
    "crawl_36kr",
    "crawl_qbitai",
    "crawl_jiqizhixin",
]

# 网络受限：本机 DNS 污染 / 代理拦截；部署到正确网络时启用
NETWORK_RESTRICTED_SOURCES = [
    "crawl_huggingface",  # huggingface.co 在本机网络不可达
]

# 需要额外服务：RSSHub 未部署时跳过
OPTIONAL_SOURCES = [
    "crawl_github_trending",  # 需要 RSSHub
]


def run_module(mod_name: str, extra_args: list[str]) -> tuple[str, int]:
    """运行一个抓取模块；返回 (name, exit_code)"""
    try:
        mod = importlib.import_module(mod_name)
    except ImportError as e:
        LOG.error(f"[{mod_name}] 模块加载失败: {e}")
        return mod_name, 1
    try:
        # 复用模块的 argparse：把 --date/--days/--all 透传
        old_argv = sys.argv
        sys.argv = [mod_name] + extra_args
        rc = mod.main()
        sys.argv = old_argv
        return mod_name, rc
    except SystemExit as e:
        sys.argv = old_argv
        return mod_name, int(e.code) if e.code is not None else 0
    except Exception:
        sys.argv = old_argv
        LOG.error(f"[{mod_name}] 异常:\n{traceback.format_exc()}")
        return mod_name, 1


def main() -> int:
    parser = argparse.ArgumentParser(description="一键跑全部抓取源")
    parser.add_argument("--with-github", action="store_true",
                        help="加上 GitHub Trending（需先部署 RSSHub）")
    parser.add_argument("--with-hf", action="store_true",
                        help="加上 HuggingFace（本机网络不可达，需正确网络环境）")
    parser.add_argument("--date", help="目标日期 YYYY-MM-DD")
    parser.add_argument("--days", type=int, default=0, help="回溯天数")
    parser.add_argument("--all", action="store_true", help="调试：忽略窗口")
    parser.add_argument("--source", action="append", default=[],
                        help="只跑指定源（可多次传），如 --source openai --source langchain")
    args = parser.parse_args()

    extra_args: list[str] = []
    if args.date:
        extra_args += ["--date", args.date]
    if args.days:
        extra_args += ["--days", str(args.days)]
    if args.all:
        extra_args += ["--all"]

    # 选择要跑的源
    if args.source:
        targets = [f"crawl_{s.lstrip('crawl_')}" for s in args.source]
    else:
        targets = list(DEFAULT_SOURCES)
        if args.with_hf:
            targets += NETWORK_RESTRICTED_SOURCES
        if args.with_github:
            targets += OPTIONAL_SOURCES

    LOG.info(f"将跑 {len(targets)} 个源: {targets}")
    results: list[tuple[str, int]] = []
    for name in targets:
        LOG.info(f"=== {name} ===")
        rc = run_module(name, extra_args)
        results.append(rc)
        LOG.info(f"--- {name} exit={rc[1]} ---")

    LOG.info("=== 汇总 ===")
    for name, rc in results:
        status = "OK" if rc == 0 else f"FAIL({rc})"
        LOG.info(f"  {name}: {status}")

    failed = [n for n, rc in results if rc != 0]
    if failed:
        LOG.warning(f"失败 {len(failed)} 个: {failed}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
