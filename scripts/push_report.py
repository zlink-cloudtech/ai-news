"""
AI 资讯追踪·通用推送脚本（v2.1 完整 python 化）
==============================================

替代 push_report.sh 869 行 bash。功能等价：
- 5 类 REPORT_TYPE：daily / weekly / special / management / test
- 4 个子命令：normal / --test / --list-published / --backfill
- v1.4 skip-if-pushed / --repush / --dry-run
- v1.5 weekly 推送文本（_build_weekly_text）
- v2.0 daily JSON 切换（push 链路读 daily JSON 调 renderers）
- EXIT trap 写 jsonl 日志 + published_record

设计原则：
- 单一 Python 语言（不再混 bash + python heredoc）
- atexit 替代 bash trap EXIT
- subprocess.run 调 lark-cli（沙箱内 LARK_CLI_NO_PROXY=1 自动加）
- 状态机简化：把 bash 全局变量收敛到 PushState dataclass

依赖：
- config/channels.json
- .secrets（注入 env vars；缺则 fail-fast）
- scripts/published_record.py
- scripts/renderers/（v2.0 daily）
- lark-cli（外部命令，路径在 PATH 或 LARK_CLI env）
"""

from __future__ import annotations

import argparse
import atexit
import base64
import datetime as _dt
import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

# 仓库根 = scripts/ 的父目录
REPO_ROOT = Path(__file__).resolve().parent.parent
TZ_SH = ZoneInfo("Asia/Shanghai")
SCHEMA_VERSION = "1.2"

# 隐含 purpose 映射（per 6-16 12:20 设计）
IMPLIED_PURPOSE = {
    "daily": "official",
    "weekly": "official",
    "special": "official",
    "management": "management",
    "test": "test",
}


# ============== 状态 ==============

@dataclass
class PushState:
    """推送状态机：bash 全局变量收敛到 dataclass"""
    target_file: str = ""          # 相对 REPO_ROOT
    report_file: Path | None = None  # 绝对路径
    report_type: str = ""          # daily/weekly/special/management/test
    doc_url: str = ""
    log_file: Path | None = None
    push_ok: bool = False
    skipped_pushed: bool = False   # v1.4：skip-if-pushed 触发
    error_msg: str = ""
    channels_result: dict = field(default_factory=dict)
    test_text: str = ""
    test_file: str = ""
    daily_v2_json: str = ""        # v2.0 daily JSON 路径
    started_at: str = ""
    ended_at: str = ""
    duration_sec: int = 0
    test_mode: bool = False
    repush: bool = False
    skip_if_pushed: bool = True
    dry_run: bool = False
    pushed_by_override: str = ""
    enabled_channels: list[str] = field(default_factory=list)
    include_manual: bool = False          # v1.2.2：让 manual_only 渠道也参与（手动预发测试用）
    only_channels: list[str] = field(default_factory=list)  # v1.2.2：白名单，只推这些渠道
    doc_title_prefix: str = ""            # v1.2.2：docx 标题前缀（手动预发测试加 [预发] 标记）
    cfg: dict | None = None               # v2.3.4：channels.json 上下文（_write_published_record 剥 manual_only 用）
    skip_check_reason: str = ""           # v2.3.4：skip-if-pushed 检查结果原因（already_pushed / no_record / manual_only_only）


# ============== 工具函数 ==============

def _now_iso() -> str:
    return _dt.datetime.now(TZ_SH).isoformat(timespec="seconds")


def _now_compact() -> str:
    return _dt.datetime.now(TZ_SH).strftime("%Y%m%d-%H%M%S")


def _now_human() -> str:
    return _dt.datetime.now(TZ_SH).strftime("%Y-%m-%d %H:%M:%S")


def _today_str() -> str:
    return _dt.datetime.now(TZ_SH).strftime("%Y-%m-%d")


def _yesterday_str() -> str:
    return (_dt.datetime.now(TZ_SH) - _dt.timedelta(days=1)).strftime("%Y-%m-%d")


