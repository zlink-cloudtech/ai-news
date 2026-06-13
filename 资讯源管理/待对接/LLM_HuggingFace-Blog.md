# Hugging Face Blog

## 基本信息
- **板块**：🧠 LLM 发展（横跨多板块：数字人、Agent）
- **URL**：https://huggingface.co/blog
- **抓取方式**：L1 官方 RSS
- **更新频率**：3-5 篇/周
- **内容覆盖**：开源模型/数据集/教程，覆盖 LLM、多模态、Agent 全栈

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
curl -s https://huggingface.co/blog/feed.xml | head -50
```

## 备注
- 内容跨多个话题板块，需做内容分类（LLM/Agent/数字人）
- HF Daily Papers 是单独的源（数据库型），不要混淆
