#!/usr/bin/env python3
"""
AI 资讯追踪·发布记录工具（v1.4 引入）
==================================

被 scripts/push_report.sh 的 EXIT trap 自动调用，记录每次发布的详情。
也提供 CLI 供手动查询/清理/列出。

用法：
    python3 scripts/published_record.py record --type daily --file 每日资讯/2026-06-15.md \
        --channels '{"feishu":{"ok":true}}' --doc-url https://... \
        --pushed-by calendar:8:30 [--dry-run]

    python3 scripts/published_record.py status daily 2026-06-15
    python3 scripts/published_record.py list [daily|weekly|special|all]
    python3 scripts/published_record.py cleanup [--days 90]

设计：
- data/published/<type>/<date>.json（详细）
- data/published/index.json（索引：最近 90 天摘要）
- data/published/_dryrun/（干跑记录，不入主索引）
- raw_summary 自动扫 data/raw/<src>/<date>.json 统计
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
PUB_DIR = REPO_ROOT / "data" / "published"
RAW_DIR = REPO_ROOT / "data" / "raw"


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def today_str() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")


# ============== 工具函数 ==============

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def collect_raw_summary(date_str: str) -> dict:
    """扫 data/raw/<src>/<date>.json 统计各源抓取条数"""
    if not RAW_DIR.exists():
        return {"sources": {}, "total_articles": 0, "sources_with_zero": []}
    sources: dict[str, int] = {}
    for src_dir in sorted(RAW_DIR.iterdir()):
        if not src_dir.is_dir():
            continue
        raw_file = src_dir / f"{date_str}.json"
        if not raw_file.exists():
            sources[src_dir.name] = 0
            continue
        try:
            data = json.loads(raw_file.read_text(encoding="utf-8"))
            # 兼容多种 schema：顶层 list / {"items": [...]} / {"articles": [...]} / {"data": [...]}
            if isinstance(data, list):
                count = len(data)
            elif isinstance(data, dict):
                for key in ("items", "articles", "data", "results"):
                    if key in data and isinstance(data[key], list):
                        count = len(data[key])
                        break
                else:
                    count = data.get("count", 0)
            else:
                count = 0
            sources[src_dir.name] = int(count)
        except Exception:
            sources[src_dir.name] = -1  # -1 表示解析失败
    zero_sources = sorted([k for k, v in sources.items() if v == 0])
    return {
        "sources": sources,
        "total_articles": sum(v for v in sources.values() if v > 0),
        "sources_with_zero": zero_sources,
    }


def derive_report_date(report_type: str, report_file: str) -> str:
    """从报告类型 + 文件名推导 report_date"""
    basename = Path(report_file).stem
    if report_type == "daily":
        # 每日资讯/2026-06-15.md → 2026-06-15
        return basename
    if report_type == "weekly":
        # 周报/2026-W24.md 或 每周汇总/2026-W24.md → 2026-W24
        return basename
    if report_type == "special":
        # 专题/2026-06-foundation-models.md → 2026-06-foundation-models
        return basename
    return basename


# ============== 记录写入 ==============

def _record_path(report_type: str, report_date: str, dry_run: bool = False) -> Path:
    if dry_run:
        return PUB_DIR / "_dryrun" / f"{report_date}.json"
    return PUB_DIR / report_type / f"{report_date}.json"


def write_record(
    report_type: str,
    report_file: str,
    channels_result: dict,
    doc_url: str | None,
    pushed_by: str,
    ok: bool,
    error: str | None = None,
    dry_run: bool = False,
    report_date: str | None = None,
    report_sha256: str | None = None,
    report_size_bytes: int | None = None,
    raw_summary: dict | None = None,
    daily_payload: dict | None = None,  # v2.0 新增：daily 完整内容（由 _daily_payload.build_daily_payload 返回）
) -> dict:
    """写一条发布记录；返回写出的 record dict

    模式：
      - v1.0 模式（daily_payload=None）：保持原行为，schema_version="1.0"，字段在顶层
      - v2.0 模式（daily_payload 提供）：schema_version="2.0"，字段嵌套 publish_record
        + v1.0 → v2.0 迁移时自动从顶层字段构造 publish_record

    v1.0 → v2.0 迁移路径：
      - 已有 v1.0 daily JSON（顶层 first_pushed_at/doc_url/...）→ 提供 daily_payload
        → 自动从 v1.0 顶层字段构造 publish_record 嵌套 → 写 v2.0 JSON
    """
    if report_date is None:
        report_date = derive_report_date(report_type, report_file)

    # 自动算 sha256 + size（如未提供）
    abs_path = REPO_ROOT / report_file
    if abs_path.exists():
        if report_sha256 is None:
            report_sha256 = sha256_file(abs_path)
        if report_size_bytes is None:
            report_size_bytes = abs_path.stat().st_size

    # 自动算 raw_summary（如未提供，且 v1.0 模式）
    if raw_summary is None and report_type == "daily" and daily_payload is None:
        try:
            dt = datetime.strptime(report_date, "%Y-%m-%d")
            raw_summary = collect_raw_summary(report_date)
        except ValueError:
            raw_summary = None

    # 读已有记录（如有）
    rec_path = _record_path(report_type, report_date, dry_run=dry_run)
    existing = None
    if rec_path.exists():
        try:
            existing = json.loads(rec_path.read_text(encoding="utf-8"))
        except Exception:
            existing = None

    now = now_iso()

    if daily_payload is not None:
        # ============== v2.0 模式 ==============
        # 以 daily_payload 为基，添加兼容字段和 publish_record 嵌套
        record = dict(daily_payload)  # 复制 v2.0 全部字段
        # v1.0 兼容字段（保留供 v1.0 兼容层读）
        record["report_file"] = report_file
        if report_sha256:
            record["report_sha256"] = report_sha256
        if report_size_bytes is not None:
            record["report_size_bytes"] = report_size_bytes
        # raw_summary 保留为兼容字段（v1.0 + v3 PM 用）
        if raw_summary is not None:
            record["raw_summary"] = raw_summary
        elif "raw_summary" not in record and report_type == "daily":
            try:
                dt = datetime.strptime(report_date, "%Y-%m-%d")
                record["raw_summary"] = collect_raw_summary(report_date)
            except ValueError:
                pass

        # 构造 publish_record（嵌套 v1.0 顶层字段）
        if existing and "publish_record" in existing:
            # 已有 v2.0 publish_record → 增量更新（保留 first_pushed_at）
            old_pr = existing["publish_record"]
            sha_changed = (report_sha256 is not None
                           and old_pr.get("report_sha256") != report_sha256)
            record["publish_record"] = {
                "first_pushed_at": old_pr.get("first_pushed_at", now),
                "last_pushed_at": now,
                "push_count": int(old_pr.get("push_count", 0)) + 1,
                "pushed_by": pushed_by,
                "doc_url": doc_url or old_pr.get("doc_url"),
                "ok": ok,
                "channels": channels_result,
                "error": error or "",
                "report_sha256": report_sha256 or old_pr.get("report_sha256"),
                "_sha_changed_since_first_push": bool(sha_changed),
            }
        elif existing and ("first_pushed_at" in existing or "doc_url" in existing):
            # v1.0 → v2.0 迁移：从 v1.0 顶层字段构造 publish_record
            old_doc_url = existing.get("doc_url") or doc_url
            old_first = existing.get("first_pushed_at", now)
            old_last = existing.get("last_pushed_at", old_first)
            old_count = int(existing.get("push_count", 1))
            old_channels = existing.get("channels") or channels_result
            old_pushed_by = existing.get("pushed_by", pushed_by)
            old_error = existing.get("error", "")
            old_sha = existing.get("report_sha256") or report_sha256
            sha_changed = (report_sha256 is not None
                           and old_sha is not None
                           and report_sha256 != old_sha)
            record["publish_record"] = {
                "first_pushed_at": old_first,
                "last_pushed_at": now,
                "push_count": old_count + 1,
                "pushed_by": pushed_by,
                "doc_url": old_doc_url,
                "ok": ok,
                "channels": channels_result or old_channels,
                "error": error or old_error,
                "report_sha256": report_sha256 or old_sha,
                "_sha_changed_since_first_push": bool(sha_changed),
            }
        else:
            # 全新 v2.0 daily JSON
            record["publish_record"] = {
                "first_pushed_at": now,
                "last_pushed_at": now,
                "push_count": 1,
                "pushed_by": pushed_by,
                "doc_url": doc_url,
                "ok": ok,
                "channels": channels_result,
                "error": error or "",
                "report_sha256": report_sha256,
                "_sha_changed_since_first_push": False,
            }
        # 顶层兼容字段（v1.0 风格的 publish_count / first_pushed_at 等也保留）
        record["first_pushed_at"] = record["publish_record"]["first_pushed_at"]
        record["last_pushed_at"] = record["publish_record"]["last_pushed_at"]
        record["push_count"] = record["publish_record"]["push_count"]
        record["pushed_by"] = record["publish_record"]["pushed_by"]
        record["doc_url"] = record["publish_record"]["doc_url"]
        record["ok"] = record["publish_record"]["ok"]
        record["channels"] = record["publish_record"]["channels"]
        record["error"] = record["publish_record"]["error"]

    elif existing is None:
        # ============== v1.0 模式（首次写入）==============
        record = {
            "schema_version": "1.0",
            "report_type": report_type,
            "report_date": report_date,
            "report_file": report_file,
            "report_sha256": report_sha256,
            "report_size_bytes": report_size_bytes,
            "first_pushed_at": now,
            "last_pushed_at": now,
            "push_count": 1,
            "pushed_by": pushed_by,
            "doc_url": doc_url,
            "ok": ok,
            "channels": channels_result,
            "raw_summary": raw_summary,
            "error": error,
        }
    else:
        # ============== v1.0 模式（更新已有）==============
        sha_changed = (report_sha256 is not None and existing.get("report_sha256") != report_sha256)
        record = dict(existing)
        record["last_pushed_at"] = now
        record["push_count"] = int(existing.get("push_count", 0)) + 1
        record["pushed_by"] = pushed_by
        record["doc_url"] = doc_url or existing.get("doc_url")
        record["ok"] = ok
        record["channels"] = channels_result
        record["error"] = error
        record["raw_summary"] = raw_summary if raw_summary is not None else existing.get("raw_summary")
        if report_sha256:
            record["report_sha256"] = report_sha256
        if report_size_bytes is not None:
            record["report_size_bytes"] = report_size_bytes
        record["_sha_changed_since_first_push"] = bool(sha_changed)

    rec_path.parent.mkdir(parents=True, exist_ok=True)
    rec_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

    # 返回 record 时附带 _path 字段（v2.0 调用方如 _daily_payload.py CLI 调试用）
    record["_path"] = str(rec_path.relative_to(REPO_ROOT))

    # 更新 index.json（dry_run 不入主索引）
    if not dry_run:
        _update_index(record)

    return record


def _update_index(record: dict) -> None:
    index_path = PUB_DIR / "index.json"
    if index_path.exists():
        try:
            idx = json.loads(index_path.read_text(encoding="utf-8"))
        except Exception:
            idx = {"schema_version": "1.0", "entries": []}
    else:
        idx = {"schema_version": "1.0", "entries": []}

    channels_ok = [k for k, v in record.get("channels", {}).items() if v.get("ok")]
    summary = {
        "report_type": record["report_type"],
        "report_date": record["report_date"],
        "last_pushed_at": record["last_pushed_at"],
        "push_count": record["push_count"],
        "ok": record["ok"],
        "channels_ok": channels_ok,
        "doc_url": record.get("doc_url"),
    }

    # 移除同 (type, date) 旧条目
    idx["entries"] = [
        e for e in idx.get("entries", [])
        if not (e.get("report_type") == record["report_type"]
                and e.get("report_date") == record["report_date"])
    ]
    idx["entries"].append(summary)
    # 按 last_pushed_at 倒序
    idx["entries"].sort(key=lambda e: e.get("last_pushed_at", ""), reverse=True)
    idx["generated_at"] = now_iso()
    index_path.write_text(json.dumps(idx, ensure_ascii=False, indent=2), encoding="utf-8")


# ============== 查询 ==============

def get_status(report_type: str, report_date: str) -> dict | None:
    rec_path = PUB_DIR / report_type / f"{report_date}.json"
    if not rec_path.exists():
        return None
    return json.loads(rec_path.read_text(encoding="utf-8"))


def list_published(report_type: str = "all") -> list[dict]:
    """从 index.json 列已发布；index 缺失时回退扫文件"""
    index_path = PUB_DIR / "index.json"
    if index_path.exists():
        try:
            idx = json.loads(index_path.read_text(encoding="utf-8"))
            entries = idx.get("entries", [])
            if report_type == "all":
                return entries
            return [e for e in entries if e.get("report_type") == report_type]
        except Exception:
            pass
    # 回退：扫文件
    out: list[dict] = []
    types = ["daily", "weekly", "special"] if report_type == "all" else [report_type]
    for t in types:
        d = PUB_DIR / t
        if not d.exists():
            continue
        for f in sorted(d.glob("*.json")):
            try:
                rec = json.loads(f.read_text(encoding="utf-8"))
                out.append({
                    "report_type": rec.get("report_type"),
                    "report_date": rec.get("report_date"),
                    "last_pushed_at": rec.get("last_pushed_at"),
                    "push_count": rec.get("push_count"),
                    "ok": rec.get("ok"),
                    "channels_ok": [k for k, v in rec.get("channels", {}).items() if v.get("ok")],
                    "doc_url": rec.get("doc_url"),
                })
            except Exception:
                continue
    out.sort(key=lambda e: e.get("last_pushed_at", ""), reverse=True)
    return out


# ============== 清理 ==============

def cleanup(days: int = 90) -> dict:
    """清理 N 天前的发布记录（详细 + 索引同步）"""
    cutoff = datetime.now(TZ) - timedelta(days=days)
    cutoff_iso = cutoff.isoformat(timespec="seconds")

    removed = []
    for t in ("daily", "weekly", "special"):
        d = PUB_DIR / t
        if not d.exists():
            continue
        for f in d.glob("*.json"):
            try:
                rec = json.loads(f.read_text(encoding="utf-8"))
                if rec.get("last_pushed_at", "") < cutoff_iso:
                    f.unlink()
                    removed.append(str(f.relative_to(REPO_ROOT)))
            except Exception:
                continue

    # 同步清理 index
    index_path = PUB_DIR / "index.json"
    if index_path.exists():
        try:
            idx = json.loads(index_path.read_text(encoding="utf-8"))
            before = len(idx.get("entries", []))
            idx["entries"] = [e for e in idx.get("entries", []) if e.get("last_pushed_at", "") >= cutoff_iso]
            after = len(idx["entries"])
            idx["generated_at"] = now_iso()
            index_path.write_text(json.dumps(idx, ensure_ascii=False, indent=2), encoding="utf-8")
            index_removed = before - after
        except Exception:
            index_removed = 0
    else:
        index_removed = 0

    return {"removed_files": removed, "removed_index_entries": index_removed, "cutoff": cutoff_iso}


# ============== CLI ==============

def cmd_record(args) -> int:
    try:
        channels = json.loads(args.channels) if args.channels else {}
    except json.JSONDecodeError as e:
        print(f"❌ --channels JSON 解析失败: {e}", file=sys.stderr)
        return 2

    raw_summary = None
    daily_payload = None
    if args.raw_summary:
        try:
            raw_summary = json.loads(args.raw_summary)
        except json.JSONDecodeError as e:
            print(f"❌ --raw-summary JSON 解析失败: {e}", file=sys.stderr)
            return 2
    if args.daily_json:
        # v2.0 daily JSON 路径：读入后作为 daily_payload 传入 write_record
        try:
            daily_payload = json.loads(Path(args.daily_json).read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError) as e:
            print(f"❌ --daily-json 读取/解析失败: {e}", file=sys.stderr)
            return 2

    record = write_record(
        report_type=args.type,
        report_file=args.file,
        channels_result=channels,
        doc_url=args.doc_url,
        pushed_by=args.pushed_by,
        ok=args.ok,
        error=args.error,
        dry_run=args.dry_run,
        raw_summary=raw_summary,
        daily_payload=daily_payload,
    )
    out_path = _record_path(args.type, record["report_date"], dry_run=args.dry_run)
    print(f"✅ 记录已写: {out_path.relative_to(REPO_ROOT)}")
    print(f"   report_type={args.type} report_date={record['report_date']} ok={args.ok} push_count={record['push_count']}")
    return 0


def cmd_status(args) -> int:
    rec = get_status(args.type, args.date)
    if rec is None:
        print(f"❌ 未找到 {args.type}/{args.date}.json", file=sys.stderr)
        return 1
    print(json.dumps(rec, ensure_ascii=False, indent=2))
    return 0


def cmd_list(args) -> int:
    entries = list_published(args.type)
    if not entries:
        print(f"(无 {args.type} 类型发布记录)")
        return 0
    print(f"{'TYPE':<10} {'DATE':<26} {'PUSH':<5} {'OK':<6} {'CHANNELS_OK':<30} {'LAST_PUSHED':<26} DOC_URL")
    print("-" * 130)
    for e in entries:
        print(f"{e.get('report_type',''):<10} {e.get('report_date',''):<26} {e.get('push_count',''):<5} {str(e.get('ok','')):<6} {','.join(e.get('channels_ok', [])):<30} {e.get('last_pushed_at',''):<26} {e.get('doc_url','') or ''}")
    print(f"\n共 {len(entries)} 条")
    return 0


def cmd_cleanup(args) -> int:
    res = cleanup(args.days)
    print(f"🧹 清理完成（> {args.days} 天）")
    print(f"   cutoff: {res['cutoff']}")
    print(f"   删除文件: {len(res['removed_files'])}")
    print(f"   删除索引: {res['removed_index_entries']}")
    if res['removed_files']:
        for f in res['removed_files'][:10]:
            print(f"     - {f}")
        if len(res['removed_files']) > 10:
            print(f"     ... 共 {len(res['removed_files'])} 条")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="AI 资讯追踪·发布记录工具（v1.4）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_rec = sub.add_parser("record", help="写一条发布记录（被 push_report.sh EXIT trap 调用）")
    p_rec.add_argument("--type", required=True, choices=["daily", "weekly", "special", "management", "test"])
    p_rec.add_argument("--file", required=True, help="报告相对仓库根的路径")
    p_rec.add_argument("--channels", default="{}", help="JSON：各渠道结果")
    p_rec.add_argument("--doc-url", default=None, help="飞书云文档 URL")
    p_rec.add_argument("--pushed-by", required=True, help="推送来源：calendar:8:30 / manual / backfill / dry-run ...")
    p_rec.add_argument("--ok", type=lambda v: v.lower() in ("1", "true", "yes"), required=True)
    p_rec.add_argument("--error", default=None)
    p_rec.add_argument("--raw-summary", default=None, help="JSON：raw 源统计（不传则自动算 daily）")
    p_rec.add_argument("--daily-json", default=None, help="v2.0 daily JSON 路径（daily 类型专用；传入则启用 v2.0 模式 + 嵌套 publish_record）")
    p_rec.add_argument("--dry-run", action="store_true", help="落档 _dryrun/，不入主索引")
    p_rec.set_defaults(func=cmd_record)

    p_st = sub.add_parser("status", help="查询某日发布状态")
    p_st.add_argument("type", choices=["daily", "weekly", "special", "management", "test"])
    p_st.add_argument("date", help="daily=YYYY-MM-DD；weekly=YYYY-Www；special=name")
    p_st.set_defaults(func=cmd_status)

    p_ls = sub.add_parser("list", help="列出已发布记录")
    p_ls.add_argument("type", nargs="?", default="all", choices=["daily", "weekly", "special", "all"])
    p_ls.set_defaults(func=cmd_list)

    p_cl = sub.add_parser("cleanup", help="清理 N 天外的发布记录")
    p_cl.add_argument("--days", type=int, default=90, help="保留天数（默认 90）")
    p_cl.set_defaults(func=cmd_cleanup)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
