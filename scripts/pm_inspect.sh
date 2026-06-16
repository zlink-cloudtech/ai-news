#!/bin/bash
# AI 资讯追踪·PM 巡检统一入口（v1.2 推送架构）
#
# 流程：generate_inspection.py → push_report.sh
#   1. 跑 generate_inspection.py 生成 data/inspections/<date>-<HH-MM>.md
#   2. 用 push_report.sh 推送到 purpose=management 的渠道（飞书主群）
#   3. 异常时：飞书群会看到 ⚠️/❌；正常时：也照常发（management 渠道是设计内）
#
# 用法：
#   bash scripts/pm_inspect.sh 12:00   # 抓取状态巡检
#   bash scripts/pm_inspect.sh 20:00   # 推送状态巡检
#
# 依赖：generate_inspection.py + push_report.sh + .secrets

set -e
set -o pipefail

TZ_LABEL="Asia/Shanghai"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

if [[ $# -lt 1 ]]; then
    echo "用法: bash scripts/pm_inspect.sh [12:00|20:00]"
    exit 1
fi

TYPE="$1"
DATE=$(TZ=$TZ_LABEL date '+%Y-%m-%d')
TIME_TAG=$(echo "$TYPE" | tr ':' '-')
INSPECT_FILE="data/inspections/${DATE}-${TIME_TAG}.md"

cd "$REPO_ROOT"

echo "==================================="
echo "🔍 PM 巡检·$TYPE（v1.2 推送架构）"
echo "📅 $DATE"
echo "==================================="

# Step 1: 生成巡检报告（写文件 + stdout）
echo ""
echo "--- Step 1: 生成巡检报告 ---"
TZ=$TZ_LABEL python3 scripts/generators/generate_inspection.py "$TYPE"

# 检查报告文件是否生成
if [[ ! -f "$INSPECT_FILE" ]]; then
    echo "❌ 巡检报告未生成: $INSPECT_FILE"
    exit 1
fi

# Step 2: 推送到 management 渠道
echo ""
echo "--- Step 2: 推送巡检报告到 management 渠道 ---"
bash scripts/push_report.sh "$INSPECT_FILE"

echo ""
echo "==================================="
echo "✅ PM 巡检完成: $TYPE → $INSPECT_FILE"
echo "==================================="
