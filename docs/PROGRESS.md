# 激光与商业航天情报日报项目进度

更新时间：2026-07-27（北京时间）

## 当前结论

```text
博查真实响应解析：已修复并合并到 main
真实候选采集：已恢复，最近一次安全运行返回 39 条
候选可见性：PR #4 已创建，尚未合并
“有无信息”PRD：已确认
“有无信息”开发：本地完成
离线测试：353 passed
新版本真实 dry-run：尚未执行
钉钉正式发送：未执行
定时任务：当前未启用 schedule
仓库可见性：public，未修改
```

## 已确认事实

- 2026-07-25、2026-07-26 的 scheduled Actions 在北京时间约 15:30、15:58 运行，可能已发送空钉钉日报。
- 目标时间为北京时间 07:30，但历史实际触发时间不符合目标。
- Actions 显示 success 只代表进程成功，不代表采集业务成功。
- 博查真实结果位于 `data.webPages.value`。
- PR #3 已合并，合并提交为 `eef32c8`。
- 当前主 workflow 只保留手动入口，强制 dry-run，不包含 schedule，不提交运行状态，不发送钉钉。
- 最近一次安全验证运行使用 4 个核心查询，返回 39 条候选、5 条待核实、0 条已核实，报告能够展示标题、摘要和链接。
- 候选正文失败的主要外部原因包括人机验证、TLS 证书链错误和 HTTP 521。
- 仓库当前为 public。

## 已完成的安全验证

### 采集恢复

- [x] 修复博查包装响应解析。
- [x] 检查博查 HTTP 业务错误码。
- [x] 保持 4 查询上限。
- [x] 保持 `dry_run=true`。
- [x] 完成三次一次性安全验证。
- [x] 确认真实博查候选不再为 0。
- [x] 确认不关闭 TLS 校验。

### 候选可见性

- [x] `PendingItem` 保留搜索摘要。
- [x] 正文抓取失败后展示候选标题、摘要和原始链接。
- [x] 报告数据完整性增加候选数量。
- [x] 本地完整测试通过。
- [x] 创建 PR #4。
- [ ] PR #4 尚未合并。

## 本轮“有无信息”开发

设计文档：

- `docs/superpowers/specs/2026-07-27-information-availability-prd.md`
- `docs/superpowers/plans/2026-07-27-information-availability.md`

当前开发分支：

```text
codex/information-availability
```

已实现：

- [x] 四个核心查询增加明确分类提示。
- [x] 商业航天融资查询移除采购、招标和中标词。
- [x] 博查 `freshness` 从 `noLimit` 调整为 `oneMonth`。
- [x] 解析并独立保存 `datePublished`。
- [x] 无时区、无效或缺失日期不再伪装为当天日期。
- [x] 增加标题、摘要和 HTTP(S) URL 结构门槛。
- [x] 增加四板块确定性主题锚点。
- [x] 商业航天融资使用“航天主体＋融资事件”双锚点。
- [x] 排除打印机、硒鼓、墨盒、雕刻、切割、美容、医美、通用医疗用品和无关 AI 软件采购。
- [x] 增加 0–7 天、8–30 天和日期未知分桶。
- [x] 近 7 天不足 5 条时才用 8–30 天补足。
- [x] 日期未知候选最多补充 2 条。
- [x] 最终候选最多 10 条。
- [x] 搜索候选在正文抓取前完成过滤。
- [x] 正文抓取失败保留板块、来源日期、摘要和链接。
- [x] 报告展示近 7 天、8–30 天补充和日期未知标签。
- [x] 报告增加采集漏斗和“信息可用/信息不足”判定。
- [x] 新增 21 条固定样本，覆盖近期、补充、未知日期、旧闻、噪声和重复 URL。

采集漏斗字段：

```text
raw_search_count
valid_shape_count
relevance_pass_count
recent_7d_count
fallback_8_30d_count
unknown_date_count
final_candidate_count
fetch_failure_count
information_available
```

## 测试结果

```text
发现层与模型测试：70 passed
发现/流水线/报告/固定样本定向测试：158 passed
完整离线测试：353 passed
git diff --check：通过
```

尚未验证：

- [ ] 博查真实 API 是否接受 `freshness=oneMonth`。
- [ ] 真实 4 查询能否稳定得到至少 5 条最终候选。
- [ ] 真实候选中打印机、美容等明显噪声是否为 0。
- [ ] 新报告的真实 Artifact 是否符合阅读预期。
- [ ] 真实 DeepSeek 分析与核验率。

## 当前 GitHub 状态

```text
origin/main：eef32c8
PR #3：merged
PR #4：https://github.com/waerjili123/daily-aerospace/pull/4
PR #4 状态：open / not merged
PR #4 分支：codex/pending-candidate-report

本地开发分支：codex/information-availability
本地开发基线：包含 PR #4 提交 c150877
```

## 下一步

需要用户另行确认后才能执行：

1. 推送 `codex/information-availability` 并创建开发 PR；
2. 使用现有安全验证方式运行一次 4 查询、强制 dry-run 的真实采集；
3. 下载并人工审核 Artifact；
4. 根据真实结果调整关键词和时间门槛；
5. 再决定 PR #4 与开发 PR 的合并顺序。

以下事项仍未授权：

- 不合并 PR #4；
- 不执行 `dry_run=false`；
- 不发送钉钉；
- 不修改 Secrets；
- 不修改仓库可见性；
- 不恢复或启用 schedule；
- 不暂停或启用 workflow。