def _load_secrets(secrets_file: Path) -> None:
    """从 .secrets 文件加载 env vars（注入到 os.environ）"""
    if not secrets_file.exists():
        print(f"❌ .secrets 不存在: {secrets_file}（v1.2 要求密钥走 env）", file=sys.stderr)
        sys.exit(1)
    with open(secrets_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            # 去掉引号
            v = v.strip().strip('"').strip("'")
            os.environ[k.strip()] = v


def _load_channels(channels_config: Path) -> dict:
    """读 channels.json"""
    with open(channels_config, encoding="utf-8") as f:
        return json.load(f)


def _get_env(name: str, channel_name: str) -> str:
    """读 env var；缺则 raise"""
    val = os.environ.get(name)
    if not val:
        raise ValueError(f"env var '{name}' not set for channel '{channel_name}' (check .secrets)")
    return val


def _curl_post(url: str, payload: dict, timeout: int = 15) -> str:
    """POST JSON；no-proxy（沙箱友好）"""
    r = subprocess.run(
        ["curl", "-sS", "--noproxy", "*", "-X", "POST", url,
         "-H", "Content-Type: application/json",
         "-d", json.dumps(payload, ensure_ascii=False)],
        capture_output=True, text=True, timeout=timeout
    )
    return r.stdout


def _infer_report_type(target_file: str) -> str:
    """按路径推断 REPORT_TYPE"""
    if target_file.startswith("每日资讯/"):
        return "daily"
    if target_file.startswith("每周汇总/"):
        return "weekly"
    if target_file.startswith("专题/"):
        return "special"
    if target_file.startswith("data/inspections/"):
        return "management"
    if target_file.startswith("test_messages/"):
        return "test"
    return "unknown"


def _select_channels(cfg: dict, report_type: str, include_manual: bool = False, only: list[str] | None = None) -> list[str]:
    """按 report_type + implied purpose 选 enabled 渠道
    v1.2.2 扩展：
      - include_manual: True 时让 manual_only 渠道也参与（手动预发测试）
      - only: 渠道白名单（如 ['wecom_test']），只推这些；与 include_manual 配合限制只推测试群
    """
    need_purpose = IMPLIED_PURPOSE.get(report_type, report_type)
    result = []
    for name, c in cfg.get("channels", {}).items():
        if not c.get("enabled"):
            continue
        if c.get("manual_only") and not include_manual:
            continue  # v1.2.2：手动专用渠道，自动任务不选
        if only and name not in only:
            continue  # v1.2.2：白名单过滤
        purposes = c.get("purpose", [])
        types = c.get("message_types", [])
        if (report_type in types or "all" in types) and need_purpose in purposes:
            result.append(name)
    return result


def _filter_manual_channels(channels_result: dict, cfg: dict | None) -> dict:
    """v2.3.4：剥掉 manual_only 渠道（写 published_record 时调用）
    目的：避免预发测试污染 channels 字典，让 per-channel skip 检查更可靠
    行为：
      - cfg 为空 → 不过滤（向后兼容）
      - channels_result 中 manual_only 渠道 → 剥掉
      - 'skipped' 标记保留（per-channel skip 触发时写的）
    """
    if not cfg or "channels" not in cfg:
        return channels_result
    manual_names = {name for name, c in cfg.get("channels", {}).items() if c.get("manual_only")}
    return {k: v for k, v in channels_result.items() if k not in manual_names}


def _derive_date_from_path(target_file: str) -> str:
    """从路径提取日期：daily=YYYY-MM-DD / weekly=YYYY-Www / special=name / management=today"""
    m = re.search(r'(\d{4}-\d{2}-\d{2})', target_file)
    if m:
        return m.group(1)
    m = re.search(r'(\d{4}-W\d{2})', target_file)
    if m:
        return m.group(1)
    return Path(target_file).stem  # fallback


def _read_published_record(report_type: str, date: str) -> dict | None:
    """读已发布记录（v1.4 skip-if-pushed 用）"""
    p = REPO_ROOT / "data" / "published" / report_type / f"{date}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


# ============== 飞书 docx ==============

def _create_feishu_docx(target_file: str, title_prefix: str = "") -> tuple[str, str]:
    """lark-cli 建飞书云文档；返回 (doc_url, doc_id)
    v1.2.2：支持 title_prefix 非空时临时改 markdown 第一行加前缀（区分正式/测试文档）"""
    lark_cli = os.environ.get("LARK_CLI", "lark-cli")
    env = {**os.environ, "LARK_CLI_NO_PROXY": "1"}

    # v1.2.2：title_prefix 处理
    content_arg = f"@{target_file}"
    tmp_path = None
    if title_prefix:
        md_path = REPO_ROOT / target_file
        if md_path.exists():
            md_text = md_path.read_text(encoding="utf-8")
            lines = md_text.split("\n")
            title_replaced = False
            new_lines = []
            for line in lines:
                if not title_replaced and line.startswith("# "):
                    new_lines.append(f"# {title_prefix} {line[2:]}")
                    title_replaced = True
                else:
                    new_lines.append(line)
            if not title_replaced:
                new_lines.insert(0, f"# {title_prefix}")
            # 写临时文件
            tmp_path = REPO_ROOT / f".tmp_docx_{_now_compact()}.md"
            tmp_path.write_text("\n".join(new_lines), encoding="utf-8")
            # v1.2.2 fix：content_arg 用相对路径（lark-cli @绝对路径会 silently fallback 丢标题）
            content_arg = f"@{tmp_path.name}"

    try:
        r = subprocess.run(
            [lark_cli, "docs", "+create",
             "--api-version", "v2",  # v1.2.2 切到 v2：v1 对标题中 [xxx] 形式会过滤/转义
             "--as", "bot",
             "--doc-format", "markdown",
             "--content", content_arg],
            capture_output=True, text=True, env=env, timeout=60
        )
        output = r.stdout
        try:
            d = json.loads(output)
            if d.get("ok") and "data" in d and "document" in d["data"]:
                doc = d["data"]["document"]
                return doc.get("url", ""), doc.get("document_id", "")
        except Exception:
            pass
        return "", ""
    finally:
        # v1.2.2：清理临时文件
        if tmp_path and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def _set_docx_public(doc_id: str) -> bool:
    """设置飞书 docx 公开访问"""
    if not doc_id:
        return False
    lark_cli = os.environ.get("LARK_CLI", "lark-cli")
    env = {**os.environ, "LARK_CLI_NO_PROXY": "1"}
    r = subprocess.run(
        [lark_cli, "drive", "permission.public", "patch",
         "--as", "bot", "--yes",
         "--params", json.dumps({"token": doc_id, "type": "docx"}),
         "--data", json.dumps({
             "external_access": True,
             "security_entity": "anyone_can_view",
             "comment_entity": "anyone_can_view",
             "share_entity": "anyone",
             "link_share_entity": "anyone_readable",
             "invite_external": True,
         })],
        capture_output=True, text=True, env=env, timeout=60
    )
    try:
        d = json.loads(r.stdout)
        return d.get("code") == 0
    except Exception:
        return False


# ============== v1.5 weekly 文本 ==============

def _build_weekly_text(target_file: str, doc_url: str, mode: str) -> str:
    """v1.5 周报推送文本：标题 + 一句话总结 + 每日资讯链接表 + 完整周报链接。
    mode: 'feishu' (text, ≤2000 char) | 'wecom' (markdown, ≤4096 char)
    """
    md_path = REPO_ROOT / target_file
    md_text = md_path.read_text(encoding="utf-8") if md_path.exists() else ""

    # 提取"本周一句话总结"
    summary = ""
    for line in md_text.splitlines():
        if "**本周一句话总结**" in line:
            summary = line.split("**本周一句话总结**", 1)[-1].lstrip("：: ").strip()
            break

    # 提取周对应 7 天
    monday = sunday = None
    for line in md_text.splitlines():
        m = re.search(r"(\d{4}-\d{2}-\d{2})\s*~\s*(\d{4}-\d{2}-\d{2})", line)
        if m:
            monday = _dt.date.fromisoformat(m.group(1))
            sunday = _dt.date.fromisoformat(m.group(2))
            break

    # 读 published/daily 7 天 doc_url
    daily_links = []
    if monday and sunday:
        d = monday
        while d <= sunday:
            pub = REPO_ROOT / "data" / "published" / "daily" / f"{d.isoformat()}.json"
            if pub.exists():
                try:
                    rec = json.loads(pub.read_text(encoding="utf-8"))
                    # v2.0 用 publish_record.doc_url；v1.0 用顶层 doc_url
                    url = rec.get("publish_record", {}).get("doc_url") or rec.get("doc_url")
                    if url:
                        daily_links.append((d, url))
                except Exception:
                    pass
            d += _dt.timedelta(days=1)

    # 拼装
    if mode == "feishu":
        # feishu：plain text
        parts = [f"📅 AI资讯周报 {monday.strftime('%Y-%m-%d') if monday else ''} ~ {sunday.strftime('%Y-%m-%d') if sunday else ''}"]
        if summary:
            parts.append(f"📌 本周一句话总结：{summary}")
        if daily_links:
            parts.append("📅 本周日报：")
            for d, url in daily_links:
                parts.append(f"• {d.strftime('%m-%d')} · 日报链接 {url}")
        else:
            parts.append("（本周日报 doc 链接未生成）")
        parts.append(f"👉 完整周报：{doc_url}")
        return "\n\n".join(parts)[:4000]
    else:
        # wecom：markdown
        parts = [f"📅 **AI资讯周报** {monday.strftime('%Y-%m-%d') if monday else ''} ~ {sunday.strftime('%Y-%m-%d') if sunday else ''}"]
        if summary:
            parts.append(f"\n📌 本周一句话总结：{summary}")
        if daily_links:
            parts.append("\n📅 **本周日报**：")
            for d, url in daily_links:
                parts.append(f"• {d.strftime('%m-%d')} · [日报链接]({url})")
        else:
            parts.append("\n（本周日报 doc 链接未生成）")
        parts.append(f"\n👉 [完整周报]({doc_url})")
        return "\n\n".join(parts)[:4000]


# ============== 渲染（v2.0 daily 切换） ==============

def _load_daily_v2(daily_v2_json_path: str) -> dict | None:
    """读 daily v2.0 JSON"""
    if not daily_v2_json_path or not Path(daily_v2_json_path).exists():
        return None
    try:
        return json.loads(Path(daily_v2_json_path).read_text(encoding="utf-8"))
    except Exception as e:
        print(f"⚠️  daily v2.0 JSON 解析失败: {e}（回退 v1.0 stub）", file=sys.stderr)
        return None


def _render_daily_for_feishu(daily_v2: dict | None, basename: str, doc_url: str) -> str:
    """daily 飞书 webhook text（≤2000 char）
    v2.0: 标题 + one_line_summary + 完整报告
    v1.0: 标题 + 完整报告 stub
    """
    if daily_v2 is not None:
        date_iso = daily_v2.get("report_date", basename)
        one_line = daily_v2.get("one_line_summary", {}).get("text", "")
        if one_line:
            return f"📰 AI资讯日报 · {date_iso}\n\n{one_line}\n\n完整报告：{doc_url}"
        return f"📰 AI资讯日报 · {date_iso}\n\n完整报告：{doc_url}"
    return f"📰 AI资讯日报 · {basename}\n\n完整报告：{doc_url}"


def _render_daily_for_wecom(daily_v2: dict | None, basename: str, doc_url: str) -> str:
    """daily 企微 markdown（≤4096 char；方案 B ≤500 char）
    v2.0: 调 render_wecom_markdown（简短摘要 + 飞书链接）
    v1.0: 标题 + 完整报告 stub
    """
    if daily_v2 is not None:
        from scripts.renderers import render_wecom_markdown
        return render_wecom_markdown(daily_v2)
    return f"📰 **AI资讯日报** · {basename}\n\n[👉 完整报告]({doc_url})"


# ============== 推送器（feishu + wecom） ==============

def _push_feishu(ch_cfg: dict, channel_name: str, state: PushState) -> dict:
    """飞书 webhook 推送"""
    basename = Path(state.target_file).stem
    if state.test_mode:
        text = state.test_text
    elif state.report_type == "management":
        text = state.report_file.read_text(encoding="utf-8")[:2000] if state.report_file else ""
        if "AI资讯" not in text:
            text = "【AI资讯·管理消息】\n" + text
    else:
        # daily/weekly/special
        if state.report_type == "daily":
            daily_v2 = _load_daily_v2(state.daily_v2_json) if state.daily_v2_json else None
            text = _render_daily_for_feishu(daily_v2, basename, state.doc_url)
        elif state.report_type == "weekly":
            text = _build_weekly_text(state.target_file, state.doc_url, "feishu")
        elif state.report_type == "special":
            text = f"📚 AI资讯专题 · {basename}\n\n完整报告：{state.doc_url}"
        else:
            text = f"📰 AI资讯 · {state.target_file}\n\n{state.doc_url or ''}"

    # 校验词
    kw = ch_cfg.get("keyword", "")
    if kw and kw not in text:
        return {"ok": False, "code": -1, "msg": f"keyword '{kw}' missing in text"}

    # 签名
    ts = str(int(time.time()))
    webhook_url = _get_env(ch_cfg["webhook_url_env"], channel_name)
    sign = ""
    if ch_cfg.get("webhook_secret_env"):
        secret = _get_env(ch_cfg["webhook_secret_env"], channel_name)
        key = f"{ts}\n{secret}".encode("utf-8")
        sign = base64.b64encode(hmac.new(key, b"", hashlib.sha256).digest()).decode("utf-8")

    payload = {"timestamp": ts, "sign": sign, "msg_type": "text", "content": {"text": text}}
    resp = _curl_post(webhook_url, payload)
    try:
        d = json.loads(resp)
        return {"ok": d.get("code") == 0, "code": d.get("code", -1), "msg": d.get("msg", "")}
    except Exception as e:
        return {"ok": False, "code": -1, "msg": f"parse err: {e}; resp={resp[:200]}"}


def _push_wecom(ch_cfg: dict, channel_name: str, state: PushState) -> dict:
    """企微 webhook 推送"""
    basename = Path(state.target_file).stem
    webhook_url = _get_env(ch_cfg["webhook_url_env"], channel_name)
    if state.test_mode:
        content = state.test_text
    elif state.report_type == "management":
        content = state.report_file.read_text(encoding="utf-8")[:2000] if state.report_file else ""
    else:
        if state.report_type == "daily":
            daily_v2 = _load_daily_v2(state.daily_v2_json) if state.daily_v2_json else None
            content = _render_daily_for_wecom(daily_v2, basename, state.doc_url)
        elif state.report_type == "weekly":
            content = _build_weekly_text(state.target_file, state.doc_url, "wecom")
        elif state.report_type == "special":
            content = f"📚 **AI资讯专题** · {basename}\n\n[👉 完整报告]({state.doc_url})"
        else:
            content = f"📰 **AI资讯** · {state.target_file}\n\n{state.doc_url or ''}"

    payload = {"msgtype": "markdown", "markdown": {"content": content}}
    resp = _curl_post(webhook_url, payload)
    try:
        d = json.loads(resp)
        return {"ok": d.get("errcode") == 0, "code": d.get("errcode", -1), "msg": d.get("errmsg", "")}
    except Exception as e:
        return {"ok": False, "code": -1, "msg": f"parse err: {e}; resp={resp[:200]}"}


PUSHERS = {
    "feishu": _push_feishu,
    "wecom": _push_wecom,
}


# ============== EXIT trap：写 jsonl 日志 + published_record ==============

def _write_log(state: PushState) -> None:
    """写 jsonl 日志"""
    if not state.log_file:
        return
    state.ended_at = _now_iso()
    try:
        started_dt = _dt.datetime.fromisoformat(state.started_at)
        ended_dt = _dt.datetime.fromisoformat(state.ended_at)
        state.duration_sec = int((ended_dt - started_dt).total_seconds())
    except Exception:
        state.duration_sec = 0
    entry = {
        "schema_version": SCHEMA_VERSION,
        "started_at": state.started_at,
        "ended_at": state.ended_at,
        "duration_sec": state.duration_sec,
        "target_file": state.target_file or None,
        "report_type": state.report_type,
        "ok": state.push_ok,
        "doc_url": state.doc_url or None,
        "test_file": state.test_file or None,
        "channels": state.channels_result,
        "error": state.error_msg or None,
    }
    try:
        state.log_file.parent.mkdir(parents=True, exist_ok=True)
        with open(state.log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        print(f"📝 结构化日志已追加: {state.log_file}")
    except Exception as e:
        print(f"⚠️  日志写入失败: {e}", file=sys.stderr)


def _write_published_record(state: PushState) -> None:
    """调 published_record.py record 写 data/published/<type>/<date>.json"""
    if state.skipped_pushed:
        print("⏭️  skip-if-pushed 触发，跳过 published 写入")
        return
    if state.test_mode:
        return
    if state.report_type not in ("daily", "weekly", "special", "management"):
        return

    # PUSHED_BY 自动检测
    pushed_by = state.pushed_by_override or os.environ.get("PUSH_INVOKED_FROM") or "manual"

    # v2.3.4：manual_only 渠道写隔离（写 published_record 时剥掉，预发测试不污染 channels 字典）
    channels_to_record = _filter_manual_channels(state.channels_result, state.cfg)

    # dry-run
    dry_flag = ["--dry-run"] if state.dry_run else []

    # v2.0 daily：传 daily JSON
    daily_json_flag = []
    if (state.report_type == "daily"
            and state.daily_v2_json
            and Path(state.daily_v2_json).exists()):
        daily_json_flag = ["--daily-json", state.daily_v2_json]

    try:
        # 基础参数（必传）
        cmd = [
            sys.executable, str(REPO_ROOT / "scripts" / "published_record.py"), "record",
            "--type", state.report_type,
            "--file", state.target_file,
            "--channels", json.dumps(channels_to_record, ensure_ascii=False),
            "--doc-url", state.doc_url,
            "--pushed-by", pushed_by,
            "--ok", "true" if state.push_ok else "false",
        ]
        # 可选参数（按需追加，避免空值污染 argparse）
        if state.error_msg:
            cmd.extend(["--error", state.error_msg])
        cmd.extend(daily_json_flag)
        cmd.extend(dry_flag)
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        # 打印 stdout 末尾
        if r.stdout:
            for line in r.stdout.splitlines()[-5:]:
                print(f"   {line}")
    except Exception as e:
        print(f"⚠️  published_record 写入失败: {e}", file=sys.stderr)


def _setup_trap(state: PushState) -> None:
    """注册 EXIT hook"""
    def _on_exit():
        _write_log(state)
        _write_published_record(state)
    atexit.register(_on_exit)


# ============== 主流程 ==============

def _resolve_daily_v2_json(state: PushState) -> None:
    """v2.0 daily：定位 daily JSON 路径"""
    if state.report_type != "daily" or not state.target_file.startswith("每日资讯/"):
        return
    daily_date = Path(state.target_file).stem
    candidate = str(REPO_ROOT / "data" / "published" / "daily" / f"{daily_date}.json")
    if Path(candidate).exists():
        state.daily_v2_json = candidate
        print(f"📦 daily v2.0 JSON: {candidate}")
    else:
        print(f"⚠️  daily v2.0 JSON 不存在 → 回退 v1.0 stub")


def _check_skip_if_pushed(state: PushState) -> bool:
    """v2.3.4 per-channel skip-if-pushed 检查；返回 True 表示已 skip

    v1.4 旧版：只看 rec['ok'] 顶层字段。
    问题：manual_only 渠道（wecom_test）预发测试 → 写 rec.ok=true → 后续 calendar 任务误判已推。

    v2.3.4 改为 per-channel：
      - rec.channels 中**有非 manual_only 渠道 OK** → skip
      - rec.channels 中**只有 manual_only 渠道 OK**（如 wecom_test 预发） → 视为未推，继续
      - rec 无 channels 字段（v1.0 老 schema） → fallback to rec.ok=true（兼容）
    """
    if not (state.skip_if_pushed and not state.repush):
        return False
    if state.report_type not in ("daily", "weekly", "special"):
        return False
    derived_date = _derive_date_from_path(state.target_file)
    rec = _read_published_record(state.report_type, derived_date)
    if not rec or not rec.get("ok"):
        state.skip_check_reason = "no_record"
        return False

    # rec.channels 可能在 publish_record 嵌套（v2.0）或 顶层（v1.0 / v2.0 兼容层）
    rec_pr = rec.get("publish_record") or {}
    rec_channels = rec_pr.get("channels") if rec_pr else None
    if rec_channels is None:
        rec_channels = rec.get("channels", {}) or {}
    prev_count = (rec_pr.get("push_count", 0) if rec_pr else rec.get("push_count", 0)) or 0

    # 过滤掉 manual_only 渠道 + 'skipped' 标记（这些不影响"应推"判断）
    manual_names = set()
    if state.cfg and "channels" in state.cfg:
        manual_names = {name for name, c in state.cfg.get("channels", {}).items() if c.get("manual_only")}
    auto_channels = {
        k: v for k, v in rec_channels.items()
        if k != "skipped" and k not in manual_names and isinstance(v, dict) and v.get("ok")
    }

    if auto_channels:
        # ✅ 有非 manual_only 渠道推送过且都 ok → skip
        auto_ok_names = sorted(auto_channels.keys())
        print(f"⏭️  已推过（per-channel OK: {','.join(auto_ok_names)}, push_count={prev_count}）: data/published/{state.report_type}/{derived_date}.json")
        print(f"   用 --repush 强制重推，或 --dry-run 干跑")
        state.skipped_pushed = True
        state.push_ok = True
        state.skip_check_reason = "already_pushed_per_channel"
        state.channels_result = {
            "skipped": True,
            "reason": "already_pushed_per_channel",
            "prev_record": f"data/published/{state.report_type}/{derived_date}.json",
            "auto_channels_ok": auto_ok_names,
        }
        return True
    # rec ok=true 但只有 manual_only 渠道 ok（如 wecom_test 预发）→ 视为未推
    print(f"ℹ️  rec ok=true 但非 manual_only 渠道无 OK 记录（仅 manual_only 渠道如 wecom_test 预发）→ 视为未推")
    state.skip_check_reason = "manual_only_only"
    return False


def _dry_run_preview(state: PushState) -> None:
    """v2.0 daily preview：dry-run 也调 renderer 展示"""
    if not (state.daily_v2_json and Path(state.daily_v2_json).exists()):
        return
    daily = _load_daily_v2(state.daily_v2_json)
    if not daily:
        return
    print("\n🔍 v2.0 daily push 预览（不实际发送）:")
    # 飞书 text
    basename = Path(state.target_file).stem
    feishu_text = _render_daily_for_feishu(daily, basename, state.doc_url or "(doc_url 尚未生成)")
    print(f"   [feishu text] ({len(feishu_text)} char):")
    for line in feishu_text.split("\n"):
        print(f"     {line}")
    # 企微 markdown
    wecom_md = _render_daily_for_wecom(daily, basename, state.doc_url or "(doc_url 尚未生成)")
    print(f"\n   [wecom markdown via render_wecom_markdown] ({len(wecom_md)} char):")
    for line in wecom_md.split("\n"):
        print(f"     {line}")


def _create_docx_if_needed(state: PushState) -> None:
    """daily/weekly/special：建飞书 docx + 公开"""
    if state.report_type not in ("daily", "weekly", "special"):
        return
    print("📄 创建飞书云文档...")
    # v1.2.2：手动预发测试支持 docx 标题加 prefix
    doc_url, doc_id = _create_feishu_docx(state.target_file, title_prefix=state.doc_title_prefix)
    if not doc_url:
        state.error_msg = f"创建飞书云文档失败（lark-cli 返回无 url）"
        print(f"❌ {state.error_msg}")
        sys.exit(1)
    state.doc_url = doc_url
    prefix_note = f"（标题前缀='{state.doc_title_prefix}'）" if state.doc_title_prefix else ""
    print(f"✅ 云文档已创建{prefix_note}: {state.doc_url}")
    if doc_id:
        print("🌐 设置公开访问...")
        if _set_docx_public(doc_id):
            print("✅ 公开访问已开启（link_share_entity=anyone_readable）")
        else:
            print("⚠️  公开访问设置失败，文档保持私有不影响推送")


def _push_all_channels(state: PushState, cfg: dict) -> None:
    """逐渠道推送"""
    for name in state.enabled_channels:
        ch_cfg = cfg.get("channels", {}).get(name, {})
        ch_type = ch_cfg.get("type", "")
        pusher = PUSHERS.get(ch_type)
        print(f"\n🚀 渠道: {name} (type={ch_type})")
        if not pusher:
            state.channels_result[name] = {
                "ok": False, "code": -1, "msg": f"no pusher for type '{ch_type}' (PUSHERS dict 未注册)"
            }
            print(f"   ❌ {state.channels_result[name]['msg']}")
            continue
        try:
            r = pusher(ch_cfg, name, state)
        except Exception as e:
            r = {"ok": False, "code": -1, "msg": f"pusher exception: {e}"}
        state.channels_result[name] = r
        status = "✅" if r["ok"] else "❌"
        print(f"   {status} code={r['code']} msg={r['msg']}")
        if r["ok"]:
            state.push_ok = True


# ============== 子命令 ==============

def cmd_normal(args, state: PushState, cfg: dict) -> int:
    """normal mode：推指定报告"""
    if not args.positional:
        # 无参数 → 默认推昨日
        state.target_file = f"每日资讯/{_yesterday_str()}.md"
    else:
        state.target_file = args.positional[0]

    state.report_file = REPO_ROOT / state.target_file
    if not state.report_file.exists():
        state.error_msg = f"报告不存在: {state.report_file}（无参数默认推昨日；指定参数：push-report <report_file>）"
        print(f"❌ {state.error_msg}")
        return 1
    print(f"✅ 报告已就绪: {state.report_file} ({state.report_file.stat().st_size} bytes)")

    state.report_type = _infer_report_type(state.target_file)
    if state.report_type == "unknown":
        state.error_msg = f"未知报告路径: {state.target_file}（支持：每日资讯/ 每周汇总/ 专题/ data/inspections/ test_messages/）"
        print(f"❌ {state.error_msg}")
        return 1
    print(f"🏷️  报告类型: {state.report_type}")

    # v2.0 daily 定位
    _resolve_daily_v2_json(state)

    # skip-if-pushed
    if _check_skip_if_pushed(state):
        return 0

    # 加载 enabled 渠道（v1.2.2 扩展 include_manual/only 参数）
    state.enabled_channels = _select_channels(
        cfg, state.report_type,
        include_manual=state.include_manual,
        only=state.only_channels or None,
    )
    if not state.enabled_channels:
        state.error_msg = f"没有任何渠道匹配 REPORT_TYPE='{state.report_type}'（检查 channels.json）"
        print(f"❌ {state.error_msg}")
        return 1
    print(f"📡 启用渠道: {' '.join(state.enabled_channels)} (REPORT_TYPE={state.report_type})")

    # 设置 log_file
    log_date = _derive_date_from_path(state.target_file) or _today_str()
    state.log_file = REPO_ROOT / "logs" / "daily" / f"{log_date}-push.jsonl"

    # dry-run
    if state.dry_run:
        print("\n===================================")
        print("🧪 DRY-RUN 模式：跳过 docx + webhook 推送（落档 _dryrun/）")
        print("===================================")
        state.doc_url = ""
        _dry_run_preview(state)
        state.channels_result = {
            "feishu": {"ok": True, "code": 0, "msg": "dry-run-simulated"},
            "wecom_official": {"ok": True, "code": 0, "msg": "dry-run-simulated"},
        }
        state.push_ok = True
        return 0

    # 建 docx（如需）
    _create_docx_if_needed(state)

    # 推送
    print("\n===================================")
    print(f"🚀 开始推送 {state.report_type} → {state.target_file}")
    print("===================================")
    _push_all_channels(state, cfg)

    # 汇总
    print(f"\n📊 汇总: {json.dumps(state.channels_result, ensure_ascii=False)}")
    print(f"📊 最终: PUSH_OK={state.push_ok} / 渠道数={len(state.enabled_channels)}")

    if state.push_ok:
        return 0
    state.error_msg = f"所有渠道都失败：{json.dumps(state.channels_result, ensure_ascii=False)}"
    return 1


def cmd_test(args, state: PushState, cfg: dict) -> int:
    """--test 模式：推探针"""
    state.test_mode = True
    state.report_type = "test"
    ts_compact = _now_compact()
    ts_human = _now_human()

    # 落档测试消息
    test_log_dir = Path("/var/log/ai-news/test_messages")
    try:
        test_log_dir.mkdir(parents=True, exist_ok=True)
    except (PermissionError, OSError):
        test_log_dir = Path.home() / ".local" / "share" / "ai-news" / "test_messages"
        test_log_dir.mkdir(parents=True, exist_ok=True)
    state.test_file = str(test_log_dir / f"{ts_compact}-probe.md")
    state.target_file = f"test_messages/probe-{ts_compact}.md"

    targets_desc = " ".join(args.test_targets) if args.test_targets else "all enabled with purpose=test"
    state.test_text = f"🔍 AI资讯渠道探针 {ts_human} — webhook 心跳测试（请忽略）"

    Path(state.test_file).write_text(
        f"# AI 资讯·渠道探针测试消息\n\n"
        f"- 时间: {ts_human} Asia/Shanghai\n"
        f"- 触发: 探活（probe）\n"
        f"- 目标渠道: {targets_desc}\n"
        f"- 内容: {state.test_text}\n"
        f"- 注意: 本文件不入 git 库；正式群（含 official）也会收到，但 message_types=test 默认不污染正式消息\n",
        encoding="utf-8"
    )
    print(f"📝 测试消息已落档: {state.test_file}")

    # 加载 enabled 渠道
    if args.test_targets:
        state.enabled_channels = args.test_targets
    else:
        state.enabled_channels = _select_channels(cfg, "test")
    if not state.enabled_channels:
        state.error_msg = "没有 enabled 且 purpose/message_types 含 'test' 的渠道"
        print(f"❌ {state.error_msg}")
        return 1

    state.log_file = REPO_ROOT / "logs" / "daily" / f"{_today_str()}-push.jsonl"

    # 推送
    print(f"📡 启用渠道: {' '.join(state.enabled_channels)} (REPORT_TYPE=test)")
    _push_all_channels(state, cfg)

    print(f"\n📊 汇总: {json.dumps(state.channels_result, ensure_ascii=False)}")
    if state.push_ok:
        return 0
    return 1


def cmd_list_published(args, state: PushState, cfg: dict) -> int:
    """--list-published：列出已发布记录"""
    from scripts.published_record import list_published
    entries = list_published(args.list_type or "all")
    if not entries:
        print(f"(无 {args.list_type or 'all'} 类型发布记录)")
        return 0
    print(f"{'TYPE':<10} {'DATE':<26} {'PUSH':<5} {'OK':<6} {'CHANNELS_OK':<30} {'LAST_PUSHED':<26} DOC_URL")
    print("-" * 130)
    for e in entries:
        print(f"{e.get('report_type', ''):<10} {e.get('report_date', ''):<26} {e.get('push_count', ''):<5} {str(e.get('ok', '')):<6} {','.join(e.get('channels_ok', [])):<30} {e.get('last_pushed_at', ''):<26} {e.get('doc_url', '') or ''}")
    print(f"\n共 {len(entries)} 条")
    return 0


def cmd_backfill(args, state: PushState, cfg: dict) -> int:
    """--backfill：批量补推"""
    rtype = args.backfill_type
    path_map = {"daily": "每日资讯", "weekly": "每周汇总", "special": "专题"}
    if rtype not in path_map:
        print(f"❌ 不支持的类型: {rtype}（仅 daily/weekly/special）")
        return 1

    since = _dt.date.fromisoformat(args.since)
    until = _dt.date.fromisoformat(args.until)
    plans = []
    d = since
    while d <= until:
        if rtype == "daily":
            label = d.strftime("%Y-%m-%d")
            rel = f"每日资讯/{label}.md"
            pub = REPO_ROOT / "data" / "published" / "daily" / f"{label}.json"
        elif rtype == "weekly":
            iy, iw, _ = d.isocalendar()
            label = f"{iy}-W{iw:02d}"
            rel = f"每周汇总/{label}.md"
            pub = REPO_ROOT / "data" / "published" / "weekly" / f"{label}.json"
        else:
            break
        f = REPO_ROOT / rel
        if f.exists() and (not pub.exists() or state.repush):
            plans.append((rel, label))
        d += _dt.timedelta(days=1)

    print(f"📋 待补推 {len(plans)} 份 {rtype}：")
    for fp, lbl in plans:
        print(f"  - {lbl}: {fp}")
    if not plans:
        print("（无）")
        return 0
    print()

    failed = 0
    for fp, lbl in plans:
        print(f"🚀 推 {lbl} ...")
        cmd = [sys.executable, str(Path(__file__).resolve()), fp]
        if state.repush:
            cmd.append("--repush")
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if r.stdout:
            print(r.stdout[-500:])
        if r.returncode != 0:
            print(f"   ❌ exit={r.returncode}")
            if r.stderr:
                print(f"   stderr: {r.stderr[-300:]}")
            failed += 1
    return 0 if failed == 0 else 1


# ============== 入口 ==============

def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    parser = argparse.ArgumentParser(
        prog="push-report",
        description="AI 资讯追踪·通用推送脚本（v2.1 python 化）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
支持路径 → REPORT_TYPE:
  每日资讯/<date>.md         → daily
  每周汇总/<date>.md         → weekly
  专题/<name>.md             → special
  data/inspections/<file>.md → management
  test_messages/<file>.md    → test

v1.4 去重策略（默认）:
  - 已推过 → 静默跳过（EXIT 0）
  - --repush → 强制重推（push_count+1）
  - --dry-run → 落档 _dryrun/
""",
    )
    parser.add_argument("positional", nargs="*", help="报告路径")
    parser.add_argument("--test", nargs="*", default=None, metavar="CHANNEL", help="推测试/探活")
    parser.add_argument("--repush", action="store_true", help="强制重推")
    parser.add_argument("--skip-if-pushed", action="store_true", default=True, help=argparse.SUPPRESS)
    parser.add_argument("--no-skip-if-pushed", dest="skip_if_pushed", action="store_false", help="不跳过已推过")
    parser.add_argument("--dry-run", action="store_true", help="干跑（落 _dryrun/）")
    parser.add_argument("--pushed-by", default="", help="手动指定 pushed_by")
    parser.add_argument("--list-published", nargs="?", const="all", dest="list_type", metavar="TYPE",
                        help="列出已发布（daily/weekly/special/all）")
    parser.add_argument("--backfill", default="", metavar="TYPE", help="批量补推（daily/weekly/special）")
    parser.add_argument("--since", default="", help="补推起始日 YYYY-MM-DD")
    parser.add_argument("--until", default="", help="补推截止日 YYYY-MM-DD")
    parser.add_argument("--channels-config", default=str(REPO_ROOT / "config" / "channels.json"),
                        help="渠道配置文件路径")
    parser.add_argument("--secrets-file", default=str(REPO_ROOT / ".secrets"),
                        help=".secrets 路径")
    # v1.2.2：手动预发测试参数
    parser.add_argument("--include-manual", action="store_true",
                        help="让 manual_only=true 渠道也参与（手动预发测试用）")
    parser.add_argument("--only", default="", metavar="CHAN1,CHAN2",
                        help="渠道白名单（逗号分隔），只推这些渠道；与 --include-manual 配合限制只推测试群")
    parser.add_argument("--doc-title-prefix", default="", metavar="STR",
                        help="docx 标题前缀（手动预发测试加 [预发] 标记方便区分正式/测试）")
    args = parser.parse_args(argv)

    # 加载 .secrets + channels
    _load_secrets(Path(args.secrets_file))
    cfg = _load_channels(Path(args.channels_config))

    # 状态
    state = PushState(
        test_mode=False,
        repush=args.repush,
        skip_if_pushed=args.skip_if_pushed,
        dry_run=args.dry_run,
        pushed_by_override=args.pushed_by,
        started_at=_now_iso(),
        include_manual=args.include_manual,                                  # v1.2.2
        only_channels=[s.strip() for s in args.only.split(",") if s.strip()],  # v1.2.2
        doc_title_prefix=args.doc_title_prefix,                             # v1.2.2
        cfg=cfg,                                                            # v2.3.4：写 published_record 时剥 manual_only 用
    )

    # 子命令早退
    if args.list_type is not None:
        return cmd_list_published(args, state, cfg)
    if args.backfill:
        if not args.since or not args.until:
            print("❌ --backfill 需配合 --since / --until")
            return 1
        return cmd_backfill(args, state, cfg)

    # 注册 EXIT trap
    _setup_trap(state)

    # 主体
    if args.test is not None:
        args.test_targets = args.test
        return cmd_test(args, state, cfg)
    return cmd_normal(args, state, cfg)


if __name__ == "__main__":
    sys.exit(main())
