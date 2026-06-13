# Replicate Blog

## 基本信息
- **板块**：🧍 数字人（横跨：多模态、Agent）
- **URL**：https://replicate.com/blog/rss
- **抓取方式**：L1 官方 RSS
- **更新频率**：1-2 篇/周
- **内容覆盖**：数千个开源模型聚合（视频/图像/语音/多模态），是数字人/多模态板块重要代理源

## 接入状态
- **状态**：🟡 待对接
- **优先级**：P0
- **首期目标**：✅ 是（数字人板块唯一带 RSS 的源）
- **负责人**：AI 调研专家
- **创建日期**：2026-06-13
- **接入日期**：2026-06-13（已跑通，122 条解析成功）
- **实际抓取脚本**：`scripts/crawlers/crawl_replicate.py`

## 历史变更
- 2026-06-13：原计划 URL `https://replicate.com/blog/rss.xml` 实测 404；正确路径是 `https://replicate.com/blog/rss`（页面 `/blog` 上有 "RSS" 链接按钮）。

## 接入计划
- [ ] 验证 RSS 路径有效
- [ ] 编写抓取脚本
- [ ] "昨日窗口"过滤
- [ ] 内容质量验证
- [ ] 7 天稳定性验证

## 抓取示例

```bash
curl -s https://replicate.com/blog/rss | head -50
```

## 备注
- 数字人板块厂商普遍**无官方 RSS**（Runway / HeyGen / Synthesia / Pika / Luma / ElevenLabs 等都没有）
- Replicate Blog 是该板块目前**唯一稳定的 RSS 源**，可作为代理源
- 配合 L5 网页抓取补 Runway Research（首期另一个目标）
