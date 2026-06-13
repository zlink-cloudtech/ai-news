# Runway Research

## 基本信息
- **板块**：🧍 数字人
- **URL**：https://runwayml.com/research
- **抓取方式**：L5 网页抓取（trafilatura / readability-lxml）
- **更新频率**：1+ 篇/月
- **内容覆盖**：Gen-3 / Gen-4 视频模型研究、图像/视频生成论文

## 接入状态
- **状态**：🟡 待对接
- **优先级**：P0
- **首期目标**：✅ 是（数字人板块必接入，无 RSS 替代源）
- **负责人**：AI 调研专家
- **创建日期**：2026-06-13

## 接入计划
- [ ] 抓取脚本开发（trafilatura + cloudscraper）
- [ ] "昨日窗口"过滤（基于页面发布时间）
- [ ] 内容质量验证（相关性 + 去广告）
- [ ] 7 天稳定性验证
- [ ] 监控页面结构变化（中等反爬风险）

## 抓取示例

```python
import trafilatura
downloaded = trafilatura.fetch_url("https://runwayml.com/research")
text = trafilatura.extract(downloaded)
```

## 备注
- **无官方 RSS**，必须 L5 抓取
- 页面结构可能变化，需监控 + 自动告警
- 抓取频率建议 ≤ 4 次/日（避免被反爬）
- 备用方案：如抓取持续失败，考虑订阅 Newsletter（如果存在）
