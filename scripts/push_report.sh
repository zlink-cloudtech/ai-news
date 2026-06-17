#!/bin/bash
# AI 资讯追踪 - 通用推送脚本（v1.4 推送架构：手动推送 + 发布记录）
# 支持 5 类 REPORT_TYPE：daily / weekly / special / management / test
#
# 用法：
#   ./scripts/push_report.sh 每日资讯/2026-06-14.md                    # 推日报（默认 skip-if-pushed）
#   ./scripts/push_report.sh 每周汇总/2026-W24.md                     # 推周报（v1.5 拍板：路径名=每周汇总）
#   ./scripts/push_report.sh 专题/2026-06-foundation-models.md       # 推专题
#   ./scripts/push_report.sh data/inspections/2026-06-16-20-00.md     # 推管理性消息（巡检）
#   ./scripts/push_report.sh --test [<channel_name>]                  # 推测试/探活（默认所有 enabled+test 渠道）
#   ./scripts/push_report.sh 每日资讯/2026-06-15.md --repush          # 强制重推
#   ./scripts/push_report.sh 每日资讯/2026-06-15.md --dry-run         # 干跑（落 _dryrun/）
#   ./scripts/push_report.sh --list-published [daily|weekly|special|all]
#   ./scripts/push_report.sh --backfill daily --since 2026-06-10 --until 2026-06-16
#   ./scripts/push_report.sh --help                                    # 帮助
#
# v1.4 新增：
#   - EXIT trap 自动调 scripts/published_record.py record 写 data/published/<type>/<date>.json
#   - 默认 --skip-if-pushed（已推过则静默跳过，不刷屏）；--repush 强制重推
#   - --dry-run 落档 _dryrun/（不入主索引）
#   - --list-published / --backfill 运维 CLI
#
# 渠道加载（config/channels.json v1.2.1）：
#   1. 加载 .secrets（注入 env vars；缺则 fail-fast 报错）
#   2. 读 channels.json（v1.2 强制 env 引用，缺 env 变量 fail-fast）
#   3. 推断 REPORT_TYPE（按文件路径）
#   4. 加载 enabled + purpose 含 REPORT_TYPE + message_types 含 REPORT_TYPE 的渠道
#      （TEST_MODE：purpose 含 "test" + message_types 含 "test"）
#
# 5 类 REPORT_TYPE 处理：
#   - daily / weekly / special：建飞书云文档（公开访问） + 推文本+链接
#   - management：纯文本（无 docx，避免污染正式群文档库；仅发到 purpose 含 management 的渠道）
#   - test：纯文本（无 docx） + 落档 /var/log/ai-news/test_messages/（沙箱无权限退 $HOME/.local/share/）
#
# 扩展新渠道：
#   1) channels.json 加 channel（含 enabled/type/purpose/message_types + env 引用）
#   2) 本脚本 PUSHERS dict 注册 push_xxx 函数
#
# 依赖：
#   - config/channels.json（v1.2.1 schema）
#   - .secrets（已 gitignored）
#   - scripts/published_record.py（v1.4 引入；发布记录）
#   - lark-cli bot 身份已就绪（docs:document:create + docs:permission:setting 已授权）
#   - 沙箱内 lark-cli 必须加 LARK_CLI_NO_PROXY=1；curl 加 --noproxy '*'

set -e
set -o pipefail

# ========== 基础配置 ==========
TZ_LABEL="Asia/Shanghai"
LARK_CLI="lark-cli"
SCHEMA_VERSION="1.2"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
CHANNELS_CONFIG="${CHANNELS_CONFIG:-$REPO_ROOT/config/channels.json}"
SECRETS_FILE="${SECRETS_FILE:-$REPO_ROOT/.secrets}"

# ========== 状态变量（trap 写日志用） ==========
STARTED_AT=""
TARGET_FILE=""         # 相对 REPO_ROOT 的路径
REPORT_FILE=""         # 绝对路径
REPORT_TYPE=""
LOG_FILE=""
PUSH_OK="false"
SKIPPED_PUSHED="false"  # v1.4：skip-if-pushed 触发后置 true，让 trap 跳过 published 写入
ERROR_MSG=""
CHANNELS_RESULT="{}"
DOC_URL=""
TEST_TEXT=""
TEST_FILE=""

