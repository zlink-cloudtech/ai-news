#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
generate_inspection.py - 生成 PM 巡检报告（v1.2 推送架构·management 消息源）

设计：报告以 markdown 形式落档到 data/inspections/<date>-<HH-MM>.md，
     再由 scripts/push_report.sh 推到 purpose=management 的渠道（飞书主群等）。
     内容控制在 < 1900 字符（飞书 text 限制 2000，留 buffer）。

调用：
  ./scripts/generators/generate_inspection.py 12:00   # 抓取状态巡检
  ./scripts/generators/generate_inspection.py 20:00   # 推送状态巡检
  ./scripts/generators/generate_inspection.py 12:00 --no-write   # 只输出不落档（调试用）

依赖：.secrets 加载通过调用方保证（push_report.sh 已 set -a source）
"""
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

TZ_SH = timezone(timedelta(hours=8))
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
INSPECTIONS_DIR = REPO_ROOT / "data" / "inspections"
LOGS_DAILY_DIR = REPO_ROOT / "logs" / "daily"
LOGS_DIR = REPO_ROOT / "logs"
DAILY_DIR = REPO_ROOT / "每日资讯"

MAX_FEISHU_CHARS = 1900   # 飞书 text 上限 2000，留 100 字符 buffer


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


# ========== 数据采集 ==========
def collect_raw_counts(date_str: str) -> dict:
    """data/raw/<source>/<date>.json 条数（已知 schema 失效源标 -1）"""
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
            # data/raw/<src>/<date>.json 结构：{source, date, count, items: [...], ...}
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
            result[source] = -1   # 解析失败（=已知 schema 失效）
    return result


def collect_daily_report(date_str: str) -> dict:
    """每日资讯/<date>.md 元信息"""
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
    """logs/daily/<report_date>-push.{jsonl,json} 最后一条 entry
       注意：日志文件名按 REPORT 日期，不是推送日期（8:30 推 6-15 报告 → logs/daily/2026-06-15-push.*）
    """
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


# ========== 报告生成 ==========
def gen_1200() -> tuple[str, str]:
    """12:00 抓取巡检；返回 (md 文本, 落档文件名)"""
    now = now_sh()
    today = get_today()
    yest = get_yesterday()
    raw = collect_raw_counts(yest)
    daily = collect_daily_report(yest)
    git = collect_git_status()
    fname = f"{today}-12-00.md"

    lines: list[str] = []
    lines.append(f"📋 **PM 巡检·12:00 抓取状态**")
    lines.append(f"⏰ {now.strftime('%Y-%m-%d %H:%M %Z')}")
    lines.append(f"🎯 目标窗口: {yest} 1:00 自动抓取")
    lines.append("")

    # 报告产物
    if daily.get("exists"):
        lines.append(f"✅ 报告: 每日资讯/{yest}.md · {daily['items']} 条 · {daily['size']}B")
    else:
        lines.append(f"❌ 报告缺失: 每日资讯/{yest}.md（先跑 run_generate_daily.sh）")

    # 各源
    if raw:
        lines.append("")
        lines.append("📊 各源落盘:")
        abnormal = []
        for src, cnt in raw.items():
            if cnt == 0:
                lines.append(f"   ⚠️ {src}: 0 条")
                abnormal.append(src)
            elif cnt == -1:
                lines.append(f"   ❌ {src}: 解析失败（schema 失效）")
                abnormal.append(src)
            else:
                lines.append(f"   ✅ {src}: {cnt} 条")
    else:
        lines.append("")
        lines.append("❌ data/raw/ 无任何源落盘")

    # Git
    lines.append("")
    if "error" not in git:
        lines.append(f"🔗 Git: {git['last_commit']}")
        if git.get("unpushed") and git["unpushed"] != "(无)":
            lines.append(f"   ⚠️ 未推送: {git['unpushed'][:80]}")

    # 结论
    lines.append("")
    if daily.get("exists") and not abnormal:
        lines.append("**结论**: ✅ 抓取+生成全链路正常")
    elif daily.get("exists"):
        lines.append(f"**结论**: ⚠️ 报告已生成，{len(abnormal)} 源异常（详见上）")
    else:
        lines.append("**结论**: ❌ 抓取/生成失败，需排查")

    return "\n".join(lines) + "\n", fname


def gen_2000() -> tuple[str, str]:
    """20:00 推送巡检；返回 (md 文本, 落档文件名)
       检查的是"今天 8:30 推送的昨天报告" → logs/daily/<yesterday>-push.{jsonl,json}
    """
    now = now_sh()
    today = get_today()
    yest = get_yesterday()
    push = collect_push_status(yest)   # ← 按报告日期查
    git = collect_git_status()
    fname = f"{today}-20-00.md"

    lines: list[str] = []
    lines.append(f"📋 **PM 巡检·20:00 推送状态**")
    lines.append(f"⏰ {now.strftime('%Y-%m-%d %H:%M %Z')}")
    lines.append(f"🎯 目标窗口: {today} 8:30 推送")
    lines.append("")

    if not push.get("log_exists"):
        lines.append(f"❌ 推送日志缺失: logs/daily/{yest}-push.{{jsonl,json}}（8:30 推送未跑）")
    elif push.get("error"):
        lines.append(f"❌ 日志解析失败: {push['error']}")
    elif push.get("entries", 0) == 0:
        lines.append(f"⚠️ 推送日志存在但无 entry")
    else:
        last = push
        if last.get("last_ok"):
            lines.append(f"✅ 推送成功: {last.get('last_target', '?')}")
            if last.get("last_doc_url"):
                lines.append(f"   📄 云文档: {last['last_doc_url'][:80]}")
            for ch, info in (last.get("last_channels") or {}).items():
                icon = "✅" if info.get("ok") else "❌"
                lines.append(f"   {icon} {ch}: code={info.get('code')} msg={info.get('msg', '')[:40]}")
        else:
            err = last.get("last_error", "未知")
            lines.append(f"❌ 推送失败: {err[:200]}")

    # Git
    lines.append("")
    if "error" not in git:
        lines.append(f"🔗 Git: {git['last_commit']}")

    # 结论
    lines.append("")
    if push.get("last_ok"):
        lines.append("**结论**: ✅ 8:30 推送正常")
    elif push.get("log_exists"):
        lines.append("**结论**: ❌ 8:30 推送失败，需排查")
    else:
        lines.append("**结论**: ⚠️ 8:30 推送未触发（可能停推或脚本异常）")

    return "\n".join(lines) + "\n", fname


# ========== Main ==========
def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("12:00", "20:00"):
        print("用法: generate_inspection.py [12:00|20:00] [--no-write]")
        sys.exit(1)

    no_write = "--no-write" in sys.argv
    typ = sys.argv[1]
    INSPECTIONS_DIR.mkdir(parents=True, exist_ok=True)

    if typ == "12:00":
        text, fname = gen_1200()
    else:
        text, fname = gen_2000()

    # 截断到飞书 text 限制
    if len(text) > MAX_FEISHU_CHARS:
        text = text[:MAX_FEISHU_CHARS - 20] + "\n…(已截断)\n"

    if not no_write:
        out = INSPECTIONS_DIR / fname
        out.write_text(text, encoding="utf-8")
        print(f"✅ 巡检报告已落档: {out}")

    # 始终打印到 stdout（调用方可用于管道）
    print("---REPORT-START---")
    print(text)
    print("---REPORT-END---")


if __name__ == "__main__":
    main()
