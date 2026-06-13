# DeepMind Blog

## 基本信息
- **板块**：🧠 LLM 发展
- **URL**：https://deepmind.google/blog/rss.xml
- **抓取方式**：L1 官方 RSS
- **更新频率**：1-3 篇/周
- **内容覆盖**：Gemini 系列、Veo、AlphaFold、Genie 等

## 接入状态
- **状态**：🟡 待对接
- **优先级**：P0
- **首期目标**：✅ 是
- **负责人**：AI 调研专家
- **创建日期**：2026-06-13

## 接入计划
- [ ] 验证 RSS 路径有效
- [ ] 编写抓取脚本
- [ ] "昨日窗口"过滤
- [ ] 内容质量验证
- [ ] 7 天稳定性验证

## 抓取示例

```bash
curl -s https://deepmind.google/blog/rss.xml | head -50
```

## 备注
- 与 Google AI Blog 内容部分重叠（Google AI 包含 DeepMind 动态）
- 可与 Google AI Blog 做去重