# ========== 加载 .secrets（注入 env vars） ==========
if [[ -f "$SECRETS_FILE" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$SECRETS_FILE"
    set +a
else
    echo "❌ .secrets 不存在: $SECRETS_FILE（v1.2 要求密钥走 env，channels.json 不再存明文）"
    exit 1
fi

# ========== 参数解析 ==========
TEST_MODE="false"
TEST_TARGETS=()
REPUSH="false"          # v1.4：强制重推
SKIP_IF_PUSHED="true"   # v1.4：默认已推过则跳过
DRY_RUN="false"         # v1.4：干跑
LIST_PUBLISHED="false"  # v1.4：列出已发布
LIST_PUBLISHED_TYPE="all"
BACKFILL_TYPE=""
BACKFILL_SINCE=""
BACKFILL_UNTIL=""
PUSHED_BY_OVERRIDE=""   # v1.4：手动指定 pushed_by
POSITIONAL=()
# 使用 while + index 以支持 --list-published [type] 形式
ARGS=("$@")
i=0
n=${#ARGS[@]}
while [[ $i -lt $n ]]; do
    arg="${ARGS[$i]}"
    case "$arg" in
        --test)        TEST_MODE="true" ;;
        --test=*)      TEST_MODE="true"; TEST_TARGETS+=("${arg#--test=}") ;;
        --repush)      REPUSH="true"; SKIP_IF_PUSHED="false" ;;
        --skip-if-pushed) SKIP_IF_PUSHED="true" ;;
        --dry-run)     DRY_RUN="true" ;;
        --list-published)
            LIST_PUBLISHED="true"
            # peek next arg as type
            next_idx=$((i+1))
            if [[ $next_idx -lt $n ]]; then
                next_arg="${ARGS[$next_idx]}"
                if [[ "$next_arg" != --* && "$next_arg" != -* ]]; then
                    LIST_PUBLISHED_TYPE="$next_arg"
                    i=$next_idx
                fi
            fi
            ;;
        --list-published=*) LIST_PUBLISHED="true"; LIST_PUBLISHED_TYPE="${arg#--list-published=}" ;;
        --backfill=*)  BACKFILL_TYPE="${arg#--backfill=}" ;;
        --backfill)
            next_idx=$((i+1))
            if [[ $next_idx -lt $n ]]; then
                BACKFILL_TYPE="${ARGS[$next_idx]}"
                i=$next_idx
            fi
            ;;
        --since=*)     BACKFILL_SINCE="${arg#--since=}" ;;
        --since)
            next_idx=$((i+1))
            if [[ $next_idx -lt $n ]]; then
                BACKFILL_SINCE="${ARGS[$next_idx]}"
                i=$next_idx
            fi
            ;;
        --until=*)     BACKFILL_UNTIL="${arg#--until=}" ;;
        --until)
            next_idx=$((i+1))
            if [[ $next_idx -lt $n ]]; then
                BACKFILL_UNTIL="${ARGS[$next_idx]}"
                i=$next_idx
            fi
            ;;
        --pushed-by=*) PUSHED_BY_OVERRIDE="${arg#--pushed-by=}" ;;
        --channels-config=*) CHANNELS_CONFIG="${arg#--channels-config=}" ;;
        --help|-h)
            cat <<EOF
用法:
  ./scripts/push_report.sh <report_file>                          # 推指定报告（默认 --skip-if-pushed）
  ./scripts/push_report.sh <report_file> --repush                 # 强制重推（覆盖记录）
  ./scripts/push_report.sh <report_file> --dry-run                # 干跑（落 _dryrun/）
  ./scripts/push_report.sh --test [<channel_name>]                # 推测试消息
  ./scripts/push_report.sh --list-published [daily|weekly|special|all]
  ./scripts/push_report.sh --backfill daily --since 2026-06-10 --until 2026-06-16
  ./scripts/push_report.sh --help

支持路径 → REPORT_TYPE:
  每日资讯/<date>.md         → daily
  每周汇总/<date>.md         → weekly
  专题/<name>.md             → special
  data/inspections/<file>.md → management
  test_messages/<file>.md    → test

v1.4 去重策略（默认）：
  - 已推过 → 静默跳过（EXIT 0）；不写 published 记录、不真推
  - 加 --repush → 强制重推（push_count+1，写新记录）
  - 加 --dry-run → 落档 _dryrun/（不入主索引）

环境变量:
  CHANNELS_CONFIG  渠道配置文件路径（默认 config/channels.json）
  SECRETS_FILE     .secrets 路径（默认 .secrets）
