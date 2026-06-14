#!/bin/bash
# AI 资讯追踪 - 每日 8:30 自动推送到飞书群
#
# 流程：
#   1. 计算"昨天"日期（Asia/Shanghai）
#   2. 跑 generate_daily.py 重抓昨天（默认就是昨天）
#   3. 读生成的 .md 报告
#   4. 改标题加 "AI资讯" 校验词
#   5. 构造 interactive card 推 webhook
#
# 用法：
#   ./scripts/push_daily_to_feishu.sh                       # 推昨天
#   ./scripts/push_daily_to_feishu.sh 2026-06-12            # 推指定日期
#   ./scripts/push_daily_to_feishu.sh --no-llm              # 不开 LLM 精炼
#
# 环境要求：
#   - webhook URL 写入下方 WEBHOOK_URL 变量
#   - 沙箱内 curl 必须加 --noproxy '*' 绕代理

set -e

# ========== 配置 ==========
WEBHOOK_URL="https://open.feishu.cn/open-apis/bot/v2/hook/6313a73f-dfaf-4ce5-857c-2e05edd38897"
KEYWORD="AI资讯"  # 用户在自定义机器人里设置的校验词
TZ_LABEL="Asia/Shanghai"

# ========== 参数解析 ==========
TARGET_DATE=""
NO_LLM_FLAG=""

for arg in "$@"; do
    case "$arg" in
        --no-llm) NO_LLM_FLAG="--no-llm" ;;
        20[0-9][0-9]-[0-1][0-9]-[0-3][0-9]) TARGET_DATE="$arg" ;;
        *) echo "未知参数: $arg"; exit 1 ;;
    esac
done

# ========== 计算目标日期 ==========
if [[ -z "$TARGET_DATE" ]]; then
    TARGET_DATE=$(TZ=$TZ_LABEL date -d "yesterday" +%Y-%m-%d)
fi
echo "📅 目标日期: $TARGET_DATE"

# ========== 切到仓库根目录 ==========
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$REPO_ROOT"
echo "📂 仓库根目录: $REPO_ROOT"

# ========== 跑生成器 ==========
echo "🔧 生成日报..."
if [[ -n "$NO_LLM_FLAG" ]]; then
    python3 scripts/generators/generate_daily.py --date "$TARGET_DATE" $NO_LLM_FLAG
else
    python3 scripts/generators/generate_daily.py --date "$TARGET_DATE"
fi

# ========== 检查产物 ==========
REPORT_FILE="每日资讯/${TARGET_DATE}.md"
if [[ ! -f "$REPORT_FILE" ]]; then
    echo "❌ 报告未生成: $REPORT_FILE"
    exit 1
fi
echo "✅ 报告已生成: $REPORT_FILE ($(wc -c < "$REPORT_FILE") bytes)"

# ========== 构造 webhook payload ==========
echo "📤 构造飞书 interactive card payload..."

# 替换标题以保证含校验词
python3 << PYEOF
import json, sys

target_date = "$TARGET_DATE"
report_path = "每日资讯/${TARGET_DATE}.md"

with open(report_path, "r", encoding="utf-8") as f:
    content = f.read()

# 标题里加 AI资讯
content = content.replace("# 🤖 AI每日资讯", "# 🤖 AI资讯日报")

# 构造 card
card = {
    "config": {"wide_screen_mode": True},
    "header": {
        "template": "blue",
        "title": {"content": f"🤖 AI资讯日报 · {target_date}", "tag": "plain_text"}
    },
    "elements": [
        {"tag": "markdown", "content": content}
    ]
}

payload = {"msg_type": "interactive", "card": card}

with open("/tmp/card_push.json", "w", encoding="utf-8") as f:
    json.dump(payload, f, ensure_ascii=False)

print(f"   payload size: {len(json.dumps(payload, ensure_ascii=False))} bytes")
print(f"   content chars: {len(content)}")
PYEOF

# ========== 推送 ==========
echo "🚀 推送 webhook..."
RESPONSE=$(curl -sS --noproxy '*' -X POST "$WEBHOOK_URL" \
    -H 'Content-Type: application/json' \
    -d @/tmp/card_push.json)

echo "📥 webhook 响应: $RESPONSE"

# ========== 校验响应 ==========
if echo "$RESPONSE" | grep -q '"code":0'; then
    echo "✅ 推送成功: $TARGET_DATE"
    exit 0
else
    echo "❌ 推送失败: $TARGET_DATE"
    exit 1
fi
