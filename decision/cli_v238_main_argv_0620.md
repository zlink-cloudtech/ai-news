# v2.3.8 cli.py run-generate-daily 入口修复（6-20 1:00 实战）

## 背景

6-20 1:00 calendar 任务（uid `e05992f0-..._split_7652527833813729582`）触发，预期跑昨日（6-19）日报。命令 `python3 -m scripts.cli run-generate-daily` 跑挂：

```
TypeError: main() takes 0 positional arguments but 1 was given
```

## 根因

cli.py v2.1 commit `fa20dfb`（6-17）引入 `run-generate-daily` 子命令，调用方式：

```python
_ra_main(["--date", target_date])     # cli.py 第 213 行
_gd_main(gen_args)                    # cli.py 第 222 行
```

但 `run_all.main()` 和 `generate_daily.main()` 都是 `def main() -> int`（无参数），内部用 `args = parser.parse_args()`（默认从 sys.argv 解析）。cli.py 传 list → TypeError。

## 影响范围

v2.1（6-17）→ v2.3.7（6-19 14:56）整个过程 cli.py run-generate-daily 入口**从未跑通**：

| 时间 | 事件 | 实际效果 |
|------|------|----------|
| 6-19 1:00 | calendar 抓取任务（6-18 报告） | 跑挂（import 漏修） |
| 6-19 11:00 | v2.3.6 commit cdb79b7 修 import | 写"入口跑通"但**没真跑过** cli.py，main 签名仍错 |
| 6-19 8:30 | 推送任务（6-18 报告） | 6-18 报告是 6-19 11:00 手工 backfill 生成（走 subprocess `python3 -m scripts.generators.generate_daily`），不依赖 cli.py |

**结论**：cli.py run-generate-daily 入口从 v2.1 引入起就是"死代码"，calendar 任务一直跑挂，只因推送任务只看 .md（不依赖 cli.py）才没暴露。

## 修复

v2.3.8 commit `1fb59a2`（feature 分支）+ merge `3d7c352`（main）+ tag `v2.3.8`：

**2 个文件 / 4 处改动**（最小侵入）：

```python
# scripts/crawlers/run_all.py
-def main() -> int:
+def main(argv: list[str] | None = None) -> int:
     ...
-    args = parser.parse_args()
+    args = parser.parse_args(argv)

# scripts/generators/generate_daily.py
-def main():
+def main(argv: list[str] | None = None):
     ...
-    args = parser.parse_args()
+    args = parser.parse_args(argv)
```

cli.py 调用方式 `_ra_main(["--date", date])` / `_gd_main(gen_args)` 已正确，无需改。

## 验证（v1.3 §10.3 抓 dry-run + 主 agent 自检）

### 1. Mock 验证（直接 import + 传 argv）
- `run_all.main(['--date', '2026-06-19'])` → rc=0（8 源全 OK）
- `generate_daily.main(['--date', '2026-06-19', '--no-llm'])` → rc=0（13 条 / 2 板块）

### 2. Dry-run（带 LLM）
`python3 -m scripts.cli run-generate-daily 2026-06-19 --no-commit`
- 抓取 8 源全 OK
- jiqizhixin 0 候选（连续第 3 天：6-17/6-18/6-19，达到 v2.3.2 升级阈值，单独议题）
- LLM 14 次调用成功（deepseek-v4-flash）
- 每日资讯/2026-06-19.md 15566 bytes / 13 条 / 2 板块
- data/published/daily/2026-06-19.json schema 2.0 / 13 条

### 3. 正式跑（带 commit + push）
`python3 -m scripts.cli run-generate-daily`
- 抓取 8 源 crawl_36kr FAIL(1)（RSS 解析 mismatched tag，fallback 走旧 raw json）
- LLM 14 次调用（**14 次缓存命中**——dry-run 调过的 LLM 缓存生效，零成本）
- 每日资讯/2026-06-19.md 15614 bytes / 13 条 / 2 板块
- commit `e1f80c5` `feat(每日资讯): 2026-06-19 重新生成`（含 每日资讯/2026-06-19.md）
- push 远端成功（origin/main = e1f80c5）

## v1.3 §10 流程合规

5 步标准流程全走：
1. ✅ `feature/v2.3.8-cli-main-argv` 分支（基于 main HEAD a8beb28）
2. ✅ 分支上 commit 1fb59a2（4 处改动）
3. ✅ 预发测试：跳过（不涉及 push 链路，按 §10.3 抓 dry-run + 主 agent 自检）
4. ✅ `--no-ff` merge → main 3d7c352（保留 feature commit + merge commit 双痕迹）
5. ✅ tag v2.3.8 → push 远端 → 本地 + 远端 feature 分支删除

## 异常 / 遗留

### 1. crawl_36kr 失败（FAIL(1)）
- 错误：`RSS 解析失败: https://36kr.com/feed -> mismatched tag`（XML 解析异常，可能是 RSS feed 格式变更或网络瞬时问题）
- 处理：cli.py run_generate_daily 走 fallback 用已有 `data/raw/36kr/2026-06-19.json`（dry-run 抓的 9 条）继续生成
- **影响**：本次 6-19 报告 36kr 板块数据正常（dry-run 抓的数据已落 raw json）；但**下次抓取 6-20 报告时仍可能 FAIL**
- **处置**：下次 1:00 抓取观察；若连续多次 FAIL，主人拍板修复（临时改 RSS 源 / 抓 sitemap 备份 / 加 retry）

### 2. jiqizhixin 连续 3 天 0 候选
- 6-17 / 6-18 / 6-19 凌晨 1:00 抓取均 0 候选（sitemap regenerate 时机约束）
- v2.3.2 决策："连续 3 天 0 才升级 v2"——**已达成升级阈值**
- **处置**：建议下个迭代（不是 1:00 calendar 任务范围）开 v2.3.9 议题：在 1:00 calendar 命令加 `--retry-on-zero`（v2.3.2 已实现，等主人拍板启用）

### 3. 6-17 raw json 8 个 untracked
- 历史遗留：6-17 1:00 抓取任务抓的数据未 commit（commit_and_push 只 add report_file）
- 与 6-19 报告无关，**不处置**（等下次手工清理或主人拍板）

### 4. data/articles/2026-06-19 5 个 untracked
- 本次抓全文生成的中间产物（36kr 2 + qbitai 3）
- commit_and_push 只 add report_file，**不 commit**
- 与 6-19 报告无关，**不处置**

## 关联

- 决策 `cli_v238_main_argv_0620.md`（本文）
- 决策 `jiqizhixin_v2_retry_on_zero_0619.md`（v2.3.2 已实现 `--retry-on-zero`，等主人拍板启用）
- 决策 `git_sandbox_truncate_recovery_0619.md`（本次任务初始撞 truncate 3 文件，按 SOP `git checkout HEAD -- <file>` 拉回）
- 决策 `ai_news_v1_3_dev_flow_spec.md`（v1.3 §10 流程规范）
- 决策 `ai_news_daily_v2_json_first.md`（v2.0 daily JSON 先行）
- 决策 `ai_news_v21_sandbox_uv_incompat_0618.md`（v2.1 沙箱 uv sync 兼容问题）

## tag 远端状态

```
v2.3.1 ~ v2.3.8 全部 tag 远端 OK
main HEAD = e1f80c5 feat(每日资讯): 2026-06-19 重新生成
         = 3d7c352 Merge feature/v2.3.8-cli-main-argv
         = 1fb59a2 fix(cli): v2.3.8 run_all/generate_daily main 接受 argv 参数
```
