#!/bin/bash
# AI 资讯追踪 - 每日 8:30 自动推送到飞书群
#
# 流程：
#   1. 计算"昨天"日期（Asia/Shanghai）
#   2. 跑 generate_daily.py 重抓昨天（默认就是昨天）
#   3. 用 lark-cli docs +create 创建飞书云文档（markdown 完美渲染）
#   4. 构造 text 消息（含云文档链接）推 webhook
#
# 用法：
#   ./scripts/push_daily_to_feishu.sh                       # 推昨天
#   ./scripts/push_daily_to_feishu.sh 2026-06-12            # 推指定日期
#   ./scripts/push_daily_to_feishu.sh --no-llm              # 不开 LLM 精炼
#
# 环境要求：
#   - webhook URL 写入下方 WEBHOOK_URL 变量
#   - lark-cli user 身份已就绪（im:message 域已授权 + docx:document:create 已授权）
#   - 沙箱内 lark-cli 必须加 LARK_CLI_NO_PROXY=1 绕代理
#   - 沙箱内 curl 必须加 --noproxy '*' 绕代理

set -e

# ========== 配置 ==========
WEBHOOK_URL="https://open.feishu.cn/open-apis/bot/v2/hook/6313a73f-dfaf-4ce5-857c-2e05edd38897"
KEYWORD="AI资讯"  # 用户在自定义机器人里设置的校验词
TZ_LABEL="Asia/Shanghai"
LARK_CLI="lark-cli"  # 沙箱已预装

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

# ========== 创建飞书云文档 ==========
echo "📄 创建飞书云文档..."
DOC_OUTPUT=$(LARK_CLI_NO_PROXY=1 $LARK_CLI docs +create \
    --as user \
    --doc-format markdown \
    --content "$REPORT_FILE" 2>&1)
echo "   $DOC_OUTPUT" | head -3

# 提取 document_id 和 url
DOC_URL=$(echo "$DOC_OUTPUT" | python3 -c "
import sys, json
raw = sys.stdin.read()
# 跳过 WARN 行
lines = raw.split('\n')
for line in lines:
    line = line.strip()
    if line.startswith('{'):
        try:
            data = json.loads(line)
            if data.get('ok') and 'data' in data:
                print(data['data']['document']['url'])
                sys.exit(0)
        except json.JSONDecodeError:
            pass
sys.exit(1)
")

if [[ -z "$DOC_URL" ]]; then
    echo "❌ 创建飞书云文档失败"
    echo "   完整输出: $DOC_OUTPUT"
    exit 1
fi
echo "✅ 云文档已创建: $DOC_URL"

# ========== 构造 webhook payload (text 类型) ==========
echo "📤 构造 webhook text payload..."

TEXT_MSG=$(cat << EOF
📰 AI资讯日报 · ${TARGET_DATE}（飞书云文档 · 完美渲染）

✅ 点开看完整报告：
${DOC_URL}

📌 表格 / 标题 / 链接 / 标签全部原生渲染
📌 历史报告：https://github.com/zlink-cloudtech/ai-news/tree/main/每日资讯

来源：zlink-cloudtech/ai-news · AI资讯追踪
EOF
)

# 验证消息含校验词
if ! echo "$TEXT_MSG" | grep -q "$KEYWORD"; then
    echo "❌ 消息不含校验词 '$KEYWORD'，飞书会拒收"
    exit 1
fi

# 写到临时文件，避免特殊字符破坏 python3 -c 字符串
echo "$TEXT_MSG" > /tmp/feishu_push_text.txt

python3 << 'PYEOF' > /tmp/feishu_push_payload.json
import json
with open("/tmp/feishu_push_text.txt", "r", encoding="utf-8") as f:
    text = f.read()
payload = {"msg_type": "text", "content": {"text": text}}
print(json.dumps(payload, ensure_ascii=False))
PYEOF

# ========== 推送 ==========
echo "🚀 推送 webhook..."
RESPONSE=$(curl -sS --noproxy '*' -X POST "$WEBHOOK_URL" \
    -H 'Content-Type: application/json' \
    -d @/tmp/feishu_push_payload.json)

echo "📥 webhook 响应: $RESPONSE"

# ========== 校验响应 ==========
if echo "$RESPONSE" | grep -q '"code":0'; then
    echo "✅ 推送成功: $TARGET_DATE → $DOC_URL"
    exit 0
else
    echo "❌ 推送失败: $TARGET_DATE"
    exit 1
fi
