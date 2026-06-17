"""
AI 资讯追踪·统一 CLI 入口（v2.0 uv 迁移版）
========================================

替代 4 个 bash 脚本（news-commit.sh / run_generate_daily.sh / pm_inspect.sh / push_report.sh）
- pyproject.toml [project.scripts] 暴露 4 个命令
- 沙箱 / 桌面端 / calendar 统一通过 `uv run <cmd>` 调用

设计原则：
- 单一入口 → 易记易调
- 子命令 + 参数 → 复用 argparse 一致性
- 错误统一退出码 → 0 成功 / 1 失败 / 2 参数错
- 所有命令以仓库根为 cwd 启动（避免路径错位）

子命令：
  push-report        推送 daily/weekly/special/management/test（替代 push_report.sh）
  pm-inspect         12:00 / 20:00 PM 巡检（替代 pm_inspect.sh）
  run-generate-daily 1:00 抓取 + 生成 + commit + push（替代 run_generate_daily.sh）
  news-commit        一键 add + commit + push（替代 news-commit.sh）
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# 仓库根 = scripts/ 的父目录
REPO_ROOT = Path(__file__).resolve().parent.parent
TZ_SH = ZoneInfo("Asia/Shanghai")


def _check_repo_root() -> None:
    """确保在仓库根目录（避免 calendar 误调时 cwd 错位）"""
    if not (REPO_ROOT / ".git").exists():
        print(f"❌ 仓库根目录异常: {REPO_ROOT}", file=sys.stderr)
        sys.exit(1)
    os.chdir(REPO_ROOT)


# ==================== 子命令：push-report ====================

def push_report() -> None:
    """推送 daily/weekly/special/management/test（替代 push_report.sh）"""
    _check_repo_root()
    # 委托给 push_report.py（v2.1 重构阶段会拆出 push_report.py）
    from push_report import main as _pr_main
    sys.exit(_pr_main(sys.argv[1:]))


# ==================== 子命令：pm-inspect ====================

def pm_inspect() -> None:
    """12:00 / 20:00 PM 巡检（替代 pm_inspect.sh）"""
    _check_repo_root()
    parser = argparse.ArgumentParser(
        prog="pm-inspect",
        description="AI 资讯追踪·PM 巡检（v1.3 8 维度）",
    )
    parser.add_argument("type", choices=["12:00", "20:00"], help="巡检类型")
    parser.add_argument("--no-escalate", action="store_true", help="跳过 owner 私聊升级")
    parser.add_argument("--dry-run", action="store_true", help="只生成报告不推送")
    args = parser.parse_args(sys.argv[1:])

    from generators.generate_inspection import main as _gi_main
    sys.exit(_gi_main([args.type] + (["--no-escalate"] if args.no_escalate else [])))


# ==================== 子命令：run-generate-daily ====================

def run_generate_daily() -> None:
    """1:00 抓取 + 生成 + commit + push（替代 run_generate_daily.sh）"""
    _check_repo_root()
    parser = argparse.ArgumentParser(
        prog="run-generate-daily",
        description="AI 资讯追踪·每日 1:00 跑昨日日报",
    )
    parser.add_argument("date", nargs="?", help="目标日期 YYYY-MM-DD（默认 = 昨天 Asia/Shanghai）")
    parser.add_argument("--no-llm", action="store_true", help="强制关闭 LLM 精炼（走规则降级）")
    parser.add_argument("--no-commit", action="store_true", help="不自动 git commit/push")
    args = parser.parse_args(sys.argv[1:])

    # 解析目标日期
    if args.date:
        target_date = args.date
    else:
        cst_now = datetime.now(TZ_SH)
        target_date = (cst_now - timedelta(days=1)).strftime("%Y-%m-%d")
    print(f"📅 目标日期: {target_date}")

    # 1. 抓取
    print("🕷️  抓取 raw json...")
    from crawlers.run_all import main as _ra_main
    try:
        _ra_main(["--date", target_date])
        print("✅ 抓取完成")
    except SystemExit as e:
        if e.code != 0:
            print(f"⚠️  抓取失败（exit {e.code}），继续用已有 raw json 跑生成")

    # 2. 生成
    print("🔧 生成日报...")
    from generators.generate_daily import main as _gd_main
    gen_args = ["--date", target_date]
    if args.no_llm:
        gen_args.append("--no-llm")
    rc = _gd_main(gen_args)
    if rc != 0:
        print(f"❌ 生成失败 (exit {rc})", file=sys.stderr)
        sys.exit(rc)

    # 3. 验证产物
    report_file = REPO_ROOT / "每日资讯" / f"{target_date}.md"
    if not report_file.exists():
        print(f"❌ 报告未生成: {report_file}", file=sys.stderr)
        sys.exit(1)
    report_bytes = report_file.stat().st_size
    print(f"✅ 报告已生成: {report_file} ({report_bytes} bytes)")

    # 4. commit + push（可选）
    if args.no_commit:
        print("ℹ️  --no-commit，跳过 git 同步")
        return

    from news_commit import commit_and_push
    commit_and_push(
        target_date=target_date,
        report_file=report_file,
    )


# ==================== 子命令：news-commit ====================

def news_commit() -> None:
    """一键 add + commit + push（替代 news-commit.sh）"""
    _check_repo_root()
    parser = argparse.ArgumentParser(
        prog="news-commit",
        description="AI 资讯追踪·一键 commit + push",
    )
    parser.add_argument("message", nargs="?", default="chore: 更新资讯", help="commit message")
    args = parser.parse_args(sys.argv[1:])

    from news_commit import commit_and_push
    commit_and_push(message=args.message)


# ==================== main ====================

if __name__ == "__main__":
    # 直接调 scripts/cli.py 时的友好提示
    print("⚠️  请通过 `uv run push-report / pm-inspect / run-generate-daily / news-commit` 调用", file=sys.stderr)
    print("   或 `uv run python scripts/cli.py <cmd> [args...]`", file=sys.stderr)
    sys.exit(1)
