# OpenAI Blog

## 基本信息
- **板块**：🧠 LLM 发展
- **URL**：https://openai.com/blog/rss.xml
- **抓取方式**：L1 官方 RSS
- **更新频率**：3-5 篇/周
- **内容覆盖**：GPT/o 系列、Sora、Operator、Agents SDK、ChatGPT 产品更新

## 接入状态
- **状态**：🟡 待对接
- **优先级**：P0
- **首期目标**：✅ 是
- **负责人**：AI 调研专家
- **创建日期**：2026-06-13

## 接入计划
- [ ] 验证 RSS 路径有效（curl 测试）
- [ ] 编写抓取脚本（feedparser）
- [ ] 实现"昨日窗口"过滤
- [ ] 验证内容质量（相关性）
- [ ] 连续运行 7 天稳定性验证

## 抓取示例

```bash
curl -s https://openai.com/blog/rss.xml | head -50
```

## 备注
- RSS 路径是 OpenAI 官方提供，稳定性高
- 建议抓取后用 LLM 做摘要+分类
- 时区注意：RSS 中 pubDate 通常是 UTC，需要转换为北京时间