EOF
            exit 0
            ;;
        每日资讯/*|每周汇总/*|专题/*|data/inspections/*|test_messages/*) POSITIONAL+=("$arg") ;;
        *) echo "❌ 未知参数: $arg"; exit 1 ;;
    esac
    i=$((i+1))
done

# ========== v1.4 运维 CLI 早退（不进入推送主流程） ==========
if [[ "$LIST_PUBLISHED" == "true" ]]; then
    exec python3 "$SCRIPT_DIR/published_record.py" list "$LIST_PUBLISHED_TYPE"
fi
if [[ -n "$BACKFILL_TYPE" ]]; then
    if [[ -z "$BACKFILL_SINCE" || -z "$BACKFILL_UNTIL" ]]; then
        echo "❌ --backfill 需配合 --since / --until"
        exit 1
    fi
    # 补推区间内 daily 且 published/ 缺失的日期
    python3 - "$BACKFILL_TYPE" "$BACKFILL_SINCE" "$BACKFILL_UNTIL" "$REPUSH" <<'PYEOF'
import json, os, subprocess, sys
from datetime import date, timedelta
from pathlib import Path

rtype, since_s, until_s, repush = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4] == "true"
since = date.fromisoformat(since_s)
until = date.fromisoformat(until_s)
repo = Path("/app/data/所有对话/主对话/AI资讯追踪")

path_map = {"daily": "每日资讯", "weekly": "每周汇总", "special": "专题"}
date_map = {"daily": "%Y-%m-%d", "weekly": "%G-W%V", "special": None}
if rtype not in path_map:
    print(f"❌ 不支持的类型: {rtype}（仅 daily/weekly/special）")
    sys.exit(1)

d, plans = since, []
while d <= until:
    if rtype == "daily":
        label = d.strftime("%Y-%m-%d")
        rel = f"每日资讯/{label}.md"
        pub = repo / "data" / "published" / "daily" / f"{label}.json"
    elif rtype == "weekly":
        iy, iw, _ = d.isocalendar()
        label = f"{iy}-W{iw:02d}"
        rel = f"每周汇总/{label}.md"
        pub = repo / "data" / "published" / "weekly" / f"{label}.json"
    else:
        break  # special 不支持 backfill
    f = repo / rel
    if f.exists() and (not pub.exists() or repush):
        plans.append((rel, label))  # 相对路径：内层 push_report.sh 才能正确解析
    d += timedelta(days=1)

print(f"📋 待补推 {len(plans)} 份 {rtype}：")
for fp, lbl in plans:
    print(f"  - {lbl}: {fp}")
if not plans:
    print("（无）")
    sys.exit(0)
print()
for fp, lbl in plans:
    print(f"🚀 推 {lbl} ...")
    cmd = ["/app/data/所有对话/主对话/AI资讯追踪/scripts/push_report.sh", fp]
    if repush:
        cmd.append("--repush")
    r = subprocess.run(cmd, capture_output=True, text=True)
    print(r.stdout[-500:] if r.stdout else "")
    if r.returncode != 0:
        print(f"   ❌ exit={r.returncode}")
        if r.stderr:
            print(f"   stderr: {r.stderr[-300:]}")
PYEOF
    exit $?
fi

# ========== 计算 STARTED_AT + 准备日志目录 ==========
STARTED_AT=$(TZ=$TZ_LABEL date -Iseconds)
TODAY=$(TZ=$TZ_LABEL date '+%Y-%m-%d')
mkdir -p "$REPO_ROOT/logs/daily"
mkdir -p "$REPO_ROOT/logs/push"
# 日志文件名按 REPORT 日期（即 target_file 的日期）命名，与旧 push_daily_to_feishu.sh 保持一致
# 例：8:30 推 6-15 报告 → logs/daily/2026-06-15-push.jsonl
# 12:00/20:00 PM 巡检按"今天推送了哪份报告"反向查日志
if [[ -n "$TARGET_FILE" ]]; then
    REPORT_DATE_FOR_LOG=$(echo "$TARGET_FILE" | grep -oE '20[0-9]{2}-[0-1][0-9]-[0-3][0-9]' | head -1)
fi
REPORT_DATE_FOR_LOG="${REPORT_DATE_FOR_LOG:-$TODAY}"
LOG_FILE="$REPO_ROOT/logs/daily/${REPORT_DATE_FOR_LOG}-push.jsonl"

# ========== EXIT trap：无论成败都写 JSON 日志（追加模式） ==========
write_log() {
    local ended_at duration_sec
    ended_at=$(TZ=$TZ_LABEL date -Iseconds)
    duration_sec=$(( $(date -d "$ended_at" +%s) - $(date -d "$STARTED_AT" +%s) ))
    # TEST_MODE 没有真实 TARGET_FILE（test_messages/probe-...），用推送日期（兼容旧 log）
    local log_target="$TARGET_FILE"
    [[ "$TEST_MODE" == "true" ]] && log_target=""
    python3 -c "
import json, sys
log_file, started, ended, dur, ok, target, rtype, doc_url, channels_json, err, test_file = sys.argv[1:12]
try:
    channels = json.loads(channels_json)
except Exception:
    channels = {}
entry = {
    'schema_version': '$SCHEMA_VERSION',
    'started_at': started,
    'ended_at': ended,
    'duration_sec': dur,
    'target_file': target or None,
    'report_type': rtype,
    'ok': ok == 'true',
    'doc_url': doc_url or None,
    'test_file': test_file or None,
    'channels': channels,
    'error': err or None
}
with open(log_file, 'a', encoding='utf-8') as f:
    f.write(json.dumps(entry, ensure_ascii=False) + '\n')
" "$LOG_FILE" "$STARTED_AT" "$ended_at" "$duration_sec" "$PUSH_OK" \
  "$log_target" "$REPORT_TYPE" "$DOC_URL" "$CHANNELS_RESULT" "$ERROR_MSG" "$TEST_FILE" \
  2>/dev/null || true
    echo "📝 结构化日志已追加: $LOG_FILE"
}
trap 'write_log; write_published_record' EXIT

# ========== v1.4 发布记录（EXIT trap 第二步：写 data/published/<type>/<date>.json） ==========
write_published_record() {
    # skip-if-pushed 触发时不写 published（不算"新的推送事件"）
    if [[ "${SKIPPED_PUSHED:-false}" == "true" ]]; then
        echo "⏭️  skip-if-pushed 触发，跳过 published 写入"
        return 0
    fi
    # TEST_MODE / DRY_RUN(可选 true→_dryrun) / management 模式都写（仅 test 模式跳过；探针不算"资讯发布"）
    if [[ "$TEST_MODE" == "true" ]]; then
        return 0
    fi
    if [[ "$REPORT_TYPE" != "daily" && "$REPORT_TYPE" != "weekly" && "$REPORT_TYPE" != "special" && "$REPORT_TYPE" != "management" ]]; then
        return 0
    fi
    # DRY_RUN 模式：pushed-by=dry-run，落档 _dryrun/
    local dry_flag=""
    if [[ "$DRY_RUN" == "true" ]]; then
        dry_flag="--dry-run"
    fi
    # PUSHED_BY 自动检测：env PUSH_INVOKED_FROM 优先；否则 manual
    local pushed_by="${PUSHED_BY_OVERRIDE:-${PUSH_INVOKED_FROM:-manual}}"
    # v2.0 daily：传 daily JSON 路径给 published_record（启用 v2.0 模式 + 嵌套 publish_record）
    local daily_json_flag=""
    if [[ "$REPORT_TYPE" == "daily" && -n "${DAILY_V2_JSON:-}" && -f "$DAILY_V2_JSON" ]]; then
        daily_json_flag="--daily-json $DAILY_V2_JSON"
    fi
    python3 "$SCRIPT_DIR/published_record.py" record \
        --type "$REPORT_TYPE" \
        --file "$TARGET_FILE" \
        --channels "$CHANNELS_RESULT" \
        --doc-url "$DOC_URL" \
        --pushed-by "$pushed_by" \
        --ok "$PUSH_OK" \
        --error "${ERROR_MSG:-}" \
        $daily_json_flag \
        $dry_flag \
        2>&1 | tail -5 || true
}

# ========== TEST_MODE 处理 ==========
if [[ "$TEST_MODE" == "true" ]]; then
    REPORT_TYPE="test"
    TS_COMPACT=$(TZ=$TZ_LABEL date '+%Y%m%d-%H%M%S')
    TS_HUMAN=$(TZ=$TZ_LABEL date '+%Y-%m-%d %H:%M:%S')

    # 落档测试消息（沙箱无 /var/log 权限则退 $HOME/.local/share/）
    TEST_LOG_DIR="/var/log/ai-news/test_messages"
    if ! mkdir -p "$TEST_LOG_DIR" 2>/dev/null; then
        TEST_LOG_DIR="$HOME/.local/share/ai-news/test_messages"
        mkdir -p "$TEST_LOG_DIR"
    fi
    TEST_FILE="$TEST_LOG_DIR/${TS_COMPACT}-probe.md"
    TARGET_FILE="test_messages/probe-${TS_COMPACT}.md"

    if [[ ${#TEST_TARGETS[@]} -gt 0 ]]; then
        TARGETS_DESC="${TEST_TARGETS[*]}"
    else
        TARGETS_DESC="all enabled with purpose=test"
    fi
    TEST_TEXT="🔍 AI资讯渠道探针 ${TS_HUMAN} — webhook 心跳测试（请忽略）"

    cat > "$TEST_FILE" <<EOF
# AI 资讯·渠道探针测试消息

- 时间: ${TS_HUMAN} ${TZ_LABEL}
- 触发: 探活（probe）
- 目标渠道: ${TARGETS_DESC}
- 内容: ${TEST_TEXT}
- 注意: 本文件不入 git 库；正式群（含 official）也会收到，但 message_types=test 默认不污染正式消息
EOF
    echo "📝 测试消息已落档: $TEST_FILE"

    # 加载 enabled + purpose 含 "test" + message_types 含 "test" 的渠道
    if [[ ${#TEST_TARGETS[@]} -gt 0 ]]; then
        ENABLED_CHANNELS=("${TEST_TARGETS[@]}")
    else
        mapfile -t ENABLED_CHANNELS < <(python3 -c "
import json
cfg = json.load(open('$CHANNELS_CONFIG'))
result = []
for name, c in cfg.get('channels', {}).items():
    if not c.get('enabled'):
        continue
    purposes = c.get('purpose', [])
    types = c.get('message_types', [])
    if 'test' in purposes and 'test' in types:
        result.append(name)
for n in result: print(n)
")
    fi
    if [[ ${#ENABLED_CHANNELS[@]} -eq 0 ]]; then
        ERROR_MSG="没有 enabled 且 purpose/message_types 含 'test' 的渠道"
        echo "❌ $ERROR_MSG"
        exit 1
    fi
else
    # ========== 正常模式：检查产物 + 推断 REPORT_TYPE ==========
    if [[ ${#POSITIONAL[@]} -eq 0 ]]; then
        # 无参数 → 默认推昨日 每日资讯（兼容旧 push_daily_to_feishu.sh 行为）
        YESTERDAY=$(TZ=$TZ_LABEL date -d "yesterday" +%Y-%m-%d)
        TARGET_FILE="每日资讯/${YESTERDAY}.md"
    else
        TARGET_FILE="${POSITIONAL[0]}"
    fi
    REPORT_FILE="$REPO_ROOT/$TARGET_FILE"
    if [[ ! -f "$REPORT_FILE" ]]; then
        ERROR_MSG="报告不存在: $REPORT_FILE（无参数默认推昨日；指定参数：./scripts/push_report.sh <report_file>）"
        echo "❌ $ERROR_MSG"
        exit 1
    fi
    echo "✅ 报告已就绪: $REPORT_FILE ($(wc -c < "$REPORT_FILE") bytes)"

    case "$TARGET_FILE" in
        每日资讯/*)         REPORT_TYPE="daily" ;;
        每周汇总/*)         REPORT_TYPE="weekly" ;;
        专题/*)             REPORT_TYPE="special" ;;
        data/inspections/*) REPORT_TYPE="management" ;;
        test_messages/*)    REPORT_TYPE="test" ;;
        *)                  REPORT_TYPE="unknown"
                            ERROR_MSG="未知报告路径: $TARGET_FILE（支持：每日资讯/ 每周汇总/ 专题/ data/inspections/ test_messages/）"
                            echo "❌ $ERROR_MSG"
                            exit 1
                            ;;
    esac
    echo "🏷️  报告类型: $REPORT_TYPE"

    # ========== v1.4 skip-if-pushed 检查 ==========
    if [[ "$SKIP_IF_PUSHED" == "true" && "$REPUSH" == "false" && ("$REPORT_TYPE" == "daily" || "$REPORT_TYPE" == "weekly" || "$REPORT_TYPE" == "special") ]]; then
        DERIVED_DATE=$(python3 -c "
import re, sys, os
p = '$TARGET_FILE'
m = re.search(r'(\\d{4}-\\d{2}-\\d{2})', p) or re.search(r'(\\d{4}-W\\d{2})', p)
if m: print(m.group(1))
else: print(os.path.splitext(os.path.basename(p))[0])
")
        PUB_REC="$REPO_ROOT/data/published/$REPORT_TYPE/${DERIVED_DATE}.json"
        if [[ -f "$PUB_REC" ]]; then
            PREV_OK=$(python3 -c "import json; print(json.load(open('$PUB_REC')).get('ok', False))" 2>/dev/null || echo "False")
            if [[ "$PREV_OK" == "True" ]]; then
                PREV_COUNT=$(python3 -c "import json; print(json.load(open('$PUB_REC')).get('push_count', 0))" 2>/dev/null || echo "0")
                echo "⏭️  已推过（ok=true, push_count=${PREV_COUNT}）：$PUB_REC"
                echo "   用 --repush 强制重推，或 --dry-run 干跑"
                # 设置标志让 EXIT trap 跳过 published 写入（但 jsonl 日志仍记一笔 "skipped"）
                SKIPPED_PUSHED="true"
                PUSH_OK="true"
                CHANNELS_RESULT='{"skipped":true,"reason":"already_pushed","prev_record":"'$PUB_REC'"}'
                exit 0
            fi
        fi
    fi

    # normal mode：按 REPORT 日期作 log 文件名（8:30 推 6-15 报告 → logs/daily/2026-06-15-push.jsonl）
    # v1.5 兼容 weekly 文件名（YYYY-Www 格式），grep 同时匹配 ISO 周；用 || true 兜底防止 set -e + pipefail 误退
    REPORT_DATE_FOR_LOG=$(echo "$TARGET_FILE" | grep -oE '20[0-9]{2}(-[0-1][0-9]-[0-3][0-9]|-W[0-9]{2})' | head -1 || true)
    REPORT_DATE_FOR_LOG="${REPORT_DATE_FOR_LOG:-$TODAY}"
    LOG_FILE="$REPO_ROOT/logs/daily/${REPORT_DATE_FOR_LOG}-push.jsonl"

    # 加载 enabled + message_types 含 REPORT_TYPE + purpose 含"对应身份" 的渠道
    # 隐含 purpose 映射（per 6-16 12:20 设计）：
    #   daily/weekly/special → "official"（正式资讯消息）
    #   management            → "management"（管理性消息）
    #   test                  → "test"（测试/探活）
    # 渠道匹配条件：message_types 含 rtype AND purpose 含 rtype 对应身份
    mapfile -t ENABLED_CHANNELS < <(python3 -c "
import json
cfg = json.load(open('$CHANNELS_CONFIG'))
rtype = '$REPORT_TYPE'
# 隐含 purpose 映射
implied_purpose = {
    'daily': 'official',
    'weekly': 'official',
    'special': 'official',
    'management': 'management',
    'test': 'test',
}
need_purpose = implied_purpose.get(rtype, rtype)
result = []
for name, c in cfg.get('channels', {}).items():
    if not c.get('enabled'):
        continue
    purposes = c.get('purpose', [])
    types = c.get('message_types', [])
    if (rtype in types or 'all' in types) and need_purpose in purposes:
        result.append(name)
for n in result: print(n)
")
    if [[ ${#ENABLED_CHANNELS[@]} -eq 0 ]]; then
        ERROR_MSG="没有任何渠道匹配 REPORT_TYPE='$REPORT_TYPE'（检查 $CHANNELS_CONFIG：channel.enabled + purpose + message_types 至少需各含 '$REPORT_TYPE'）"
        echo "❌ $ERROR_MSG"
        exit 1
    fi
fi
echo "📡 启用渠道: ${ENABLED_CHANNELS[*]} (REPORT_TYPE=$REPORT_TYPE)"

# 估算推送条数（仅 normal mode + daily/weekly/special）
if [[ "$TEST_MODE" != "true" && -n "$REPORT_FILE" && -f "$REPORT_FILE" ]]; then
    ITEMS_PUSHED=$(grep -cE '(\*\*重要程度\*\*|\*\*[🔴🟡🟢🔵]\*\*)' "$REPORT_FILE" 2>/dev/null || echo "0")
fi

# ========== 处理 docx（仅 daily/weekly/special） ==========
NEED_DOCX="false"
if [[ "$REPORT_TYPE" == "daily" || "$REPORT_TYPE" == "weekly" || "$REPORT_TYPE" == "special" ]]; then
    NEED_DOCX="true"
fi

# v2.0 daily：定位 daily JSON 路径（每日资讯/2026-06-15.md → data/published/daily/2026-06-15.json）
# ⚠️ 必须在 dry-run 早退点之前计算（dry-run preview 也需要）
DAILY_V2_JSON=""
if [[ "$REPORT_TYPE" == "daily" && "$TARGET_FILE" == *每日资讯* ]]; then
    DAILY_DATE=$(basename "$TARGET_FILE" .md)
    DAILY_V2_JSON="$REPO_ROOT/data/published/daily/${DAILY_DATE}.json"
fi
# 透传给内嵌 python3（通过 env var 传递绝对路径）
export DAILY_V2_JSON

# v1.4 dry-run 早退点：必须在 docx 创建之前，否则会污染正式群文档库
if [[ "$DRY_RUN" == "true" && "$TEST_MODE" != "true" ]]; then
    echo ""
    echo "==================================="
    echo "🧪 DRY-RUN 模式：跳过 docx + webhook 推送（落档 _dryrun/）"
    echo "==================================="
    DOC_URL=""

    # v2.0 daily preview：dry-run 也调 renderer 展示"会推送什么"（不实际发送）
    if [[ -n "$DAILY_V2_JSON" && -f "$DAILY_V2_JSON" ]]; then
        echo ""
        echo "🔍 v2.0 daily push 预览（不实际发送）:"
        DAIRY_PREVIEW=$(DAILY_V2_JSON="$DAILY_V2_JSON" python3 2>&1 - "$REPO_ROOT" "每日资讯/dry-run-preview" << 'PYEOF'
import json, sys, os
repo_root = sys.argv[1]
daily_path = os.environ.get("DAILY_V2_JSON", "")
if not daily_path or not os.path.exists(daily_path):
    print("   (no daily v2.0 JSON)")
    sys.exit(0)
daily = json.load(open(daily_path, encoding="utf-8"))
# 飞书 webhook text（plain）
date_iso = daily.get("report_date", "")
one_line = daily.get("one_line_summary", {}).get("text", "")
doc_url = daily.get("publish_record", {}).get("doc_url") or "(doc_url 尚未生成)"
feishu_text = f"📰 AI资讯日报 · {date_iso}\n\n{one_line}\n\n完整报告：{doc_url}" if one_line else f"📰 AI资讯日报 · {date_iso}\n\n完整报告：{doc_url}"
print(f"   [feishu text] ({len(feishu_text)} char):")
for line in feishu_text.split("\n"):
    print(f"     {line}")
# 企微 markdown 调 renderer
sys.path.insert(0, os.path.join(repo_root, "scripts"))
from renderers import render_wecom_markdown
wecom_md = render_wecom_markdown(daily)
print(f"\n   [wecom markdown via render_wecom_markdown] ({len(wecom_md)} char):")
for line in wecom_md.split("\n"):
    print(f"     {line}")
PYEOF
)
        echo "$DAIRY_PREVIEW"
    fi

    CHANNELS_RESULT='{"feishu":{"ok":true,"code":0,"msg":"dry-run-simulated"},"wecom_official":{"ok":true,"code":0,"msg":"dry-run-simulated"}}'
    PUSH_OK="true"
    LOG_FILE="$REPO_ROOT/logs/daily/${REPORT_DATE_FOR_LOG:-$TODAY}-push.jsonl"
    exit 0
fi

if [[ "$NEED_DOCX" == "true" ]]; then
    echo "📄 创建飞书云文档..."
    # lark-cli --content 要求相对当前目录的相对路径
    REL_REPORT_FILE="$TARGET_FILE"
    DOC_OUTPUT=$(LARK_CLI_NO_PROXY=1 $LARK_CLI docs +create \
        --as bot \
        --doc-format markdown \
        --content "@$REL_REPORT_FILE" 2>&1)
    echo "   $(echo "$DOC_OUTPUT" | head -1)"

    DOC_URL=$(echo "$DOC_OUTPUT" | python3 -c "
import sys, json
raw = sys.stdin.read()
try:
    d = json.loads(raw)
    if d.get('ok') and 'data' in d and 'document' in d['data']:
        print(d['data']['document']['url'])
        sys.exit(0)
except Exception:
    pass
sys.exit(1)
" 2>/dev/null) || DOC_URL=""

    if [[ -z "$DOC_URL" ]]; then
        ERROR_MSG="创建飞书云文档失败：${DOC_OUTPUT:0:200}"
        echo "❌ $ERROR_MSG"
        exit 1
    fi
    echo "✅ 云文档已创建: $DOC_URL"

    DOC_ID=$(echo "$DOC_OUTPUT" | python3 -c "
import sys, json
try:
    d = json.loads(sys.stdin.read())
    if d.get('ok') and 'data' in d and 'document' in d['data']:
        print(d['data']['document'].get('document_id', ''))
except Exception:
    pass
" 2>/dev/null) || DOC_ID=""

    if [[ -n "$DOC_ID" ]]; then
        echo "🌐 设置公开访问..."
        PERM_OUTPUT=$(LARK_CLI_NO_PROXY=1 $LARK_CLI drive permission.public patch \
            --as bot --yes \
            --params "{\"token\":\"$DOC_ID\",\"type\":\"docx\"}" \
            --data '{"external_access":true,"security_entity":"anyone_can_view","comment_entity":"anyone_can_view","share_entity":"anyone","link_share_entity":"anyone_readable","invite_external":true}' 2>&1)
        PERM_CODE=$(echo "$PERM_OUTPUT" | python3 -c "import sys, json; d=json.loads(sys.stdin.read()); print(d.get('code', -1))" 2>/dev/null || echo "-1")
        if [[ "$PERM_CODE" == "0" ]]; then
            echo "✅ 公开访问已开启（link_share_entity=anyone_readable）"
        else
            echo "⚠️  公开访问设置失败（code=$PERM_CODE），文档保持私有不影响推送"
        fi
    fi
fi

# ========== 推送（python3 内部 try/except，单渠道失败不阻塞） ==========
echo ""
echo "==================================="
echo "🚀 开始推送 ${REPORT_TYPE} → ${TARGET_FILE}"
echo "==================================="

# 提醒 DAILY_V2_JSON 状态（实际计算在 docx 处理前已完成）
if [[ -n "$DAILY_V2_JSON" && -f "$DAILY_V2_JSON" ]]; then
    echo "📦 daily v2.0 JSON: $DAILY_V2_JSON"
elif [[ "$REPORT_TYPE" == "daily" ]]; then
    echo "⚠️  daily v2.0 JSON 不存在 → 回退 v1.0 stub"
fi

PY_OUTPUT=$(DAILY_V2_JSON="$DAILY_V2_JSON" python3 2>&1 - "$REPO_ROOT" "$TARGET_FILE" "$REPORT_TYPE" "$DOC_URL" "$CHANNELS_CONFIG" "$TEST_TEXT" "${ENABLED_CHANNELS[@]}" << 'PYEOF'
import json, sys, hmac, hashlib, base64, time, subprocess, os, re
from datetime import date, timedelta
from pathlib import Path

repo_root, target_file, rtype, doc_url, cfg_path, test_text = sys.argv[1:7]
enabled = sys.argv[7:]

# v2.0 daily JSON（透传自 bash env DAILY_V2_JSON；推送时读 JSON 调 renderers）
_daily_v2_json_path = os.environ.get("DAILY_V2_JSON", "")
daily_v2 = None
if _daily_v2_json_path and os.path.exists(_daily_v2_json_path):
    try:
        with open(_daily_v2_json_path, encoding="utf-8") as f:
            daily_v2 = json.load(f)
    except Exception as e:
        print(f"⚠️  daily v2.0 JSON 解析失败: {e}（回退 v1.0 stub）", flush=True)
        daily_v2 = None

def curl_post(url, payload):
    return subprocess.run(
        ["curl", "-sS", "--noproxy", "*", "-X", "POST", url,
         "-H", "Content-Type: application/json",
         "-d", json.dumps(payload, ensure_ascii=False)],
        capture_output=True, text=True, timeout=15
    ).stdout

def get_env(name, channel_name):
    val = os.environ.get(name)
    if not val:
        raise ValueError(f"env var '{name}' not set for channel '{channel_name}' (check .secrets)")
    return val

def _build_weekly_text(target_file, doc_url, mode):
    """v1.5 周报推送文本：标题 + 一句话总结 + 每日资讯链接表 + 完整周报链接。
    mode: 'feishu' (text, ≤2000 char) | 'wecom' (markdown, ≤4096 char)
    数据源：
      - 每周汇总/<week>.md：标题 / 一句话总结 / 汇总周期（确定 7 天）
      - data/published/daily/<date>.json：每日的 doc_url（v1.4 发布记录）
    """
    abs_md = os.path.join(repo_root, target_file)
    md_text = open(abs_md, encoding="utf-8").read() if os.path.exists(abs_md) else ""
    basename = target_file.split("/")[-1].replace(".md", "")  # e.g. "2026-W25"
    # 提取"本周一句话总结"（blockquote 行）
    summary = ""
    for line in md_text.splitlines():
        if "**本周一句话总结**" in line:
            summary = line.split("**本周一句话总结**", 1)[-1].lstrip("：: ").strip()
            break
    # 提取周对应 7 天日期（从"汇总周期"行找 "YYYY-MM-DD ~ YYYY-MM-DD"）
    monday = sunday = None
    for line in md_text.splitlines():
        m = re.search(r"(\d{4}-\d{2}-\d{2})\s*~\s*(\d{4}-\d{2}-\d{2})", line)
        if m:
            monday = date.fromisoformat(m.group(1))
            sunday = date.fromisoformat(m.group(2))
            break
    # 读 published/daily 7 天 doc_url
    daily_links = []
    if monday and sunday:
        d = monday
        while d <= sunday:
            pub = Path(repo_root) / "data" / "published" / "daily" / f"{d.isoformat()}.json"
            if pub.exists():
                try:
                    rec = json.loads(pub.read_text(encoding="utf-8"))
                    url = rec.get("doc_url")
                    if url and rec.get("ok", False):
                        daily_links.append((d, url))
                except Exception:
                    pass
            d += timedelta(days=1)
    # 拼接推送文本
    if mode == "feishu":
        parts = [f"📊 AI资讯周报 · {basename}"]
        if summary:
            parts.append(summary)
        if daily_links:
            parts.append("📅 本周日报：")
            for d, url in daily_links:
                parts.append(f"• {d.strftime('%m-%d')} · {url}")
        else:
            parts.append("（本周日报 doc 链接未生成）")
        parts.append(f"完整周报：{doc_url}")
        return "\n\n".join(parts)[:2000]
    else:  # wecom markdown
        parts = [f"📊 **AI资讯周报** · {basename}"]
        if summary:
            parts.append(summary)
        if daily_links:
            parts.append("📅 **本周日报**：")
            for d, url in daily_links:
                parts.append(f"• {d.strftime('%m-%d')} · [日报链接]({url})")
        else:
            parts.append("（本周日报 doc 链接未生成）")
        parts.append(f"👉 [完整周报]({doc_url})")
        return "\n\n".join(parts)[:4000]

def push_feishu(ch_cfg, channel_name):
    if rtype == "test":
        text = test_text
    elif rtype == "management":
        abs_path = os.path.join(repo_root, target_file)
        with open(abs_path) as f:
            text = f.read()[:2000]   # 飞书 text 上限 2000 字符
        # 必须含 "AI资讯" 校验词（per feishu webhook 配置）
        if "AI资讯" not in text:
            text = "【AI资讯·管理消息】\n" + text
    else:
        # daily/weekly/special
        basename = target_file.split("/")[-1].replace(".md", "")
        if "每日资讯" in target_file:
            if daily_v2 is not None:
                # v2.0：用 daily JSON 的 one_line_summary 增强（plain text，飞书 webhook 不支持 markdown）
                date_iso = daily_v2.get("report_date", basename)
                one_line = daily_v2.get("one_line_summary", {}).get("text", "")
                if one_line:
                    text = f"📰 AI资讯日报 · {date_iso}\n\n{one_line}\n\n完整报告：{doc_url}"
                else:
                    text = f"📰 AI资讯日报 · {date_iso}\n\n完整报告：{doc_url}"
            else:
                text = f"📰 AI资讯日报 · {basename}\n\n完整报告：{doc_url}"
        elif "每周汇总" in target_file:
            text = _build_weekly_text(target_file, doc_url, "feishu")
        elif "专题" in target_file:
            text = f"📚 AI资讯专题 · {basename}\n\n完整报告：{doc_url}"
        else:
            text = f"📰 AI资讯 · {target_file}\n\n{doc_url or ''}"

    kw = ch_cfg.get("keyword", "")
    if kw and kw not in text:
        return {"ok": False, "code": -1, "msg": f"keyword '{kw}' missing in text"}

    ts = str(int(time.time()))
    webhook_url = get_env(ch_cfg["webhook_url_env"], channel_name)
    if ch_cfg.get("webhook_secret_env"):
        secret = get_env(ch_cfg["webhook_secret_env"], channel_name)
        key = f"{ts}\n{secret}".encode("utf-8")
        sign = base64.b64encode(hmac.new(key, b"", hashlib.sha256).digest()).decode("utf-8")
    else:
        sign = ""

    payload = {"timestamp": ts, "sign": sign, "msg_type": "text", "content": {"text": text}}
    resp = curl_post(webhook_url, payload)
    try:
        d = json.loads(resp)
        return {"ok": d.get("code") == 0, "code": d.get("code", -1), "msg": d.get("msg", "")}
    except Exception as e:
        return {"ok": False, "code": -1, "msg": f"parse err: {e}; resp={resp[:200]}"}

def push_wecom(ch_cfg, channel_name):
    webhook_url = get_env(ch_cfg["webhook_url_env"], channel_name)
    if rtype == "test":
        content = test_text
    elif rtype == "management":
        abs_path = os.path.join(repo_root, target_file)
        with open(abs_path) as f:
            content = f.read()[:2000]
    else:
        basename = target_file.split("/")[-1].replace(".md", "")
        if "每日资讯" in target_file:
            if daily_v2 is not None:
                # v2.0：调 render_wecom_markdown（方案 B：简短摘要 + 飞书公开链接，≤500 字符）
                sys.path.insert(0, os.path.join(repo_root, "scripts"))
                from renderers import render_wecom_markdown
                content = render_wecom_markdown(daily_v2)
            else:
                content = f"📰 **AI资讯日报** · {basename}\n\n[👉 完整报告]({doc_url})"
        elif "每周汇总" in target_file:
            content = _build_weekly_text(target_file, doc_url, "wecom")
        elif "专题" in target_file:
            content = f"📚 **AI资讯专题** · {basename}\n\n[👉 完整报告]({doc_url})"
        else:
            content = f"📰 **AI资讯** · {target_file}\n\n{doc_url or ''}"

    payload = {"msgtype": "markdown", "markdown": {"content": content}}
    resp = curl_post(webhook_url, payload)
    try:
        d = json.loads(resp)
        return {"ok": d.get("errcode") == 0, "code": d.get("errcode", -1), "msg": d.get("errmsg", "")}
    except Exception as e:
        return {"ok": False, "code": -1, "msg": f"parse err: {e}; resp={resp[:200]}"}

PUSHERS = {"feishu": push_feishu, "wecom": push_wecom}

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
        results[name] = {"ok": False, "code": -1, "msg": f"no pusher for type '{ch_type}' (PUSHERS dict 未注册)"}
        print(f"   ❌ {results[name]['msg']}", flush=True)
        continue
    try:
        res = pusher(ch_cfg, name)
    except Exception as e:
        res = {"ok": False, "code": -1, "msg": f"pusher exception: {e}"}
    results[name] = res
    status = "✅" if res["ok"] else "❌"
    print(f"   {status} code={res['code']} msg={res['msg']}", flush=True)
    if res["ok"]:
        any_ok = True

print(f"\n📊 汇总: {json.dumps(results, ensure_ascii=False)}", flush=True)
print(f"PUSH_OK_BOOL::{'true' if any_ok else 'false'}")
print(f"CHANNELS_JSON::{json.dumps(results, ensure_ascii=False)}")
PYEOF
)

# 把 python3 内部日志（非协议行）显示给用户
echo "$PY_OUTPUT" | grep -v -E '^(PUSH_OK_BOOL|CHANNELS_JSON)::'

# 解析 python3 输出
PUSH_OK=$(echo "$PY_OUTPUT" | grep '^PUSH_OK_BOOL::' | sed 's/^PUSH_OK_BOOL:://' | head -1)
CHANNELS_RESULT=$(echo "$PY_OUTPUT" | grep '^CHANNELS_JSON::' | sed 's/^CHANNELS_JSON:://' | head -1)
[[ -z "$CHANNELS_RESULT" ]] && CHANNELS_RESULT="{}"
[[ -z "$PUSH_OK" ]] && PUSH_OK="false"

echo "==================================="
echo "📊 最终: PUSH_OK=$PUSH_OK / 渠道数=${#ENABLED_CHANNELS[@]}"
echo "==================================="

if [[ "$PUSH_OK" == "true" ]]; then
    exit 0
else
    ERROR_MSG="所有渠道都失败：$CHANNELS_RESULT"
    echo "❌ $ERROR_MSG"
    exit 1
fi
               