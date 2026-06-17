"""
AI 资讯追踪·Git 一键 commit + push（v2.0 替代 news-commit.sh）
==============================================================

封装 git add + commit + push 的胶水逻辑，供 cli.py news-commit 和 run-generate-daily 调用。

设计：
- post-commit hook 会自动 push（per .git/hooks/post-commit 配置）
- 但有些环境（沙箱）无 post-commit hook → 这里直接 push 兜底
- 绕 git-lfs pre-push hook（沙箱内 git-lfs 每次新 session 缺），用 --no-verify
- commit message 含中文双引号时 → shell 解析异常 → 用 `git commit -F <file>` here-doc
"""

from __future__ import annotations

import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GIT_AUTHOR_NAME = "AI资讯追踪"
GIT_AUTHOR_EMAIL = "ai-news@zlink.cloud"


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """subprocess.run wrapper，统一 cwd=REPO_ROOT，捕获 stderr"""
    return subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        **kwargs,
    )


def has_changes() -> bool:
    """检查是否有未提交/未追踪的变更"""
    r = _run(["git", "status", "--porcelain"])
    return bool(r.stdout.strip())


def stage_files(paths: list[Path] | None = None) -> int:
    """git add；paths 为 None 时 add 全部"""
    if paths is None:
        r = _run(["git", "add", "."])
    else:
        r = _run(["git", "add", "-A", *[str(p) for p in paths]])
    return r.returncode


def commit(message: str) -> int:
    """git commit -m；中文双引号用 -F here-doc 兜底"""
    r = _run(["git", "diff", "--cached", "--quiet"])
    if r.returncode == 0:
        print("ℹ️  无 staged 变更，跳过 commit")
        return 0

    # 启发式：含中文 + 双引号 → 走 here-doc
    use_heredoc = bool(re.search(r'[\u4e00-\u9fff].*"', message))

    env_overrides = {
        "GIT_AUTHOR_NAME": GIT_AUTHOR_NAME,
        "GIT_AUTHOR_EMAIL": GIT_AUTHOR_EMAIL,
        "GIT_COMMITTER_NAME": GIT_AUTHOR_NAME,
        "GIT_COMMITTER_EMAIL": GIT_AUTHOR_EMAIL,
    }

    if use_heredoc:
        # 写临时文件 + git commit -F
        msg_file = REPO_ROOT / "logs" / ".commit_msg.tmp"
        msg_file.parent.mkdir(parents=True, exist_ok=True)
        msg_file.write_text(message, encoding="utf-8")
        r = _run(["git", "commit", "-F", str(msg_file)], env={**__import__("os").environ, **env_overrides})
        msg_file.unlink(missing_ok=True)
    else:
        r = _run(["git", "commit", "-m", message], env={**__import__("os").environ, **env_overrides})

    if r.returncode != 0:
        print(f"❌ git commit 失败: {r.stderr}", file=sys.stderr)
        return r.returncode
    print(f"✅ commit: {message}")
    return 0


def push() -> int:
    """git push --no-verify（绕 git-lfs pre-push hook 误报）"""
    r = _run(["git", "push", "--no-verify"])
    if r.returncode != 0:
        print(f"❌ git push 失败: {r.stderr}", file=sys.stderr)
        return r.returncode
    print("✅ push 完成")
    return 0


def commit_and_push(message: str | None = None, target_date: str | None = None, report_file: Path | None = None) -> int:
    """一键 commit + push

    Args:
        message: 自定义 commit message（None 时按 target_date 生成默认）
        target_date: 目标日期 YYYY-MM-DD（用于生成默认 commit message）
        report_file: 报告文件路径（add 进去；None 时 add 全部）
    """
    if not has_changes():
        print("ℹ️  无变更，跳过")
        return 0

    # 显示待提交文件
    r = _run(["git", "status", "--short"])
    print("📋 待提交文件：")
    for line in r.stdout.splitlines():
        print(f"   {line}")

    # stage
    if report_file is not None:
        rc = stage_files([report_file])
    else:
        rc = stage_files(None)
    if rc != 0:
        return rc

    # 默认 message
    if message is None:
        if target_date is None:
            target_date = datetime.now().strftime("%Y-%m-%d")
        message = f"feat(每日资讯): {target_date} 重新生成"

    rc = commit(message)
    if rc != 0:
        return rc
    return push()
