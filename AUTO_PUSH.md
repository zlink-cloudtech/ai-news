# 自动 push 机制说明

每次在该仓库内执行 `git commit` 后，post-commit hook 会自动执行 `git push origin main`，无需人工干预。

## 配套脚本

- `scripts/news-commit.sh`：便捷脚本，封装"添加文件 + 提交 + push"流程
- 后续每日/每周资讯生成后，只需调用该脚本即可

## 凭据管理

- remote URL 已嵌入 GitHub PAT（fine-grained token，权限：Contents Read/Write，仅限 ai-news 仓库）
- 撤销方式：GitHub → Settings → Developer settings → Personal access tokens → Revoke
- 重新生成后只需更新 remote URL 即可

## Hook 文件

`.git/hooks/post-commit` —— 注意 hooks 目录不会被 git 跟踪，所以新克隆的仓库需要重新配置
