#!/bin/bash
# AI 资讯追踪·PM 巡检统一入口（v1.2 推送架构 + v1.3 8 维度）
#
# 流程：generate_inspection.py → push_report.sh
#   1. 跑 generate_inspection.py 生成 data/inspections/<date>-<HH-MM>.md（详细）
#                                          + data/inspections/<date>-<HH-MM>-feishu.md（飞书摘要）
#   2. 用 push_report.sh 推送**飞书摘要**到 purpose=management 的渠道（飞书主群）
#   3. 异常时：飞书群会看到 ⚠️/❌；同时 generate_inspection.py 内部走 owner 私聊升级
#
# 用法：
#   bash scripts/pm_inspect.sh 12:00   # 抓取状态巡检
#   bash scripts/pm_inspect.sh 20:00   # 推送状态巡检
#   bash scripts/pm_inspect.sh 12:00 --no-escalate   # 调试用：跳过 owner 私聊升级
#
# 依赖：generate_inspection.py + push_report.sh + .secrets

set -e
set -o pipefail

TZ_LABEL="Asia/Shanghai"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

if [[ $# -lt 1 ]]; then
    echo "用法: bash scripts/pm_inspect.sh [12:00|20:00] [--no-escalate]"
    exit 1
fi

TYPE="$1"
shift || true
EXTRA_ARGS=("$@")

DATE=$(TZ=$TZ_LABEL date '+%Y-%m-%d')
TIME_TAG=$(echo "$TYPE" | tr ':' '-')
INSPECT_FILE="data/inspections/${DATE}-${TIME_TAG}.md"
FEISHU_FILE="data/inspections/${DATE}-${TIME_TAG}-feishu.md"

cd "$REPO_ROOT"

echo "==================================="
echo "🔍 PM 巡检·$TYPE（v1.3 8 维度）"
echo "📅 $DATE"
echo "==================================="

# Step 1: 生成巡检报告（写详细 + 飞书摘要，stdout 也打印）
echo ""
echo "--- Step 1: 生成巡检报告 ---"
TZ=$TZ_LABEL python3 scripts/generators/generate_inspection.py "$TYPE" "${EXTRA_ARGS[@]}"

# 检查报告文件是否生成
if [[ ! -f "$INSPECT_FILE" ]]; then
    echo "❌ 巡检报告未生成: $INSPECT_FILE"
    exit 1
fi

# Step 2: 推送**飞书摘要**到 management 渠道
echo ""
echo "--- Step 2: 推送飞书摘要到 management 渠道 ---"
if [[ ! -f "$FEISHU_FILE" ]]; then
    echo "⚠️ 飞书摘要未生成: $FEISHU_FILE（fallback 推详细）"
    FEISHU_FILE="$INSPECT_FILE"
fi
bash scripts/push_report.sh "$FEISHU_FILE"

echo ""
echo "==================================="
echo "✅ PM 巡检完成: $TYPE → $INSPECT_FILE"
echo "==================================="
