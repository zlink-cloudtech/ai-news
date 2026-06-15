#!/bin/bash
# AI 资讯追踪 - 每日 8:30 推送昨日 AI 资讯日报到多渠道
#
# 支持渠道（config/channels.json 控制开关 + 参数）：
#   - feishu: 飞书自定义机器人 webhook（带签名 + 校验词）
#   - wecom:  企业微信群机器人 webhook（markdown，key 已在 URL）
#
# 流程：
#   1. 计算"昨天"日期（Asia/Shanghai）
#   2. 加载 config/channels.json，遍历 enabled 渠道
#   3. 检查 每日资讯/<date>.md 是否存在
#   4. 用 lark-cli docs +create 创建飞书云文档（飞书/企业微信都需要 docx URL）
#   5. 各渠道独立推送（任一失败不阻塞其他）
#   6. 写结构化 JSON 日志 logs/daily/<date>-push.json（探针可解析）
#
# 用法：
#   ./scripts/push_daily_to_feishu.sh                       # 推昨天
#   ./scripts/push_daily_to_feishu.sh 2026-06-12            # 推指定日期
#
# 依赖：
#   - config/channels.json（必须）
#   - 每日资讯/<date>.md 必须已由 scripts/run_generate_daily.sh 生成
#   - lark-cli user 身份已就绪（docx:document:create 已授权）
#   - 沙箱内 lark-cli 必须加 LARK_CLI_NO_PROXY=1 绕代理
#   - 沙箱内 curl 必须加 --noproxy '*' 绕代理
#   - 日志 schema 见 docs/log_schema.md
#
# 扩展新渠道：
#   1) config/channels.json 加一个 channel（含 enabled=true + type=xxx）
#   2) 本脚本的 PUSHERS dict 加 push_xxx 函数
#   3) 改 inspect 探针读 push.json 逻辑（如有需要）

set -e

# ========== 基础配置 ==========
TZ_LABEL="Asia/Shanghai"
LARK_CLI="lark-cli"
SCHEMA_VERSION="1.0"
CHANNELS_CONFIG="config/channels.json"

# ========== 状态变量（供 trap 写日志用） ==========
TARGET_DATE=""
STARTED_AT=""
LOG_FILE=""
DOC_URL=""
ITEMS_PUSHED="0"
ERROR_MSG=""
PUSH_OK="false"          # 任一渠道成功即 true
WEBHOOK_CODE=""          # 兼容旧探针：取飞书渠道（若无则取首个成功）
WEBHOOK_MSG=""
CHANNELS_RESULT="{}"     # JSON 字符串：{"feishu":{"ok":true,"code":0,"msg":"..."}, ...}

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

# ========== 检查配置 ==========
if [[ ! -f "$CHANNELS_CONFIG" ]]; then
    echo "❌ 配置文件不存在: $CHANNELS_CONFIG"
    exit 1
fi

# ========== 准备日志目录 + 启动时间 ==========
mkdir -p logs/daily
STARTED_AT=$(TZ=$TZ_LABEL date -Iseconds)
LOG_FILE="logs/daily/${TARGET_DATE}-push.json"

