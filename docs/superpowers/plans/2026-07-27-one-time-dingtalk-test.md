# 一次性钉钉试发实施计划

日期：2026-07-27（北京时间）

对应设计：
`docs/superpowers/specs/2026-07-27-one-time-dingtalk-test-design.md`

## 步骤 1：固化设计与基线

- 在 `codex/information-availability` 保存设计和实施计划。
- 确认完整离线测试通过。
- 推送文档提交到现有开发分支。

## 步骤 2：创建隔离的一次性分支

- 从开发分支最新提交创建 `codex/dingtalk-test-20260727`。
- 不修改 main、PR #4 或 PR #5 的合并状态。

## 步骤 3：把一次性分支改为真实发送

修改 `.github/workflows/daily-intelligence.yml`：

- 保持唯一触发器为 `workflow_dispatch`；
- 保持查询选择范围为 1–4，默认 4；
- 把占位钉钉变量替换为现有 GitHub Secrets 引用；
- 从流水线命令中移除 `--dry-run`；
- 保持 `contents: read` 和 Artifact 上传。

同步修改 workflow 配置测试，明确断言：

- 不存在 schedule；
- 使用四项 Secrets；
- 命令不包含 `--dry-run`；
- 最大查询次数仍为 4；
- 仓库权限仍为只读。

## 步骤 4：本地验证

- 运行 workflow 配置测试；
- 运行完整离线测试；
- 运行 `git diff --check`；
- 确认没有 Secret 值写入文件；
- 确认一次性分支工作区干净。

## 步骤 5：推送但不触发

- 提交一次性 workflow 修改；
- 推送 `codex/dingtalk-test-20260727`；
- 不创建或合并 PR；
- 不自动触发 Actions。

## 步骤 6：用户手动试发

用户在 GitHub Actions 页面：

1. 选择 `Daily Laser and Space Intelligence`；
2. 点击 `Run workflow`；
3. 分支选择 `codex/dingtalk-test-20260727`；
4. `max_queries` 选择 `4`；
5. 确认运行。

## 步骤 7：业务验收与收口

- 核对运行提交 SHA、触发方式、查询数和结论；
- 下载并审核 Artifact；
- 用户确认钉钉实际收到的消息；
- 核对候选板块、日期、标题、链接和“未核实”标签；
- 把一次性分支恢复为强制 dry-run 并推送；
- 更新 `docs/PROGRESS.md`；
- 保持 schedule 关闭，不合并 PR。
