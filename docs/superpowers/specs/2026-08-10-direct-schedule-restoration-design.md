# 直接定时触发恢复设计

## 背景

独立的 `.github/workflows/daily-scheduler.yml` 于 2026-08-06 新建，用两个
GitHub 原生 cron 调用可复用的 `daily-intelligence.yml`。真实记录显示，该入口在
2026-08-07 至 2026-08-09 连续数日延迟数小时才创建运行，不能满足北京时间早晨送达
的业务要求。下游采集、分析和钉钉投递通常只需数分钟，延迟发生在新 scheduler 的
运行创建阶段。

## 方案选择

考虑三个方案：

1. 继续使用独立 scheduler 并增加 cron 次数：仍共享同一异常入口，不能解决准时性。
2. 恢复长期存在的 `daily-intelligence.yml` 直接接收 schedule：减少一层新建 wrapper，
   沿用已有 workflow 身份和执行链路。选择此方案。
3. 使用外部定时器触发 `workflow_dispatch`：可形成真正独立的故障域，但需要新增外部
   平台和凭据，不在本次改动范围内。

## 目标结构

- 删除 `.github/workflows/daily-scheduler.yml`。
- `daily-intelligence.yml` 保留 `workflow_dispatch`，并直接增加两个 schedule：
  - UTC `23:50`，即北京时间次日 `07:50` 主触发；
  - UTC `00:20`，即北京时间 `08:20` 备用触发。
- 不再需要 `workflow_call`；该工作流只承担手动运行和自身定时运行。
- 定时事件固定使用：
  - `discovery_mode=daily`；
  - `max_queries=20`；
  - `delivery_mode=dingtalk_live`。
- 手动事件继续读取用户输入，并保持 `delivery_mode=dry_run` 为默认值。
- 运行标题：定时为 `scheduled-live`；手动按输入显示 `manual-live`、`manual-test` 或
  `manual-dry-run`。

## 参数解析

工作流在表达式层生成三个有效参数：

- `EFFECTIVE_DISCOVERY_MODE`
- `EFFECTIVE_MAX_QUERIES`
- `EFFECTIVE_DELIVERY_MODE`

当 `github.event_name == 'schedule'` 时使用固定生产值；否则使用
`workflow_dispatch` 输入。分支保护、钉钉 Secret 注入、CLI 参数和投递保护全部只读取
有效参数，避免定时事件因没有 `inputs` 而意外落入 dry-run 或空值。

## 安全与去重

- 不修改任何 Secret 值、仓库可见性或 Actions 总开关。
- 手动入口默认不发送钉钉。
- 定时入口才自动使用现有钉钉 Secrets。
- 现有 `delivery_guard` 和仓库级同日去重保持不变；主触发成功后，备用触发跳过采集和
  投递。
- `concurrency` 继续禁止同一日报流程并发执行。

## 验证

1. 静态测试确认 `daily-intelligence.yml` 同时包含 `workflow_dispatch` 和两个 schedule。
2. 静态测试确认手动默认 `dry_run`，定时固定 `dingtalk_live`。
3. 静态测试确认所有关键步骤使用有效参数，不直接误用缺失的定时 inputs。
4. 静态测试确认独立 scheduler 文件已删除。
5. 运行完整测试和 YAML 解析检查。
6. 推送 `main` 后只手动执行一次 `dry_run` 验证新版本，不额外发送钉钉。
7. 次日核对 07:50/08:20 的实际 schedule 创建时间和钉钉投递状态。

## 成功标准

- 手动运行默认仍为 dry-run。
- 定时运行不需要人工输入并可正式投递。
- 同一天最多发送一条正式日报。
- 直接 schedule 至少完成一次真实准时性验证；在验证前不宣称问题已经彻底解决。
