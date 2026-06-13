#!/bin/bash
# AI 资讯追踪仓库 - 一键提交并推送脚本
# 用法：
#   ./scripts/news-commit.sh "commit message"
#   ./scripts/news-commit.sh "feat(每日资讯): 2026-06-12"   # 推荐格式
#
# 功能：
#   1. 添加所有变更文件
#   2. 提交（自动触发 post-commit hook → 自动 push）
#
# 注意：
#   - 需要在仓库根目录执行
#   - 需要 git remote URL 已配置好 PAT

set -e

COMMIT_MSG="${1:-chore: 更新资讯}"

# 切到仓库根目录（脚本所在目录的上一级）
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$REPO_ROOT"

echo "📂 仓库根目录: $REPO_ROOT"
echo "💬 Commit message: $COMMIT_MSG"
echo ""

# 检查是否有变更
if [[ -z $(git status --porcelain) ]]; then
  echo "ℹ️  没有需要提交的变更"
  exit 0
fi

# 显示待提交文件
echo "📋 待提交文件："
git status --short
echo ""

# 添加并提交（post-commit hook 会自动 push）
git add .
git commit -m "$COMMIT_MSG"

echo ""
echo "✅ 提交完成，post-commit hook 已自动 push 到 origin main"
git log --oneline -1
git ls-remote origin | head -1
