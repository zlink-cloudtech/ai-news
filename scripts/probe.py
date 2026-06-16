#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
probe.py - 渠道健康探针（v1.2；改进 6-15 20:00 P3 副作用问题）

设计原则：
- 静默：webhook 探测走 HEAD/OPTIONS（不发群消息；webhook 端点对 GET 通常返回 4xx 即视为 URL 可达）
- 报告：默认通过 lark-cli im +message-send --as user --chat_id <owner_openid> DM 主人
        （避免 6-15 20:00 探活误发群副作用再次发生）
- 落档：结果写 logs/daily/<date>-probe.jsonl（结构化，便于 PM 巡检聚合）

调用：
  ./scripts/probe.py                 # DM 主人 + 写日志
  ./scripts/probe.py --silent        # 只写日志，不 DM
  ./scripts/probe.py --json          # 输出 JSON 到 stdout（不写日志、不 DM）

依赖：OWNER_OPENID 环境变量（默认 ou_d61bd0e15a001066cac4a97cace649f8 = 王鸿奇）
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

TZ_SH = timezone(timedelta(hours=8))
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
CHANNELS_CONFIG = REPO_ROOT / "config" / "channels.json"
SECRETS_FILE = REPO_ROOT / ".secrets"
LOGS_DAILY_DIR = REPO_ROOT / "logs" / "daily"
OWNER_OPENID = os.environ.get("OWNER_OPENID", "ou_d61bd0e15a001066cac4a97cace649f8")


def now_sh() -> datetime:
    return datetime.now(TZ_SH)


def sh(cmd: str, env_extra: dict | None = None) -> str:
    env = {**os.environ, "TZ": "Asia/Shanghai"}
    if env_extra:
        env.update(env_extra)
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10, env=env)
        return r.stdout.strip()
    except Exception as e:
        return f"ERR: {e}"


def load_secrets():
    """加载 .secrets 进 os.environ（不覆盖已有 env）"""
    if not SECRETS_FILE.exists():
        return
    for line in SECRETS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)


def check_webhook(name: str, ch_cfg: dict) -> dict:
    """HEAD 探测 webhook URL（不发消息；4xx 通常表示 URL 可达 + 方法不允许）"""
    env_name = ch_cfg.get("webhook_url_env")
    if not env_name:
        return {"reachable": False, "error": "missing webhook_url_env in channels.json"}
    url = os.environ.get(env_name)
    if not url:
        return {"reachable": False, "error": f"env {env_name} not set (check .secrets)"}

    try:
        out = sh(
            f"curl -sS --noproxy '*' -o /dev/null -w '%{{http_code}}' -m 5 -I '{url}'",
            env_extra={"LARK_CLI_NO_PROXY": "1"},
        )
        code = int(out.strip() or "0")
        # webhook 端点 GET/HEAD 通常返回 400/405/404：表示 URL 可达 + 服务在线
        return {
            "reachable": code in (200, 400, 401, 403, 404, 405),
            "http_code": code,
        }
    except Exception as e:
        return {"reachable": False, "error": str(e)[:200]}


def last_push_status(channel_name: str, today: str) -> dict | None:
    """从 logs/daily/<today>-push.jsonl 读最近推送结果"""
    f = LOGS_DAILY_DIR / f"{today}-push.jsonl"
    if not f.exists():
        return None
    try:
        entries = [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]
        for e in reversed(entries):
            ch = e.get("channels", {}).get(channel_name)
            if ch:
                return {
                    "ok": ch.get("ok"),
                    "code": ch.get("code"),
                    "msg": ch.get("msg", "")[:80],
                    "at": e.get("ended_at"),
                }
        return None
    except Exception:
        return None


def dm_owner(message: str) -> dict:
    """lark-cli IM DM 主人（静默）"""
    try:
        r = subprocess.run(
            ["lark-cli", "im", "+message-send",
             "--as", "user", "--chat-id", OWNER_OPENID,
             "--text", message],
            capture_output=True, text=True, timeout=20,
            env={**os.environ, "LARK_CLI_NO_PROXY": "1", "TZ": "Asia/Shanghai"},
        )
        return {
            "ok": r.returncode == 0,
            "code": r.returncode,
            "stdout": r.stdout[:300],
            "stderr": r.stderr[:300],
        }
    except FileNotFoundError:
        return {"ok": False, "error": "lark-cli not found in PATH"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def main():
    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
        sys.exit(0)

    silent = "--silent" in sys.argv
    json_only = "--json" in sys.argv

    load_secrets()

    if not CHANNELS_CONFIG.exists():
        print(f"❌ channels.json 不存在: {CHANNELS_CONFIG}", file=sys.stderr)
        sys.exit(1)

    cfg = json.loads(CHANNELS_CONFIG.read_text(encoding="utf-8"))
    today = sh("date '+%Y-%m-%d'")
    now = now_sh().isoformat(timespec="seconds")

    channels_result = {}
    for name, ch in cfg.get("channels", {}).items():
        if not ch.get("enabled"):
            channels_result[name] = {"enabled": False, "skipped": True}
            continue
        probe = check_webhook(name, ch)
        last_push = last_push_status(name, today)
        channels_result[name] = {
            "enabled": True,
            "type": ch.get("type"),
            "purpose": ch.get("purpose"),
            "webhook_reachable": probe.get("reachable"),
            "http_code": probe.get("http_code"),
            "error": probe.get("error"),
            "last_push": last_push,
        }

    summary = {
        "schema_version": "1.0",
        "tool": "probe.py",
        "at": now,
        "date": today,
        "channels": channels_result,
    }

    if json_only:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return

    # 写日志
    LOGS_DAILY_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOGS_DAILY_DIR / f"{today}-probe.jsonl"
    with log_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(summary, ensure_ascii=False) + "\n")
    print(f"📝 探针日志: {log_file}")

    # 打印简洁结果
    total = sum(1 for c in channels_result.values() if c.get("enabled") and not c.get("skipped"))
    healthy = sum(1 for c in channels_result.values() if c.get("webhook_reachable"))
    print(f"\n🔍 探活结果: {healthy}/{total} 渠道 webhook 可达")
    for name, info in channels_result.items():
        if info.get("skipped"):
            continue
        icon = "✅" if info.get("webhook_reachable") else "❌"
        print(f"   {icon} {name} ({info.get('type')}): code={info.get('http_code')}")

    # DM 主人
    if not silent:
        msg_lines = [f"🔍 AI 资讯·渠道探活 {today}", ""]
        for name, info in channels_result.items():
            if info.get("skipped"):
                continue
            icon = "✅" if info.get("webhook_reachable") else "❌"
            last = info.get("last_push") or {}
            last_str = ""
            if last.get("ok") is True:
                last_str = " · 上次推送成功"
            elif last.get("ok") is False:
                last_str = f" · 上次推送失败: {last.get('msg', '')}"
            msg_lines.append(f"{icon} {name} ({info.get('type')}){last_str}")
        msg_lines.append("")
        msg_lines.append(f"汇总: {healthy}/{total} 渠道 webhook 可达")
        dm_text = "\n".join(msg_lines)
        dm_result = dm_owner(dm_text)
        print(f"\n📨 DM 主人: ok={dm_result.get('ok')}")
        if not dm_result.get("ok"):
            print(f"   错误: {dm_result.get('stderr') or dm_result.get('error')}")


if __name__ == "__main__":
    main()
