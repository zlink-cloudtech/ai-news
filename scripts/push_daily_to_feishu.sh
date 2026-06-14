#!/bin/bash
# AI 资讯追踪 - 每日 8:30 推送昨日 AI 资讯日报到飞书群
#
# 流程：
#   1. 计算"昨天"日期（Asia/Shanghai）
#   2. 检查 每日资讯/<date>.md 是否存在（不存在则报错退出 —— 由 1:00 抓取任务负责生成）
#   3. 用 lark-cli docs +create 创建飞书云文档（markdown 完美渲染）
#   4. 构造 text 消息（含云文档链接）推 webhook
#   5. 写结构化 JSON 日志 logs/daily/<date>-push.json（供探针解析）
#
# 用法：
#   ./scripts/push_daily_to_feishu.sh                       # 推昨天
#   ./scripts/push_daily_to_feishu.sh 2026-06-12            # 推指定日期
#
# 依赖：
#   - 每日资讯/<date>.md 必须已由 scripts/run_generate_daily.sh 生成
#   - lark-cli user 身份已就绪（docx:document:create 已授权）
#   - 沙箱内 lark-cli 必须加 LARK_CLI_NO_PROXY=1 绕代理
#   - 沙箱内 curl 必须加 --noproxy '*' 绕代理
#   - 日志 schema 见 docs/log_schema.md

set -e

# ========== 配置 ==========
WEBHOOK_URL="https://open.feishu.cn/open-apis/bot/v2/hook/6313a73f-dfaf-4ce5-857c-2e05edd38897"
KEYWORD="AI资讯"  # 用户在自定义机器人里设置的校验词
TZ_LABEL="Asia/Shanghai"
LARK_CLI="lark-cli"  # 沙箱已预装
SCHEMA_VERSION="1.0"

# ========== 状态变量（供 trap 写日志用） ==========
TARGET_DATE=""
STARTED_AT=""
ENDED_AT=""
PUSH_OK="false"
DOC_URL=""
WEBHOOK_CODE=""
WEBHOOK_MSG=""
ITEMS_PUSHED="0"
ERROR_MSG=""
SCRIPT_RC="0"

# ========== 参数解析 ==========
for arg in "$@"; do
    case "$arg" in
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

# ========== 准备日志目录 + 启动时间 ==========
mkdir -p logs/daily
STARTED_AT=$(TZ=$TZ_LABEL date -Iseconds)
LOG_FILE="logs/daily/${TARGET_DATE}-push.json"

# ========== EXIT trap：无论成败都写 JSON 日志 ==========
write_log() {
    ENDED_AT=$(TZ=$TZ_LABEL date -Iseconds)
    local started_epoch ended_epoch duration_sec
    started_epoch=$(date -d "$STARTED_AT" +%s 2>/dev/null || echo 0)
    ended_epoch=$(date -d "$ENDED_AT" +%s 2>/dev/null || echo 0)
    duration_sec=$((ended_epoch - started_epoch))

    python3 - "$LOG_FILE" "$SCHEMA_VERSION" "$TARGET_DATE" "$STARTED_AT" "$ENDED_AT" \
            "$duration_sec" "$PUSH_OK" "$ITEMS_PUSHED" "$DOC_URL" \
            "$WEBHOOK_CODE" "$WEBHOOK_MSG" "$ERROR_MSG" << 'PYEOF' 2>/dev/null
import json, sys
log_file, schema, date, started, ended, dur, ok, items, doc_url, code, msg, err = sys.argv[1:13]
payload = {
    "schema_version": schema,
    "date": date,
    "step": "push",
    "ok": ok == "true",
    "started_at": started,
    "ended_at": ended,
    "duration_sec": int(dur),
    "metrics": {
        "items_pushed": int(items)
    },
    "outputs": {
        "doc_url": doc_url if doc_url else None,
        "report_path": f"每日资讯/{date}.md"
    },
    "webhook": {
        "code": int(code) if code else None,
        "msg": msg if msg else None
    },
    "error": err if err else None
}
with open(log_file, "w", encoding="utf-8") as f:
    json.dump(payload, f, ensure_ascii=False, indent=2)
PYEOF
    if [[ -f "$LOG_FILE" ]]; then
        echo "📝 结构化日志: $LOG_FILE"
    fi
}

