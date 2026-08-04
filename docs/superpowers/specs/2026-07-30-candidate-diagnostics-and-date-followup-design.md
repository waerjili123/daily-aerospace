# 候选诊断与发布日期定向核验设计

日期：2026-07-30
状态：已获用户书面复核确认
开发分支：`codex/verification-promotion-20260728`

## 1. 背景

提交 `3ca57e6` 后的真实 dry-run 运行 #22 使用了正确分支和最新提交，但 Artifact
仍显示：

- 基础检索 12 次，弹性核验 0 次；
- 博查原始 97、主题相关 10、最终候选 4；
- 严格已核实 0、待核实 2；
- 两条 pending 均为微光启航，原因是
  `missing_required_fields:published_at`；
- 光邮星空腾讯新闻出现在报告候选区，但没有进入 pending，Artifact 无法说明它在
  抓取、分析还是核验阶段被排除；
- 每次 Actions dry-run 从仓库内空状态启动，上一轮 Artifact 的 pending 不会自动成为
  下一轮输入。

当前问题包含两个独立但相关的缺口：

1. 网页缺发布日期时不会启动弹性检索，预留预算继续闲置；
2. 最终候选没有完整的阶段诊断，无法解释“展示了但未进入 pending”。

## 2. 目标

1. 允许 `missing_required_fields:published_at` 进入弹性核验。
2. 只使用候选发布日期判断 90 天检索资格，不把它当成已核实发布日期。
3. 为每条进入抓取链路的最终候选输出精简、结构化的生命周期诊断。
4. 让 Artifact 明确解释候选是否进入弹性核验，以及未进入的具体原因。
5. 保持基础 12、弹性最多 3、总上限 15。
6. 不降低现有来源、正文和逐字证据门槛。

## 3. 非目标与安全边界

- 本轮不实现跨 Actions 的 pending 状态持久化。
- 不把搜索结果发布日期写入 `AnalysisResult.published_at`。
- 不把 URL 中的日期、HTTP 头时间或抓取时间当作网页发布日期。
- 不因多篇文章都缺日期而推定事件日期。
- 不保存网页正文、模型原始输出、提示词、Secrets 或 webhook。
- 不修改仓库可见性、workflow 启停或定时配置。
- 不设置 `dry_run=false`，不发送钉钉，不合并 PR。

## 4. 方案选择

采用独立 `candidate-diagnostics.json` 和日期缺口定向核验。

未采用：

- 仅把原因加入允许列表并复用 research trace：检索轨迹与候选生命周期职责混杂，
  后续难以稳定比较。
- 同时实现跨运行状态持久化：涉及可信状态来源、冲突合并和恢复策略，超出本轮范围。

## 5. 组件设计

### 5.1 候选诊断模型

新增精简诊断记录，字段如下：

- `source_url`
- `title`
- `discovery_source`
- `selected_for_report`
- `category_hint`
- `stage`
- `status`
- `reason`
- `source_grade`
- `missing_fields`
- `elastic_eligible`
- `elastic_ineligible_reason`
- `elastic_attempted`
- `elastic_not_attempted_reason`

枚举语义：

- `stage`：`fetch`、`analysis`、`verification`、`persisted`
- `status`：`failed`、`rejected`、`pending`、`verified`

诊断只保存状态和原因，不保存正文或完整证据。记录按规范化 URL 去重；若同一 URL
经历多个阶段，保留最终阶段，并保留是否尝试过弹性核验。

`selected_for_report` 区分报告中的最终候选与旁路佐证、官方种子、历史核验池或弹性新增
来源。`elastic_eligible` 记录首次资格判断；如果候选从未具备资格，才填写
`elastic_ineligible_reason`。尝试后的无新增停止原因继续记录在 research trace，避免覆盖
候选最初为何能够或不能进入弹性核验。

### 5.2 生命周期采集

候选进入流水线后：

1. 抓取失败：记录 `stage=fetch`、`status=failed` 和安全的错误类型。
2. 分析失败：记录 `stage=analysis`、`status=failed` 和安全的错误类型。
3. 核验拒绝：记录 `stage=verification`、`status=rejected` 和精确原因。
4. 核验待核实：记录 `status=pending`、原因、缺失字段和弹性资格。
5. 核验通过：记录 `status=verified`；若 payload 后置校验失败，更新为对应 pending。
6. 成功落入 event、financing 或 pending 状态后，阶段更新为 `persisted`。

诊断不改变原处理分支，也不能把被拒候选放入 pending。

### 5.3 弹性资格解释

把规划器的资格判断拆成可解释结果，至少覆盖：

- `eligible`
- `status_not_pending`
- `reason_not_supported`
- `classification_incomplete`
- `organization_missing`
- `published_at_outside_pool`
- `no_new_source_threshold`
- `query_exhausted`