# ========== EXIT trap：无论成败都写 JSON 日志 ==========
write_log() {
    local ended_at duration_sec started_epoch ended_epoch
    ended_at=$(TZ=$TZ_LABEL date -Iseconds)
    started_epoch=$(date -d "$STARTED_AT" +%s 2>/dev/null || echo 0)
    ended_epoch=$(date -d "$ended_at" +%s 2>/dev/null || echo 0)
    duration_sec=$((ended_epoch - started_epoch))

    python3 - "$LOG_FILE" "$SCHEMA_VERSION" "$TARGET_DATE" "$STARTED_AT" "$ended_at" \
            "$duration_sec" "$PUSH_OK" "$ITEMS_PUSHED" "$DOC_URL" \
            "$WEBHOOK_CODE" "$WEBHOOK_MSG" "$CHANNELS_RESULT" "$ERROR_MSG" << 'PYEOF' 2>/dev/null
import json, sys
log_file, schema, date, started, ended, dur, ok, items, doc_url, code, msg, channels_json, err = sys.argv[1:14]
try:
    channels = json.loads(channels_json)
except Exception:
    channels = {}
payload = {
    "schema_version": schema,
    "date": date,
    "step": "push",
    "ok": ok == "true",
    "started_at": started,
    "ended_at": ended,
    "duration_sec": int(dur),
    "metrics": {
        "items_pushed": int(items),
        "channels_total": len(channels),
        "channels_ok": sum(1 for c in channels.values() if c.get("ok"))
    },
    "outputs": {
        "doc_url": doc_url if doc_url else None,
        "report_path": f"每日资讯/{date}.md"
    },
    "webhook": {
        "code": int(code) if code not in ("", None) else None,
        "msg": msg if msg else None
    },
    "channels": channels,
    "error": err if err else None
}
with open(log_file, "w", encoding="utf-8") as f:
    json.dump(payload, f, ensure_ascii=False, indent=2)
PYEOF
    if [[ -f "$LOG_FILE" ]]; then
        echo "📝 结构化日志: $LOG_FILE"
    fi
}
trap 'write_log' EXIT

# ========== 主流程 ==========

# ---- 检查产物 ----
REPORT_FILE="每日资讯/${TARGET_DATE}.md"
if [[ ! -f "$REPORT_FILE" ]]; then
    ERROR_MSG="报告不存在: $REPORT_FILE（先跑 ./scripts/run_generate_daily.sh 生成）"
    echo "❌ $ERROR_MSG"
    exit 1
fi
echo "✅ 报告已就绪: $REPORT_FILE ($(wc -c < "$REPORT_FILE") bytes)"

# ---- 估算推送条数 ----
ITEMS_PUSHED=$(grep -cE '(\*\*重要程度\*\*|\*\*[🔴🟡🟢🔵]\*\*)' "$REPORT_FILE" 2>/dev/null || echo "0")

