#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
generate_inspection.py - 生成 PM 日报（v1.3 推送架构·management 消息源·8 维度）

v1.3 升级（2026-06-16 21:52 主人拍板 Q12-Q14）：
- 8 节模板（抓取/推送/探针/累计异常/资源/待办/日历/结论）
- 飞书端：摘要 < feishu_max_chars（结论 + 异常项 + 待办摘要 + 落档路径）
- 落档：详细报告 data/inspections/<date>-<HH-MM>.md
- 异常升级：连续 N 天 0 条 / 日历漏触发 / schema 失效 → owner 私聊

调用：
  ./scripts/generators/generate_inspection.py 12:00
  ./scripts/generators/generate_inspection.py 20:00
  ./scripts/generators/generate_inspection.py 12:00 --no-write       # 调试：只输出不落档
  ./scripts/generators/generate_inspection.py 12:00 --no-escalate    # 调试：跳过 owner 私聊

依赖：
  - .secrets 加载通过调用方保证（push_report.sh / pm_inspect.sh 已 set -a source）
  - config/inspector.yaml 阈值配置
  - lark-cli 已在 PATH（升级 hook 用 im +message-send）
"""
import json
import os
import re
import subprocess
import sys
import shutil
from datetime import datetime, timezone, timedelta
from pathlib import Path

import yaml

TZ_SH = timezone(timedelta(hours=8))
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
INSPECTIONS_DIR = REPO_ROOT / "data" / "inspections"
LOGS_DAILY_DIR = REPO_ROOT / "logs" / "daily"
LOGS_DIR = REPO_ROOT / "logs"
DAILY_DIR = REPO_ROOT / "每日资讯"
PUBLISHED_DIR = REPO_ROOT / "data" / "published"  # v1.4
INSPECTOR_CONFIG = REPO_ROOT / "config" / "inspector.yaml"
ESCALATE_DEDUP_FILE = REPO_ROOT / "logs" / "inspections" / "escalate_dedup.json"
# recent_memory 在 workspace（/app/data/所有对话/主对话/），不在 git 仓库里
WORKSPACE_ROOT = SCRIPT_DIR.parent.parent.parent
RECENT_MEMORY = WORKSPACE_ROOT / "recent_memory" / "decision"

# ========== 加载阈值配置 ==========
def load_config() -> dict:
    if not INSPECTOR_CONFIG.exists():
        return {
            "consecutive_zero_days": 2,
            "lookback_days": 7,
            "disk_warn_pct": 80,
            "push_log_warn_bytes": 10485760,
            "escalate_on": ["consecutive_zero", "calendar_missed", "schema_invalid"],
            "owner_openid": "ou_d61bd0e15a001066cac4a97cace649f8",
            "escalate_dedup_hours": 24,
            "feishu_max_chars": 1800,
        }
    return yaml.safe_load(INSPECTOR_CONFIG.read_text(encoding="utf-8"))


CFG = load_config()
FEISHU_MAX = CFG.get("feishu_max_chars", 1800)
CONSECUTIVE_ZERO_DAYS = CFG.get("consecutive_zero_days", 2)
LOOKBACK_DAYS = CFG.get("lookback_days", 7)
PUBLISH_LOOKBACK_DAYS = CFG.get("publish_lookback_days", 7)  # v1.4：发布维度回溯天数
DISK_WARN_PCT = CFG.get("disk_warn_pct", 80)
PUSH_LOG_WARN_BYTES = CFG.get("push_log_warn_bytes", 10485760)
ESCALATE_ON = CFG.get("escalate_on", [])
OWNER_OPENID = CFG.get("owner_openid", "")
ESCALATE_DEDUP_HOURS = CFG.get("escalate_dedup_hours", 24)


# ========== 工具函数 ==========
def now_sh() -> datetime:
    return datetime.now(TZ_SH)


def sh(cmd: str, cwd: Path = REPO_ROOT) -> str:
    """执行 shell 命令，返回 stdout（去尾换行）"""
    try:
        r = subprocess.run(
            cmd, shell=True, cwd=cwd,
            capture_output=True, text=True, timeout=10,
            env={**os.environ, "TZ": "Asia/Shanghai"},
        )
        return r.stdout.strip()
    except Exception as e:
        return f"ERR: {e}"


def get_yesterday() -> str:
    return (now_sh() - timedelta(days=1)).strftime("%Y-%m-%d")


def get_today() -> str:
    return now_sh().strftime("%Y-%m-%d")


def get_date_n_days_ago(n: int) -> str:
    return (now_sh() - timedelta(days=n)).strftime("%Y-%m-%d")


def truncate(s: str, max_chars: int) -> str:
    """按字符数截断（不是字节数）"""
    if len(s) <= max_chars:
        return s
    return s[: max_chars - 20] + "\n…(已截断)\n"


# ========== 数据采集：原有 4 个（v1.2 沿用） ==========
def collect_raw_counts(date_str: str) -> dict:
    """data/raw/<source>/<date>.json 条数；schema 失效源标 -1"""
    raw_dir = REPO_ROOT / "data" / "raw"
    result = {}
    if not raw_dir.exists():
        return result
    for source in sorted(os.listdir(raw_dir)):
        date_file = raw_dir / source / f"{date_str}.json"
        if not date_file.exists():
            continue
        try:
            data = json.loads(date_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                if "count" in data and isinstance(data["count"], int):
                    result[source] = data["count"]
                elif "items" in data and isinstance(data["items"], list):
                    result[source] = len(data["items"])
                else:
                    result[source] = -1   # 结构未知
            elif isinstance(data, list):
                result[source] = len(data)
            else:
                result[source] = 1
        except Exception:
            result[source] = -1   # 解析失败
    return result


def collect_daily_report(date_str: str) -> dict:
    f = DAILY_DIR / f"{date_str}.md"
    if not f.exists():
        return {"exists": False}
    try:
        content = f.read_text(encoding="utf-8")
        items = len(re.findall(r"\*\*重要程度\*\*|\*\*[🔴🟡🟢🔵]\*\*", content))
        return {
            "exists": True,
            "items": items,
            "size": len(content.encode("utf-8")),
        }
    except Exception as e:
        return {"exists": False, "error": str(e)[:100]}


def collect_push_status(report_date: str) -> dict:
    for ext in ("jsonl", "json"):
        f = LOGS_DAILY_DIR / f"{report_date}-push.{ext}"
        if not f.exists():
            continue
        try:
            if ext == "jsonl":
                entries = [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]
                if not entries:
                    return {"log_exists": True, "entries": 0, "format": ext}
                last = entries[-1]
            else:
                last = json.loads(f.read_text(encoding="utf-8"))
            return {
                "log_exists": True,
                "entries": 1,
                "format": ext,
                "last_ok": last.get("ok"),
                "last_target": last.get("target_file") or last.get("outputs", {}).get("report_path"),
                "last_doc_url": last.get("doc_url") or last.get("outputs", {}).get("doc_url"),
                "last_channels": last.get("channels", {}),
                "last_error": last.get("error"),
            }
        except Exception as e:
            return {"log_exists": True, "error": str(e)[:200]}
    return {"log_exists": False}


def collect_git_status() -> dict:
    try:
        last = sh("git log -1 --format='%h %s' HEAD")
        unpushed = sh("git log origin/main..HEAD --oneline 2>&1")
        return {"last_commit": last[:120], "unpushed": unpushed[:200] or "(无)"}
    except Exception as e:
        return {"error": str(e)[:200]}


# ========== 数据采集：v1.3 新增 4 个 ==========
def collect_consecutive_failures() -> list:
    """扫最近 lookback_days 天 raw/<src>/<date>.json，输出连续 0 条或 schema 失效的源
       返回: [{source, type, days, dates}, ...]（type: zero | schema_invalid）
    """
    raw_dir = REPO_ROOT / "data" / "raw"
    if not raw_dir.exists():
        return []
    sources = [d for d in os.listdir(raw_dir) if (raw_dir / d).is_dir()]
    failures = []
    for src in sorted(sources):
        zero_streak = 0
        zero_dates = []
        invalid_dates = []
        for n in range(1, LOOKBACK_DAYS + 1):  # 从 1 天前（昨天）往前
            d = get_date_n_days_ago(n)
            f = raw_dir / src / f"{d}.json"
            if not f.exists():
                break
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    cnt = data.get("count")
                    if isinstance(cnt, int) and cnt == 0:
                        zero_streak += 1
                        zero_dates.append(d)
                    elif isinstance(cnt, int) and cnt > 0:
                        break  # 中断
                    else:
                        invalid_dates.append(d)
                        break  # schema 失效
                elif isinstance(data, list) and len(data) == 0:
                    zero_streak += 1
                    zero_dates.append(d)
                else:
                    break
            except Exception:
                invalid_dates.append(d)
                break
        if zero_streak >= CONSECUTIVE_ZERO_DAYS:
            failures.append({
                "source": src,
                "type": "zero",
                "days": zero_streak,
                "dates": zero_dates,
            })
        if invalid_dates:
            failures.append({
                "source": src,
                "type": "schema_invalid",
                "days": len(invalid_dates),
                "dates": invalid_dates,
            })
    return failures


def collect_probe_status() -> dict:
    """探针 v0 进度（2/5：L1 bash + L3 calendar 已落地；L2 异常升级本次实现中）"""
    l1 = "✅ 已落地（probe.py 探活 + DM owner）"
    l2 = "⏸️ 立项中（本次 v1.3 升级中实现）"
    l3 = "✅ 已落地（calendar 3 event 改造）"
    return {
        "version": "v0 进度 2/5",
        "L1_bash": l1,
        "L2_escalation": l2,
        "L3_calendar": l3,
    }


def collect_resource_usage() -> dict:
    """磁盘 + 推送日志 + raw 体积"""
    disk = {}
    try:
        out = sh("df -P / | tail -1")
        parts = out.split()
        if len(parts) >= 5:
            disk = {
                "total": parts[1],
                "used": parts[2],
                "avail": parts[3],
                "use_pct": parts[4].rstrip("%"),
            }
    except Exception:
        pass

    push_log_size = 0
    push_log_files = 0
    if LOGS_DAILY_DIR.exists():
        for f in LOGS_DAILY_DIR.glob("*-push.*"):
            push_log_size += f.stat().st_size
            push_log_files += 1

    raw_size = 0
    raw_files = 0
    raw_dir = REPO_ROOT / "data" / "raw"
    if raw_dir.exists():
        for f in raw_dir.rglob("*.json"):
            raw_size += f.stat().st_size
            raw_files += 1

    return {
        "disk": disk,
        "push_log_size_mb": round(push_log_size / 1024 / 1024, 2),
        "push_log_files": push_log_files,
        "raw_size_mb": round(raw_size / 1024 / 1024, 2),
        "raw_files": raw_files,
        "push_log_warn": push_log_size > PUSH_LOG_WARN_BYTES,
        "disk_warn": int(disk.get("use_pct", "0").rstrip("%") or 0) >= DISK_WARN_PCT,
    }


def collect_calendar_today() -> list:
    """今日 4 个日历事件触发情况（基于 logs/inspections/ + daily 抓取时间戳）"""
    today = get_today()
    events = [
        {"id": "1:00-crawl", "name": "1:00 自动抓取", "cron": "01:00"},
        {"id": "8:30-push", "name": "8:30 推送", "cron": "08:30"},
        {"id": "12:00-inspect", "name": "12:00 抓取巡检", "cron": "12:00"},
        {"id": "20:00-inspect", "name": "20:00 推送巡检", "cron": "20:00"},
    ]
    now = now_sh()
    now_min = now.hour * 60 + now.minute
    for ev in events:
        hh, mm = ev["cron"].split(":")
        cron_min = int(hh) * 60 + int(mm)
        # 触发情况 = 当前时间是否已过 cron 点（过了说明应该已跑）
        # 实际触发证据：
        #   1:00-crawl：data/raw/<src>/<yesterday>.json 存在
        #   8:30-push：logs/daily/<yesterday>-push.jsonl 存在
        #   12:00-inspect：data/inspections/<today>-12-00.md 存在
        #   20:00-inspect：data/inspections/<today>-20-00.md 存在（本巡检生成）
        if ev["id"] == "1:00-crawl":
            yest = get_yesterday()
            raw_dir = REPO_ROOT / "data" / "raw"
            triggered = raw_dir.exists() and any((raw_dir / s / f"{yest}.json").exists() for s in os.listdir(raw_dir))
        elif ev["id"] == "8:30-push":
            yest = get_yesterday()
            triggered = (LOGS_DAILY_DIR / f"{yest}-push.jsonl").exists() or (LOGS_DAILY_DIR / f"{yest}-push.json").exists()
        elif ev["id"] == "12:00-inspect":
            triggered = (INSPECTIONS_DIR / f"{today}-12-00.md").exists()
        else:  # 20:00-inspect
            triggered = (INSPECTIONS_DIR / f"{today}-20-00.md").exists()
        ev["triggered"] = triggered
        ev["status"] = "✅" if triggered else ("⏳" if now_min < cron_min else "❌")
    return events


def collect_pending_decisions() -> list:
    """读 recent_memory/decision/ 找主人未拍板项（status 含 pending / 等拍板 / 阻塞）"""
    if not RECENT_MEMORY.exists():
        return []
    pending = []
    keywords = ["拍板", "等指示", "P2", "P3", "blocked", "未拍板"]
    for f in sorted(RECENT_MEMORY.glob("*.md"), reverse=True):
        content = f.read_text(encoding="utf-8")
        if not any(kw in content for kw in keywords):
            continue
        # 找文件中含"等拍板"或"待拍板"的行
        for line in content.splitlines():
            if any(kw in line for kw in ["等拍板", "待拍板", "未拍板", "blocked", "P2", "P3"]):
                # 提取"- xxx" 或 "| xxx" 开头的关键短语（截短到 80 字符）
                m = re.search(r"(?:[-|]\s*)(.{8,80})", line)
                if m:
                    pending.append({
                        "file": f.name,
                        "snippet": m.group(1).strip()[:80],
                    })
    # 去重（同 file 内只保留前 3 条 snippet）
    seen = {}
    deduped = []
    for p in pending:
        seen.setdefault(p["file"], 0)
        if seen[p["file"]] < 3:
            deduped.append(p)
            seen[p["file"]] += 1
    return deduped


# ========== v1.4 新增维度：第 9 节「发布」 ==========
def collect_publish_anomalies(lookback_days: int = 7) -> dict:
    """扫 data/published/daily/ 最近 N 天，检测漏推 / 失败累计。

    返回：
        {
            "lookback_days": N,
            "missed": [{"date": "2026-06-15", "report_file": "每日资讯/2026-06-15.md"}],
            "consecutive_failures": [{"start_date": "...", "end_date": "...", "days": 2, "last_error": "..."}],
            "success_rate": 0.85, "total_days": 7, "ok_count": 6, "fail_count": 0, "missed_count": 1,
        }
    """
    result = {
        "lookback_days": lookback_days,
        "missed": [], "consecutive_failures": [],
        "success_rate": 1.0, "total_days": lookback_days,
        "ok_count": 0, "fail_count": 0, "missed_count": 0,
    }
    if not PUBLISHED_DIR.exists():
        return result
    today = get_today()
    today_dt = datetime.strptime(today, "%Y-%m-%d").date()
    day_list = [(today_dt - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(lookback_days)]
    day_list.reverse()
    last_error = None
    consecutive_fail_count = 0
    consecutive_fail_start = None
    consecutive_fail_end = None
    for date_str in day_list:
        md_file = DAILY_DIR / f"{date_str}.md"
        pub_file = PUBLISHED_DIR / "daily" / f"{date_str}.json"
        if not pub_file.exists():
            if md_file.exists():
                result["missed"].append({"date": date_str, "report_file": f"每日资讯/{date_str}.md"})
            continue
        try:
            rec = json.loads(pub_file.read_text(encoding="utf-8"))
            if rec.get("ok"):
                result["ok_count"] += 1
                if consecutive_fail_count > 0:
                    if consecutive_fail_count >= 2:
                        result["consecutive_failures"].append({
                            "start_date": consecutive_fail_start, "end_date": consecutive_fail_end,
                            "days": consecutive_fail_count, "last_error": last_error,
                        })
                    consecutive_fail_count = 0
                    consecutive_fail_start = None
                    consecutive_fail_end = None
                    last_error = None
            else:
                result["fail_count"] += 1
                last_error = rec.get("error") or rec.get("channels", {})
                if consecutive_fail_count == 0:
                    consecutive_fail_start = date_str
                consecutive_fail_end = date_str
                consecutive_fail_count += 1
        except Exception:
            result["fail_count"] += 1
            last_error = "记录解析失败"
            if consecutive_fail_count == 0:
                consecutive_fail_start = date_str
            consecutive_fail_end = date_str
            consecutive_fail_count += 1
    if consecutive_fail_count >= 2:
        result["consecutive_failures"].append({
            "start_date": consecutive_fail_start, "end_date": consecutive_fail_end,
            "days": consecutive_fail_count, "last_error": last_error,
        })
    result["missed_count"] = len(result["missed"])
    denom = result["ok_count"] + result["fail_count"]
    result["success_rate"] = round(result["ok_count"] / denom, 3) if denom > 0 else 1.0
    return result


# ========== 8 节模板渲染 ==========
def render_full_report(typ: str, sections: dict) -> str:
    """详细报告（落档版，无字符限制）"""
    lines = []
    lines.append(f"# PM 日报 · {sections['meta']['today']} {typ}")
    lines.append("")
    lines.append(f"⏰ 生成时间: {sections['meta']['now']}")
    lines.append(f"🎯 巡检类型: {typ}")
    lines.append("")
    lines.append("═══════════════════════════════════════")
    lines.append("")

    # 【1】抓取状态
    lines.append("## 【1】抓取状态")
    lines.append("")
    sec1 = sections["crawl"]
    if sec1.get("daily", {}).get("exists"):
        d = sec1["daily"]
        lines.append(f"- ✅ 报告: 每日资讯/{sec1['yesterday']}.md · {d['items']} 条 · {d['size']}B")
    else:
        lines.append(f"- ❌ 报告缺失: 每日资讯/{sec1['yesterday']}.md")
    if sec1.get("raw"):
        lines.append("")
        lines.append("**各源落盘:**")
        for src, cnt in sec1["raw"].items():
            if cnt == 0:
                lines.append(f"   - ⚠️ {src}: 0 条")
            elif cnt == -1:
                lines.append(f"   - ❌ {src}: 解析失败（schema 失效）")
            else:
                lines.append(f"   - ✅ {src}: {cnt} 条")
    lines.append("")

    # 【2】推送状态
    lines.append("## 【2】推送状态")
    lines.append("")
    sec2 = sections["push"]
    if not sec2.get("log_exists"):
        lines.append(f"- ❌ 推送日志缺失: logs/daily/{sec2['report_date']}-push.{{jsonl,json}}（8:30 推送未跑）")
    elif sec2.get("error"):
        lines.append(f"- ❌ 日志解析失败: {sec2['error']}")
    elif sec2.get("entries", 0) == 0:
        lines.append(f"- ⚠️ 推送日志存在但无 entry")
    else:
        lines.append(f"- {'✅ 推送成功' if sec2.get('last_ok') else '❌ 推送失败'}: {sec2.get('last_target', '?')}")
        if sec2.get("last_doc_url"):
            lines.append(f"   - 📄 云文档: {sec2['last_doc_url']}")
        for ch, info in (sec2.get("last_channels") or {}).items():
            icon = "✅" if info.get("ok") else "❌"
            lines.append(f"   - {icon} {ch}: code={info.get('code')} msg={info.get('msg', '')[:40]}")
    lines.append("")

    # 【3】探针状态
    lines.append("## 【3】探针状态")
    lines.append("")
    sec3 = sections["probe"]
    lines.append(f"- 进度: {sec3['version']}")
    lines.append(f"   - L1 bash 探针: {sec3['L1_bash']}")
    lines.append(f"   - L2 异常升级: {sec3['L2_escalation']}")
    lines.append(f"   - L3 calendar 兜底: {sec3['L3_calendar']}")
    lines.append("")

    # 【4】累计异常
    lines.append("## 【4】累计异常")
    lines.append("")
    sec4 = sections["consec_failures"]
    if not sec4:
        lines.append("- ✅ 无累计异常源")
    else:
        for f in sec4:
            icon = "❌" if f["type"] == "schema_invalid" else "⚠️"
            label = "schema 失效" if f["type"] == "schema_invalid" else f"连续 {f['days']} 天 0 条"
            lines.append(f"- {icon} {f['source']}: {label} ({', '.join(f['dates'][:3])})")
    lines.append("")

    # 【5】资源监控
    lines.append("## 【5】资源监控")
    lines.append("")
    sec5 = sections["resource"]
    d = sec5.get("disk", {})
    if d:
        warn = "⚠️" if sec5.get("disk_warn") else "✅"
        lines.append(f"- 磁盘: {warn} 已用 {d.get('use_pct', '?')}% / 总 {d.get('total', '?')}")
    warn = "⚠️" if sec5.get("push_log_warn") else "✅"
    lines.append(f"- 推送日志: {warn} {sec5['push_log_files']} 文件 / {sec5['push_log_size_mb']}MB")
    lines.append(f"- 抓取 raw: ✅ {sec5['raw_files']} 文件 / {sec5['raw_size_mb']}MB")
    lines.append("")

    # 【6】待办决策
    lines.append("## 【6】待办决策")
    lines.append("")
    sec6 = sections["pending"]
    if not sec6:
        lines.append("- ✅ 无待办决策")
    else:
        for p in sec6:
            lines.append(f"- ⏳ {p['snippet']} _(来源: {p['file']})_")
    lines.append("")

    # 【7】今日日历事件
    lines.append("## 【7】今日日历事件")
    lines.append("")
    sec7 = sections["calendar_today"]
    for ev in sec7:
        lines.append(f"- {ev['status']} {ev['name']} ({ev['cron']})")
    lines.append("")

    # Git
    lines.append("## Git")
    lines.append("")
    if "error" not in sections["git"]:
        lines.append(f"- 最近 commit: {sections['git']['last_commit']}")
        if sections["git"].get("unpushed") and sections["git"]["unpushed"] != "(无)":
            lines.append(f"- ⚠️ 未推送: {sections['git']['unpushed'][:80]}")
    lines.append("")

    # 【9】发布（v1.4）
    lines.append("## 【9】发布")
    lines.append("")
    sec9 = sections["publish"]
    lines.append(f"- 回溯: 最近 {sec9['lookback_days']} 天")
    lines.append(f"- 成功率: {sec9.get('success_rate', 0)*100:.0f}% "
                 f"(✅ {sec9['ok_count']} / ❌ {sec9['fail_count']} / ⚠️ 漏推 {sec9['missed_count']})")
    if sec9.get("missed"):
        lines.append("")
        lines.append("**漏推:**")
        for m in sec9["missed"]:
            lines.append(f"  - ❌ {m['date']} → {m['report_file']}")
    if sec9.get("consecutive_failures"):
        lines.append("")
        lines.append("**连续失败:**")
        for f in sec9["consecutive_failures"]:
            lines.append(f"  - ❌ {f['start_date']} ~ {f['end_date']}（{f['days']} 天）")
    lines.append("")

    # 【8】结论
    lines.append("## 【8】结论")
    lines.append("")
    lines.append(f"- {sections['conclusion']}")
    lines.append("")
    lines.append("═══════════════════════════════════════")

    return "\n".join(lines) + "\n"


def render_feishu_summary(typ: str, sections: dict) -> str:
    """飞书端摘要（< feishu_max_chars）"""
    lines = []
    lines.append(f"📋 **PM 日报 · {sections['meta']['today']} {typ}**")
    lines.append(f"⏰ {sections['meta']['now']}")
    lines.append("")
    lines.append("═══════════════════════════════")

    # 累计异常（重点）
    sec4 = sections["consec_failures"]
    if sec4:
        lines.append("")
        lines.append("**【4】累计异常**")
        for f in sec4:
            icon = "❌" if f["type"] == "schema_invalid" else "⚠️"
            label = "schema 失效" if f["type"] == "schema_invalid" else f"连续 {f['days']} 天 0 条"
            lines.append(f"  {icon} {f['source']}: {label}")

    # 日历事件
    sec7 = sections["calendar_today"]
    cal_untriggered = [ev for ev in sec7 if ev["status"] == "❌"]
    if cal_untriggered:
        lines.append("")
        lines.append("**【7】日历事件漏触发**")
        for ev in cal_untriggered:
            lines.append(f"  ❌ {ev['name']} ({ev['cron']})")

    # 资源告警
    sec5 = sections["resource"]
    if sec5.get("disk_warn") or sec5.get("push_log_warn"):
        lines.append("")
        lines.append("**【5】资源告警**")
        if sec5.get("disk_warn"):
            d = sec5.get("disk", {})
            lines.append(f"  ⚠️ 磁盘: 已用 {d.get('use_pct', '?')}%")
        if sec5.get("push_log_warn"):
            lines.append(f"  ⚠️ 推送日志: {sec5['push_log_size_mb']}MB")

    # 待办决策（限 5 条）
    sec6 = sections["pending"]
    if sec6:
        lines.append("")
        lines.append("**【6】待办决策**")
        for p in sec6[:5]:
            lines.append(f"  ⏳ {p['snippet']}")
        if len(sec6) > 5:
            lines.append(f"  ... 另 {len(sec6) - 5} 项")

    # 【9】发布（v1.4，仅异常时显示）
    sec9 = sections["publish"]
    if sec9.get("missed") or sec9.get("consecutive_failures"):
        lines.append("")
        lines.append("**【9】发布异常**")
        for m in sec9.get("missed", []):
            lines.append(f"  ❌ 漏推: {m['date']}")
        for f in sec9.get("consecutive_failures", []):
            lines.append(f"  ❌ 连续失败 {f['days']} 天 ({f['start_date']} ~ {f['end_date']})")

    # 结论
    lines.append("")
    lines.append("═══════════════════════════════")
    lines.append(f"**{sections['conclusion']}**")
    lines.append(f"📄 详细: data/inspections/{sections['meta']['today']}-{typ.replace(':', '-')}.md")
    lines.append("═══════════════════════════════")

    return truncate("\n".join(lines) + "\n", FEISHU_MAX)


# ========== 异常升级 hook ==========
def should_escalate(sections: dict) -> tuple[bool, list[str]]:
    """判断是否需要升级到 owner 私聊，返回 (should, reasons)"""
    if not ESCALATE_ON or not OWNER_OPENID:
        return False, []

    reasons = []
    if "consecutive_zero" in ESCALATE_ON:
        for f in sections["consec_failures"]:
            if f["type"] == "zero" and f["days"] >= CONSECUTIVE_ZERO_DAYS:
                reasons.append(f"源 {f['source']} 连续 {f['days']} 天 0 条")
    if "schema_invalid" in ESCALATE_ON:
        for f in sections["consec_failures"]:
            if f["type"] == "schema_invalid":
                reasons.append(f"源 {f['source']} schema 失效 {f['days']} 天")
    if "calendar_missed" in ESCALATE_ON:
        for ev in sections["calendar_today"]:
            if ev["status"] == "❌":
                reasons.append(f"日历事件漏触发: {ev['name']}")
    if "publish_missed" in ESCALATE_ON:
        for m in sections["publish"].get("missed", []):
            reasons.append(f"日报漏推: {m['date']} → {m['report_file']}")
    if "publish_consecutive_failure" in ESCALATE_ON:
        for f in sections["publish"].get("consecutive_failures", []):
            reasons.append(f"推送连续失败 {f['days']} 天 ({f['start_date']} ~ {f['end_date']})")
    return bool(reasons), reasons


def load_dedup() -> dict:
    if not ESCALATE_DEDUP_FILE.exists():
        return {}
    try:
        return json.loads(ESCALATE_DEDUP_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_dedup(d: dict):
    ESCALATE_DEDUP_FILE.parent.mkdir(parents=True, exist_ok=True)
    ESCALATE_DEDUP_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


def escalate_to_owner(reasons: list[str], sections: dict) -> dict:
    """通过 lark-cli im +message-send 给 owner 发私聊
       24h 同一原因只发 1 次（dedup 机制）"""
    dedup = load_dedup()
    now = now_sh()
    fresh_reasons = []
    for r in reasons:
        last = dedup.get(r)
        if last:
            try:
                last_dt = datetime.fromisoformat(last)
                if (now - last_dt).total_seconds() < ESCALATE_DEDUP_HOURS * 3600:
                    continue   # 24h 内发过
            except Exception:
                pass
        fresh_reasons.append(r)

    if not fresh_reasons:
        return {"escalated": False, "reason": "all reasons in dedup window", "reasons_input": reasons}

    # 拼私聊消息
    lines = [
        f"🚨 PM 日报·异常升级（{sections['meta']['today']} {sections['meta']['hhmm']}）",
        "",
        "以下异常触发升级阈值：",
    ]
    for r in fresh_reasons:
        lines.append(f"  - {r}")
    lines.append("")
    lines.append(f"📄 详细: data/inspections/{sections['meta']['today']}-{sections['meta']['hhmm'].replace(':', '-')}.md")
    text = "\n".join(lines)

    # lark-cli im +messages-send --as user --user-id <owner_openid>
    try:
        result = subprocess.run(
            ["lark-cli", "im", "+messages-send",
             "--as", "user",
             "--user-id", OWNER_OPENID,
             "--text", text],
            capture_output=True, text=True, timeout=15,
            env={**os.environ, "LARK_CLI_NO_PROXY": "1", "TZ": "Asia/Shanghai"},
        )
        ok = result.returncode == 0
    except Exception as e:
        return {"escalated": False, "reason": f"lark-cli exception: {e}", "reasons_input": reasons}

    # 更新 dedup
    for r in fresh_reasons:
        dedup[r] = now.isoformat()
    save_dedup(dedup)

    return {
        "escalated": ok,
        "reasons": fresh_reasons,
        "lark_stderr": result.stderr[:200] if result.stderr else "",
        "lark_stdout": result.stdout[:200] if result.stdout else "",
    }


# ========== 总结论生成 ==========
def make_conclusion(sections: dict) -> str:
    """生成【8】结论"""
    issues = []
    # 抓取异常
    sec1 = sections["crawl"]
    if not sec1.get("daily", {}).get("exists"):
        issues.append("日报缺失")
    if sec1.get("raw"):
        abnormal_cnt = sum(1 for c in sec1["raw"].values() if c <= 0)
        if abnormal_cnt > 0:
            issues.append(f"{abnormal_cnt} 源异常")
    # 推送异常
    sec2 = sections["push"]
    if not sec2.get("log_exists"):
        issues.append("推送未跑")
    elif sec2.get("entries", 0) == 0:
        issues.append("推送无 entry")
    elif sec2.get("last_ok") is False:
        issues.append("8:30 推送失败")
    # 累计异常
    sec4 = sections["consec_failures"]
    if sec4:
        issues.append(f"{len(sec4)} 源累计异常")
    # 日历漏触发
    sec7 = sections["calendar_today"]
    cal_missed = sum(1 for ev in sec7 if ev["status"] == "❌")
    if cal_missed:
        issues.append(f"{cal_missed} 日历事件漏触发")
    # 资源告警
    sec5 = sections["resource"]
    if sec5.get("disk_warn"):
        issues.append("磁盘超阈值")
    if sec5.get("push_log_warn"):
        issues.append("推送日志过大")
    # 待办
    sec6 = sections["pending"]
    if sec6:
        issues.append(f"{len(sec6)} 项待办")
    # 发布异常（v1.4）
    sec9 = sections["publish"]
    if sec9.get("missed"):
        issues.append(f"{len(sec9['missed'])} 日报漏推")
    if sec9.get("consecutive_failures"):
        issues.append(f"推送连续失败 {sec9['consecutive_failures'][0]['days']} 天")

    if not issues:
        return "✅ 整体正常"
    if any("❌" in i or "失败" in i or "缺失" in i or "未跑" in i or "漏触发" in i for i in issues):
        return f"❌ {len(issues)} 项异常: " + " / ".join(issues)
    return f"⚠️ {len(issues)} 项待跟进: " + " / ".join(issues)


# ========== 12:00 / 20:00 主入口 ==========
def generate(typ: str, write_file: bool = True, escalate: bool = True) -> dict:
    """生成完整报告 + 飞书摘要 + 升级；返回 dict 便于调用方使用"""
    today = get_today()
    yest = get_yesterday()
    now = now_sh()

    # 采集
    raw = collect_raw_counts(yest)
    daily = collect_daily_report(yest)
    push = collect_push_status(yest)   # 20:00 查 8:30 推 yest 报告
    git = collect_git_status()
    consec = collect_consecutive_failures()
    probe = collect_probe_status()
    resource = collect_resource_usage()
    calendar_today = collect_calendar_today()
    pending = collect_pending_decisions()

    # 12:00 简略推送状态（仅展示 8:30 推送的是前天报告，8:30 推送应已跑过）
    if typ == "12:00":
        push_summary = push
    else:
        push_summary = push

    sections = {
        "meta": {
            "today": today,
            "yesterday": yest,
            "now": now.strftime("%Y-%m-%d %H:%M %Z"),
            "hhmm": typ,
        },
        "crawl": {"yesterday": yest, "daily": daily, "raw": raw},
        "push": dict(push_summary, **{"report_date": yest}),
        "probe": probe,
        "consec_failures": consec,
        "resource": resource,
        "calendar_today": calendar_today,
        "pending": pending,
        "git": git,
        "publish": collect_publish_anomalies(lookback_days=PUBLISH_LOOKBACK_DAYS),  # v1.4 第 9 维度
    }
    sections["conclusion"] = make_conclusion(sections)

    full_text = render_full_report(typ, sections)
    feishu_text = render_feishu_summary(typ, sections)

    fname = f"{today}-{typ.replace(':', '-')}.md"
    feishu_fname = f"{today}-{typ.replace(':', '-')}-feishu.md"
    if write_file:
        INSPECTIONS_DIR.mkdir(parents=True, exist_ok=True)
        out = INSPECTIONS_DIR / fname
        out.write_text(full_text, encoding="utf-8")
        feishu_out = INSPECTIONS_DIR / feishu_fname
        feishu_out.write_text(feishu_text, encoding="utf-8")

    result = {
        "type": typ,
        "today": today,
        "full_report_file": fname if write_file else None,
        "full_text_chars": len(full_text),
        "feishu_text_chars": len(feishu_text),
        "conclusion": sections["conclusion"],
        "issues_count": sum(1 for ev in calendar_today if ev["status"] == "❌") + len(consec),
    }

    # 升级 hook
    if escalate:
        should, reasons = should_escalate(sections)
        if should:
            esc = escalate_to_owner(reasons, sections)
            result["escalation"] = esc
        else:
            result["escalation"] = {"escalated": False, "reason": "no threshold triggered"}

    return result, sections, feishu_text


# ========== Main ==========
def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("12:00", "20:00"):
        print("用法: generate_inspection.py [12:00|20:00] [--no-write] [--no-escalate]")
        sys.exit(1)

    typ = sys.argv[1]
    write = "--no-write" not in sys.argv
    escalate = "--no-escalate" not in sys.argv

    result, sections, feishu_text = generate(typ, write_file=write, escalate=escalate)

    # 落档打印
    if result["full_report_file"]:
        print(f"✅ 巡检报告已落档: data/inspections/{result['full_report_file']}")
    print(f"📊 飞书摘要: {result['feishu_text_chars']} 字符 / 落档详细: {result['full_text_chars']} 字符")
    print(f"🔍 结论: {result['conclusion']}")
    if "escalation" in result:
        esc = result["escalation"]
        if esc.get("escalated"):
            print(f"🚨 异常升级: {len(esc.get('reasons', []))} 项已 DM owner")
        else:
            print(f"✅ 无需升级: {esc.get('reason', '?')}")
    print("")
    print("---FEISHU_SUMMARY---")
    print(feishu_text)
    print("---END---")


if __name__ == "__main__":
    main()
