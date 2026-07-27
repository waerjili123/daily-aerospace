# 一次性钉钉试发设计

日期：2026-07-27（北京时间）

## 目标

使用现有 GitHub Secrets 完成一次真实采集和钉钉试发，验证“有信息时能否把候选内容正确推送到钉钉”。没有已核实事件时，允许发送通过搜索门槛的候选，但必须明确标注“未核实”。

## 已确认前提

- 博查真实响应解析已经恢复，最近一次真实运行取得 40 条原始结果、10 条主题相关结果和 5 条最终候选。
- 5 条最终候选此前因展示缺陷未出现在日报中；提交 `18296de` 已修复该问题。
- 本机没有 `BOCHA_API_KEY`、`DEEPSEEK_API_KEY`、`DINGTALK_WEBHOOK` 和 `DINGTALK_SECRET`。
- 四项凭据已配置为 GitHub Secrets，本次不读取、不修改 Secrets。
- 当前 workflow 只有手动入口，强制 dry-run，没有 schedule。

## 方案

创建一次性分支 `codex/dingtalk-test-20260727`，基于
`codex/information-availability` 的最新提交。

该分支中的 workflow：

- 只保留 `workflow_dispatch`，不增加 schedule；
- 保持最多 4 次博查查询；
- 使用现有 `DEEPSEEK_API_KEY`、`BOCHA_API_KEY`、`DINGTALK_WEBHOOK` 和 `DINGTALK_SECRET` Secrets；
- 仅在这个名称明确的一次性分支中移除 `--dry-run`；
- 继续上传报告与数据 Artifact；
- 保持 `contents: read`，不允许 workflow 写回仓库；
- 不修改 main、PR #4 或 PR #5，不合并任何 PR。

## 数据流

1. 用户在 Actions 页面选择一次性测试分支并手动运行。
2. 流水线执行最多 4 次博查查询。
3. 确定性门槛筛选近期相关候选。
4. 正文抓取、AI 分析和规则核验照常执行。
5. 已核实内容进入正式板块。
6. 未进入正式数据或待核实池的最终搜索候选进入“今日重点跟进”，标注“搜索候选（未核实）”。
7. 完整日报发送到钉钉，同时保存 Artifact。

## 安全边界

- 本设计只授权一次钉钉试发，不授权恢复定时发送。
- 不修改、导出或显示任何 Secret。
- 不降低 TLS 校验。
- 不修改仓库可见性。
- 不合并 PR #4 或 PR #5。
- 不把未核实候选描述为已核实事件。
- 不以 Actions 的绿色状态代替业务验收。

## 验收标准

技术验收：

- workflow 由 `workflow_dispatch` 触发；
- 使用一次性测试分支的最新提交；
- 流程成功生成 Artifact；
- 钉钉发送步骤没有认证或签名错误。

业务验收：

- 钉钉实际收到日报；
- 若本次存在最终候选，消息中至少展示一条候选；
- 每条候选包含板块、来源日期或“发布日期未知”、标题和原始链接；
- 候选明确标注“未核实”；
- Artifact 的候选数量和钉钉展示内容可以相互核对；
- 明显无关的打印机、美容、通用医疗用品等噪声不应进入展示候选。

## 失败处理

- 如果采集 API 失败或最终候选为 0，保留 Artifact 并报告失败原因，不将绿色 Actions 状态视为成功。
- 如果钉钉认证、签名或发送失败，保留日志中的错误类型，但不得输出 Secret。
- 如果消息为空或候选再次丢失，停止后续正式发送，继续修复展示链路。
- 如果出现明显噪声，先调整筛选规则，不启用 schedule。

## 试发后的收口

试发完成并取得 Artifact 后：

1. 核对钉钉消息与 Artifact；
2. 把一次性测试分支的 workflow 恢复为强制 dry-run；
3. 保持 main、PR 合并状态、Secrets、仓库可见性和 schedule 不变；
4. 将结果和后续建议写入 `docs/PROGRESS.md`。