# ---- 加载 enabled 渠道列表 ----
ENABLED_CHANNELS=($(python3 -c "
import json
cfg = json.load(open('$CHANNELS_CONFIG'))
print(' '.join(n for n, c in cfg.get('channels', {}).items() if c.get('enabled')))
"))
if [[ ${#ENABLED_CHANNELS[@]} -eq 0 ]]; then
    ERROR_MSG="没有任何启用的推送渠道（$CHANNELS_CONFIG 里所有 channel.enabled 都是 false）"
    echo "❌ $ERROR_MSG"
    exit 1
fi
echo "📡 启用渠道: ${ENABLED_CHANNELS[*]}"

# ---- 飞书/企业微信渠道需要 docx URL（lark-cli 一次性创建，URL 给两边复用） ----
NEED_DOCX="false"
for ch in "${ENABLED_CHANNELS[@]}"; do
    ch_type=$(python3 -c "import json; print(json.load(open('$CHANNELS_CONFIG'))['channels'].get('$ch',{}).get('type',''))")
    if [[ "$ch_type" == "feishu" || "$ch_type" == "wecom" ]]; then
        NEED_DOCX="true"
        break
    fi
done

if [[ "$NEED_DOCX" == "true" ]]; then
    echo "📄 创建飞书云文档..."
    DOC_OUTPUT=$(LARK_CLI_NO_PROXY=1 $LARK_CLI docs +create \
        --as user \
        --doc-format markdown \
        --content "@$REPORT_FILE" 2>&1)
    echo "   $(echo "$DOC_OUTPUT" | head -1)"

    DOC_URL=$(echo "$DOC_OUTPUT" | python3 -c "
import sys, json
raw = sys.stdin.read()
try:
    data = json.loads(raw)
    if data.get('ok') and 'data' in data and 'document' in data['data']:
        print(data['data']['document']['url'])
        sys.exit(0)
except (json.JSONDecodeError, KeyError, TypeError) as e:
    print(f'PARSE_ERROR: {e}', file=sys.stderr)
sys.exit(1)
")

    if [[ -z "$DOC_URL" ]]; then
        ERROR_MSG="创建飞书云文档失败：${DOC_OUTPUT:0:200}"
        echo "❌ $ERROR_MSG"
        exit 1
    fi
    echo "✅ 云文档已创建: $DOC_URL"
fi

# ---- 推送 + 汇总（python3 内部 try/except 包裹，单渠道失败不阻塞脚本） ----
echo ""
echo "==================================="
echo "🚀 开始推送 ${TARGET_DATE} 报告"
echo "==================================="

PY_OUTPUT=$(python3 2>&1 - "$TARGET_DATE" "$DOC_URL" "$CHANNELS_CONFIG" "${ENABLED_CHANNELS[@]}" << 'PYEOF'
import json, sys, hmac, hashlib, base64, time, subprocess

target_date, doc_url, cfg_path = sys.argv[1], sys.argv[2], sys.argv[3]
enabled = sys.argv[4:]

# ---- 渠道推送函数库 ----
def curl_post(url, payload):
    return subprocess.run(
        ["curl", "-sS", "--noproxy", "*", "-X", "POST", url,
         "-H", "Content-Type: application/json",
         "-d", json.dumps(payload, ensure_ascii=False)],
        capture_output=True, text=True, timeout=15
    ).stdout

def push_feishu(ch_cfg):
    """飞书自定义机器人：text 极简版 + HMAC-SHA256 签名 + 校验词"""
    text = f"📰 AI资讯日报 · {target_date}\n\n完整报告：{doc_url}"
    kw = ch_cfg.get("keyword", "")
    if kw and kw not in text:
        return {"ok": False, "code": -1, "msg": f"keyword '{kw}' missing in text"}
    ts = str(int(time.time()))
    secret = ch_cfg.get("webhook_secret", "")
    # 飞书算法：key = f"{ts}\n{secret}"，msg = b""，HMAC-SHA256 → base64
    key = f"{ts}\n{secret}".encode("utf-8")
    sign = base64.b64encode(hmac.new(key, b"", hashlib.sha256).digest()).decode("utf-8")
    payload = {"timestamp": ts, "sign": sign, "msg_type": "text", "content": {"text": text}}
    resp = curl_post(ch_cfg["webhook_url"], payload)
    try:
        d = json.loads(resp)
        return {"ok": d.get("code") == 0, "code": d.get("code", -1), "msg": d.get("msg", "")}
    except Exception as e:
        return {"ok": False, "code": -1, "msg": f"parse err: {e}; resp={resp[:200]}"}

def push_wecom(ch_cfg):
    """企业微信群机器人：markdown（支持 [文本](url) 链接）"""
    content = f"📰 **AI资讯日报** · {target_date}\n\n[👉 完整报告]({doc_url})"
    payload = {"msgtype": "markdown", "markdown": {"content": content}}
    resp = curl_post(ch_cfg["webhook_url"], payload)
    try:
        d = json.loads(resp)
        return {"ok": d.get("errcode") == 0, "code": d.get("errcode", -1), "msg": d.get("errmsg", "")}
    except Exception as e:
        return {"ok": False, "code": -1, "msg": f"parse err: {e}; resp={resp[:200]}"}

PUSHERS = {
    "feishu": push_feishu,
    "wecom":  push_wecom,
}

# ---- 读配置 + 遍历 enabled 渠道 ----
try:
    with open(cfg_path) as f:
        cfg = json.load(f)
except Exception as e:
    print(f"❌ 配置读取失败: {e}", flush=True)
    print("PUSH_OK_BOOL::false")
    print("CHANNELS_JSON::{}")
    sys.exit(0)

results = {}
any_ok = False

for name in enabled:
    ch_cfg = cfg.get("channels", {}).get(name, {})
    ch_type = ch_cfg.get("type", "")
    pusher = PUSHERS.get(ch_type)
    print(f"\n🚀 渠道: {name} (type={ch_type})", flush=True)
    if not pusher:
        results[name] = {"ok": False, "code": -1, "msg": f"no pusher for type '{ch_type}' (scripts/push_daily_to_feishu.sh PUSHERS dict 未注册)"}
        print(f"   ❌ {results[name]['msg']}", flush=True)
        continue
    if not ch_cfg.get("webhook_url"):
        results[name] = {"ok": False, "code": -1, "msg": "missing webhook_url in config"}
        print(f"   ❌ {results[name]['msg']}", flush=True)
        continue
    res = pusher(ch_cfg)
    results[name] = res
    status = "✅" if res["ok"] else "❌"
    print(f"   {status} code={res['code']} msg={res['msg']}", flush=True)
    if res["ok"]:
        any_ok = True

print(f"\n📊 汇总: {json.dumps(results, ensure_ascii=False)}", flush=True)

# 计算 webhook_code/msg（feishu 优先，否则首个 ok），给旧探针 webhook.code 字段
webhook_code = -1
webhook_msg = ""
if results.get("feishu", {}).get("ok"):
    webhook_code = results["feishu"].get("code", -1)
    webhook_msg = results["feishu"].get("msg", "")
else:
    for n, d in results.items():
        if d.get("ok"):
            webhook_code = d.get("code", -1)
            webhook_msg = d.get("msg", "")
            break

# 输出 PUSH_OK_BOOL + CHANNELS_JSON + WEBHOOK_CODE + WEBHOOK_MSG（4 行用 :: 分隔前缀，bash 解析）
print(f"PUSH_OK_BOOL::{'true' if any_ok else 'false'}")
print(f"CHANNELS_JSON::{json.dumps(results, ensure_ascii=False)}")
print(f"WEBHOOK_CODE::{webhook_code}")
print(f"WEBHOOK_MSG::{webhook_msg}")
PYEOF
)

# 把 python3 内部日志（非协议行）显示给用户
echo "$PY_OUTPUT" | grep -v -E '^(PUSH_OK_BOOL|CHANNELS_JSON|WEBHOOK_CODE|WEBHOOK_MSG)::'

# 解析 python3 输出
PUSH_OK=$(echo "$PY_OUTPUT" | grep '^PUSH_OK_BOOL::' | sed 's/^PUSH_OK_BOOL:://' | head -1)
CHANNELS_RESULT=$(echo "$PY_OUTPUT" | grep '^CHANNELS_JSON::' | sed 's/^CHANNELS_JSON:://' | head -1)
[[ -z "$CHANNELS_RESULT" ]] && CHANNELS_RESULT="{}"
WEBHOOK_CODE=$(echo "$PY_OUTPUT" | grep "^WEBHOOK_CODE::" | sed "s/^WEBHOOK_CODE:://" | head -1)
WEBHOOK_MSG=$(echo "$PY_OUTPUT" | grep "^WEBHOOK_MSG::" | sed "s/^WEBHOOK_MSG:://" | head -1)
[[ -z "$WEBHOOK_CODE" ]] && WEBHOOK_CODE="-1"
[[ -z "$WEBHOOK_MSG" ]] && WEBHOOK_MSG=""

# 取 webhook 字段（兼容旧探针）：优先飞书渠道，否则首个成功渠道
echo "📊 最终: PUSH_OK=$PUSH_OK / webhook_code=$WEBHOOK_CODE"
echo "==================================="

# 任一成功 → exit 0（让 trap 写 ok=true 的 log）
if [[ "$PUSH_OK" == "true" ]]; then
    exit 0
else
    ERROR_MSG="所有渠道都失败：$CHANNELS_RESULT"
    echo "❌ $ERROR_MSG"
    exit 1
fi