trap 'SCRIPT_RC=$?; write_log' EXIT

# ========== 主流程（任何一步失败 → trap 写失败日志） ==========

# ---- 检查产物 ----
REPORT_FILE="每日资讯/${TARGET_DATE}.md"
if [[ ! -f "$REPORT_FILE" ]]; then
    ERROR_MSG="报告不存在: $REPORT_FILE（先跑 ./scripts/run_generate_daily.sh 生成）"
    echo "❌ $ERROR_MSG"
    exit 1
fi
echo "✅ 报告已就绪: $REPORT_FILE ($(wc -c < "$REPORT_FILE") bytes)"

# ---- 估算推送条数（粗略：满级 Top + 其余简版） ----
ITEMS_PUSHED=$(grep -cE '(\*\*重要程度\*\*|\*\*[🔴🟡🟢🔵]\*\*)' "$REPORT_FILE" 2>/dev/null || echo "0")

# ---- 创建飞书云文档 ----
echo "📄 创建飞书云文档..."
# 注意：lark-cli docs +create 的 --content 支持 @file 语法读取文件内容
DOC_OUTPUT=$(LARK_CLI_NO_PROXY=1 $LARK_CLI docs +create \
    --as user \
    --doc-format markdown \
    --content "@$REPORT_FILE" 2>&1)
echo "   $(echo "$DOC_OUTPUT" | head -1)"

DOC_URL=$(echo "$DOC_OUTPUT" | python3 -c "
import sys, json
raw = sys.stdin.read()
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
    ERROR_MSG="创建飞书云文档失败：${DOC_OUTPUT:0:200}"
    echo "❌ $ERROR_MSG"
    exit 1
fi
echo "✅ 云文档已创建: $DOC_URL"

# ---- 构造 webhook payload (text 类型) ----
echo "📤 构造 webhook text payload..."

TEXT_MSG=$(cat << EOF
📰 AI 资讯日报 · ${TARGET_DATE}

完整报告：${DOC_URL}
EOF
)

# 验证消息含校验词
if ! echo "$TEXT_MSG" | grep -q "$KEYWORD"; then
    ERROR_MSG="消息不含校验词 '$KEYWORD'，飞书会拒收"
    echo "❌ $ERROR_MSG"
    exit 1
fi

echo "$TEXT_MSG" > /tmp/feishu_push_text.txt

python3 << 'PYEOF' > /tmp/feishu_push_payload.json
import json
with open("/tmp/feishu_push_text.txt", "r", encoding="utf-8") as f:
    text = f.read()
payload = {"msg_type": "text", "content": {"text": text}}
print(json.dumps(payload, ensure_ascii=False))
PYEOF

# ---- 推送 ----
echo "🚀 推送 webhook..."
RESPONSE=$(curl -sS --noproxy '*' -X POST "$WEBHOOK_URL" \
    -H 'Content-Type: application/json' \
    -d @/tmp/feishu_push_payload.json)

echo "📥 webhook 响应: $RESPONSE"

# ---- 解析 webhook 响应 ----
WEBHOOK_CODE=$(echo "$RESPONSE" | python3 -c "
import sys, json
try:
    data = json.loads(sys.stdin.read())
    print(data.get('code', -1))
except Exception:
    print(-1)
")
WEBHOOK_MSG=$(echo "$RESPONSE" | python3 -c "
import sys, json
try:
    data = json.loads(sys.stdin.read())
    print(data.get('msg', ''))
except Exception:
    print('')
")

# ---- 校验响应 ----
if [[ "$WEBHOOK_CODE" == "0" ]]; then
    PUSH_OK="true"
    echo "✅ 推送成功: $TARGET_DATE → $DOC_URL"
    exit 0
else
    ERROR_MSG="webhook 失败: code=$WEBHOOK_CODE msg=$WEBHOOK_MSG"
    echo "❌ $ERROR_MSG"
    exit 1
fi