流水线使用同一资格函数规划查询和写诊断，避免“诊断说可用但规划器拒绝”的分叉。

### 5.4 发布日期缺口

支持的原因仅为精确的：

`missing_required_fields:published_at`

若还同时缺主体、类别或事件类型，则不具备弹性资格。候选必须满足：

- 境内、范围、类别和事件类型结论完整；
- 主体存在；
- 候选 `source_published_at` 或已有分析日期位于 90 天池内；
- 未达到连续无新增阈值。

候选发布日期只参与滚动池判断和查询排序，不写回分析结果或核验事实。

### 5.5 日期定向查询

融资日期缺口查询增加：

- `发布日期`
- `发布时间`
- `公告时间`
- `官方披露`

查询仍按以下顺序：

1. 已登记官方企业或投资方域名；
2. 公司、投资方或机构官方披露；
3. 独立媒体交叉来源。

查询结果进入现有抓取、分析和严格核验链路。只有新页面正文或标题中存在可被日期规则
逐字识别的完整年月日，才能形成 `published_at` 事实和证据。

### 5.6 Artifact 输出

`RunResult` 增加候选诊断列表。CLI 使用与 research trace 相同的原子写入模式，输出：

`data/candidate-diagnostics.json`

workflow 已上传整个 `data/` 目录，因此无需修改 Artifact 上传范围。

输出顺序固定为规范化 URL，JSON 使用 UTF-8 和稳定缩进，便于两次运行直接比较。

## 6. 数据流

1. 基础发现与候选筛选产生最终候选。
2. 流水线为每个候选创建初始诊断槽位。
3. 抓取、分析和首次严格核验逐步更新诊断。
4. 资格函数返回是否可弹性核验和拒绝原因。
5. 合格的日期缺口目标按逐次预算执行搜索。
6. 新来源重新进入抓取、分析和严格核验。
7. 最终候选的状态、原因、缺失字段和弹性结果写入诊断列表。
8. CLI 原子写入报告、research trace 和 candidate diagnostics。

## 7. 错误与降级

- 候选抓取/分析异常：记录异常类型，不记录异常消息中的页面内容或凭据。
- 诊断字段无法计算：使用 `reason=diagnostic_unavailable`，不影响主流水线结果。
- 日期查询无结果：计入连续无新增，保持 pending。
- 搜索 API 失败：记录现有失败原因，不产生日期事实。
- 新页面仍只有“近日”：保持 `missing_required_fields:published_at`。
- 候选诊断写入失败：CLI 按流水线输出失败处理，避免生成不完整 Artifact。

## 8. 测试

### 8.1 日期资格

- 仅缺 `published_at` 的融资候选可以进入弹性核验。
- 同时缺主体或类别时不可进入。
- 候选发布日期只影响 90 天资格，不写回分析发布日期。
- 超过 90 天或未来日期不可进入。
- 连续无新增阈值继续生效。

### 8.2 查询

- 日期缺口查询包含发布日期、发布时间、公告时间和官方披露。
- 已登记官方域名仍优先生成 `site:`。
- 新来源正文没有完整日期时不能晋升。
- 搜索摘要日期不能成为核验证据。

### 8.3 诊断

- 抓取、分析、拒绝、待核实、已核实均输出对应阶段和状态。
- 光邮星空未进入 pending 时仍能看到精确拒绝原因。
- 微光启航显示 `missing_fields=["published_at"]` 和弹性资格。
- 未执行弹性时必须说明未进入原因。
- 诊断不包含正文、模型输出、Secrets 或 webhook。

### 8.4 回归

- 基础不超过 12、弹性不超过 3、总计不超过 15。
- 连续无新增停止、目标切换和 pending 更新保持正确。
- 单一 B 级来源、C 级转载和搜索摘要不能晋升。
- 完整离线测试和 `git diff --check` 通过。
- workflow 仍仅手动触发并强制 `--dry-run`。

## 9. 验收标准

代码验收：

- 两条微光启航日期缺口可以消耗弹性预算寻找带网页日期的新来源；
- 候选日期不会写入分析事实；
- 每条最终候选均有精简诊断；
- 光邮星空即使被拒也能在 Artifact 中看到阶段和原因；
- `candidate-diagnostics.json` 不包含正文或敏感信息；
- 所有预算与严格核验边界保持不变。

真实运行验收：

- 弹性调用不超过 3，总调用不超过 15；
- Artifact 同时包含 research trace 和 candidate diagnostics；
- 日期缺口查询和结果可追踪；
- 找不到网页日期时仍保持待核实；
- 目标仍为至少 1 条符合既有严格标准的 `verified`；
- Actions success 不能替代业务验收。
