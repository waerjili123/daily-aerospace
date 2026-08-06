# GitHub 原生定时调度重新登记设计

## 问题

2026-08-06 北京时间 07:50 和 08:20，GitHub 没有创建任何 `schedule` 运行。原
workflow 虽为 `active` 且默认分支包含 cron，但 GitHub 的实际调度记录仍停留在
2026-07-26。采集、分析和钉钉投递均未开始。

## 方案

新建独立的 `.github/workflows/daily-scheduler.yml`，只负责两个原生 cron：

- `50 23 * * *`：北京时间 07:50 主任务；
- `20 0 * * *`：北京时间 08:20 兜底。

新 workflow 通过 `workflow_call` 复用现有 `daily-intelligence.yml`，固定传入
`daily + 20 + dingtalk_live` 并继承现有 Secrets。原 workflow 移除 `schedule`，继续
保留手动入口和安全默认 `dry_run`，避免同一份业务流水线被两个 GitHub cron 同时登记。

## 去重

投递门禁改为查询仓库全部 Actions 运行，而不是只查询旧 workflow 的运行。这样新
scheduler 的 `scheduled-live` 和原 workflow 的 `manual-live` 都参与同日去重：首次正式
投递成功后，08:20 兜底在安装依赖、采集和发送前跳过；失败或取消不阻止兜底。

## 验证

- 静态测试验证旧 workflow 仅包含 `workflow_dispatch + workflow_call`；
- 静态测试验证新 scheduler 只包含两个 cron、`run-name=scheduled-live`、固定生产参数和
  `secrets: inherit`；
- 门禁测试验证其查询仓库级运行列表；
- 完整测试和 `git diff --check` 通过后直接推送 `main`；
- GitHub API 必须返回新 workflow 独立 ID、状态 `active`。

不增加 Codex、Windows 或第三方定时任务，不修改 Secrets、仓库可见性和报告核实标准。
