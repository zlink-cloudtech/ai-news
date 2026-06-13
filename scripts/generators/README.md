# 每日资讯生成器

读 `data/raw/<source>/<date>.json` → 按话题板块归类 → 评分排序 → 渲染模板 → 落盘到 `每日资讯/<date>.md`

## 文件结构

```
scripts/generators/
├── __init__.py
├── _utils_gen.py          # 共用工具：源→板块映射、评分、评级、影响分析生成
├── generate_daily.py      # 入口：扫描 + 渲染 + 落盘
└── README.md
```

## 用法

```bash
# 生成指定日期的每日资讯（推荐，CI/cron 用）
python3 scripts/generators/generate_daily.py --date 2026-06-12

# 默认日期 = 昨天（Asia/Shanghai）
python3 scripts/generators/generate_daily.py

# 干跑（只打印到 stdout，不落盘）— 调试用
python3 scripts/generators/generate_daily.py --date 2026-06-12 --dry-run
```

## 评分规则

每条 item 在 0-100 区间打分，由三部分组成：

| 维度     | 来源               | 范围  |
|----------|--------------------|-------|
| 源权重   | `SOURCE_WEIGHT`    | 0-30  |
| 关键词命中 | `KEYWORD_BOOST`    | 0-60+ |
| 内容厚度 | summary 长度 / 标签数 | 0-13  |

## 评级阈值

| 分数区间 | 等级 | 视觉 |
|----------|------|------|
| ≥ 60     | 高优 | 🔴   |
| 30-59    | 中等 | 🟡   |
| < 30     | 一般 | 🟢   |

## 板块映射

| 板块 | 默认源 |
|------|--------|
| 🧠 LLM 发展 | OpenAI / DeepMind / HuggingFace / Anthropic |
| 💻 编程 Agent | LangChain / GitHub Trending / Cursor / Devin |
| 🧍 数字人 | Replicate / Runway / HeyGen / Synthesia |
| 📦 其他 | （兜底，新源未分类时归这里） |

新增源时：
1. 在 `资讯源管理/源清单.md` 加源
2. 在 `_utils_gen.py::SOURCE_TO_BOARD` 加映射
3. 在 `_utils_gen.py::SOURCE_WEIGHT` 加权重

## 影响分析

`generate_impact()` 基于标题 / 摘要 / 标签的关键词匹配，从 `IMPACT_TEMPLATES` 中挑选 1-2 条最相关的分析文本。
**当前是模板化版本**——只命中关键词，不会做深度语义判断。
后续可接入 LLM 做精细化生成。

## 报告结构

输出 Markdown 包含 8 个区块：
1. 标题（带日期）
2. 数据范围说明
3. 📊 昨日速览（板块分布 + 主题词 + 等级分布）
4. 一句话总结（动态生成，指向 🔴 头条）
5. 4 个话题板块的 Top N 详情
6. 💡 跨话题洞察（基于实际命中的模板）
7. 📌 待跟进（指向 🔴 头条）
8. 📎 信息源记录（命中 + 0 命中都列）

## 主题词提取

`extract_keywords()` 统计板块内所有 item 的 `matched_tags` 频次，取 Top 4 作为板块主题词。
这些是评分时实际命中的关键词，不引入额外 LLM 判断。

## 测试 / 调试

```bash
# 跑某天并查看 Markdown
python3 scripts/generators/generate_daily.py --date 2026-06-12 --dry-run | less

# 验证某板块是否被正确归类（直接调用 _utils_gen）
python3 -c "from scripts.generators._utils_gen import classify_board, score_item; print(classify_board('openai')); print(score_item({'source':'openai','title':'GPT-5 launch','summary':'','tags':[]}, '2026-06-12'))"
```

## 后续演进

- [ ] 接入 LLM 做"核心内容"精炼（目前直接复用 RSS summary）
- [ ] 接入 LLM 做"跨话题洞察"（目前用模板）
- [ ] 支持 `--window` 自定义窗口（默认 = 昨日 00:00-23:59 CST）
- [ ] 支持多日合并（生成跨日汇总）
