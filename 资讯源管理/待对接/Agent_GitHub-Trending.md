# GitHub Trending

## 基本信息
- **板块**：💻 编程 Agent（横跨：开源 LLM 数字人）
- **URL**：https://github.com/trending
- **抓取方式**：L3 RSSHub `/github/trending/daily`
- **更新频率**：实时
- **内容覆盖**：热门仓库（按语言/时间范围筛选 AI/ML 相关）

## 接入状态
- **状态**：🟡 待对接
- **优先级**：P0
- **首期目标**：✅ 是
- **负责人**：AI 调研专家
- **创建日期**：2026-06-13

## 接入计划
- [ ] 部署 RSSHub（docker compose）
- [ ] 验证 `/github/trending/daily` 路由可访问
- [ ] 编写抓取脚本
- [ ] 过滤 AI/ML 相关仓库
- [ ] 7 天稳定性验证

## 抓取示例

```bash
# RSSHub 部署后
curl -s "https://<rsshub-host>/github/trending/daily" | head -50

# 筛选 Python
curl -s "https://<rsshub-host>/github/trending/daily/python"
```

## 备注
- **前置条件**：RSSHub 已部署（首期 1.5h 任务之一）
- 推荐每日抓取，按 Stars 增量排序
- 配合 LLM 摘要生成"昨日 AI 开源热点"
