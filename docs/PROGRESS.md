# 项目进度

更新时间：2026-07-27（北京时间）

详细上下文见：[HANDOFF-2026-07-27.md](HANDOFF-2026-07-27.md)。

## 当前总体状态

```text
代码主体：已实现
离线测试：通过
GitHub Secrets：已配置
首次真实 dry-run：程序成功、业务失败
博查根因：已定位
博查修复：PR #3 已完成但未合并
真实数据采集验收：未通过
DeepSeek 真实验收：未完成
正式钉钉验收：未完成
定时运行：已经自动运行，但时间和内容均有问题
服务器迁移：未开始
```

## 已完成

- [x] 从丢失/损坏的原项目状态恢复代码并建立 Git 历史。
- [x] PR #1 合并恢复版项目。
- [x] 项目内容与“AI日报”隔离。
- [x] 实现四个板块的数据模型和报告结构。
- [x] 实现采购全生命周期串联和去重规则。
- [x] 实现滚动三个月项目池。
- [x] 实现 JSON/JSONL 状态持久化。
- [x] 实现 Markdown 日报。
- [x] 实现 GitHub Actions 手动和定时运行。
- [x] 将 Tavily 替换为博查 Web Search API。
- [x] 实现钉钉加签。
- [x] PR #2 合并到 `main`。
- [x] 配置四个 GitHub Repository Secrets。
- [x] 完成第一次 `workflow_dispatch`、`dry_run=true`。
- [x] 下载并检查第一次 Artifact。
- [x] 确认第一次 Artifact 业务数据全部为空。
- [x] 确认博查资源包已生效、API Key 可用。
- [x] 用单次脱敏诊断确认博查真实结构为 `data.webPages.value`。
- [x] 完成兼容修复及 345 项测试。
- [x] 创建 PR #3。
- [x] 确认 PR #3 截至 2026-07-27 尚未合并。
- [x] 确认 7 月 25、26 日 scheduled workflow 已自动运行并提交空日报。
- [x] 确认仓库当前为 public。

## 正在等待决策

- [ ] 是否立即暂停/禁用 scheduled workflow，避免继续发空日报和消耗博查额度。
- [ ] 是否把 GitHub 仓库从 public 改为 private。
- [ ] 是否允许更新并合并 PR #3。

## 修复后必须完成

- [ ] 从最新 `origin/main` 更新 PR #3 分支。
- [ ] 合并 PR #3；不要合并诊断分支。
- [ ] 把验收搜索数量临时限制到很小，避免重复消耗约 15 次。
- [ ] 重新手动运行 `dry_run=true`。
- [ ] 下载并审核新 Artifact。
- [ ] 确认博查候选数大于 0。
- [ ] 确认不再出现“博查 API 返回格式异常”。
- [ ] 确认 DeepSeek 对真实候选完成分析，或输出明确的安全降级原因。
- [ ] 确认原始链接可点击并能被抓取。
- [ ] 确认正式事件和 `pending` 的分流正确。
- [ ] 解决或接受四个官方来源在 GitHub runner 上访问失败的问题。
- [ ] 调查 scheduled run 实际发生在北京时间约 15:30 的原因。
- [ ] 验证每天北京时间 07:30 触发。
- [ ] 用户人工审核日报内容。
- [ ] 仅在人工审核通过后运行一次 `dry_run=false`。
- [ ] 验收钉钉只收到一条完整 Markdown，链接能直接跳转。
- [ ] 再决定是否开启长期自动推送。

## 当前 Git 与 GitHub 状态

```text
远端 main：c35e11e
PR #3 分支：codex/bocha-response-fix
PR #3 提交：f76fb90
PR #3 URL：https://github.com/waerjili123/daily-aerospace/pull/3
PR #3：open / not merged

诊断分支：codex/bocha-response-diagnostic
诊断结果提交：9f86dab
诊断分支：不要合并

交接分支：codex/handoff-20260727
```

## 重要纠正

- Actions 显示 success，只表示进程和步骤没有失败，不代表采集到了有效数据。
- 第一次 dry-run 没有验证 DeepSeek 的真实处理能力，因为候选数为 0。
- 7 月 25、26 日 scheduled run 仍生成空日报；按代码和成功状态判断，很可能已经发到钉钉。
- 工作流目标是北京时间 07:30，但实际 API 时间对应北京时间约 15:30/15:57，尚未验收。
- README 预期私有仓库，但仓库当前实际是 public。

