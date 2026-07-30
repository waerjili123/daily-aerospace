# 官方搜索线索与弹性核验停止实施计划

日期：2026-07-30  
设计：`docs/superpowers/specs/2026-07-30-official-clue-and-verification-stop-design.md`

## 目标

在不降低严格核验门槛的前提下，让登记投资方别名能够从结构化字段、同页证据、标题和摘要
触发精确官方域名查询；同时修复境内主体证据、运行内连续无新增停止和分配标签。

## 任务 1：确定性官方搜索线索提取

修改：

- `src/laser_space_daily/verification_followup.py`
- `tests/test_verification_followup.py`

步骤：

1. 增加结构化投资方为空、候选摘要含登记别名的失败测试。
2. 增加独立线索提取器，按结构化字段、同页证据、标题和摘要顺序匹配登记别名。
3. 输出匹配域名、别名和命中层，不回写 `AnalysisResult`。
4. 使用匹配域名生成精确 `site:` 查询。
5. 增加未登记名称和近似名称不能触发官方域名的回归。
6. 运行规划器定向测试。

## 任务 2：境内主体逐字证据

修改：

- `src/laser_space_daily/analyzer.py`
- `tests/test_analyzer.py`
- `tests/test_fetch_verify.py`（仅在端到端核验需要时）

步骤：

1. 增加“北京光邮星空科技有限公司”页面样本。
2. 让规则分析器选择包含境内地名和主体名称的最短原文片段作为 `in_china` 证据。
3. 确保 `ResilientAnalyzer` 只在主分析与规则分析结论一致时补入该证据。
4. 断言搜索摘要或跨页面文本不能参与补证。
5. 运行分析器和核验器定向测试。

## 任务 3：运行内连续无新增停止

修改：

- `src/laser_space_daily/verification_followup.py`
- `src/laser_space_daily/pipeline.py`
- `tests/test_verification_followup.py`
- `tests/test_pipeline.py`

步骤：

1. 为规划器增加每个事件级 `target_key` 的运行内连续无新增输入。
2. 搜索新增独立 URL 时归零，无新增时加一。
3. 达到配置阈值后停止该目标，并尝试转移给其他合格事件。
4. 没有其他事件时提前结束弹性循环，保留未用预算。
5. 将运行内结果正确合并到持久化 `PendingItem`。
6. 覆盖“连续两次无新增止于 2”“新增后归零”“预算转移”测试。

## 任务 4：分配标签与研究轨迹

修改：

- `src/laser_space_daily/verification_followup.py`
- `src/laser_space_daily/pipeline.py`
- `tests/test_pipeline.py`
- `tests/test_report_notifier.py`（仅在报告字段变化时）

步骤：

1. 只有实际切换到未查询事件时使用 `cover_distinct_target`。
2. 同事件后续查询使用 `retry_same_target`。
3. 首次官方域名优先使用 `official_source_match`。
4. 研究轨迹记录别名命中层、命中域名和停止原因。
5. 保持既有 trace 字段向后兼容。

## 任务 5：完整验证与文档

修改：

- `docs/PROGRESS.md`

步骤：

1. 运行所有定向测试。
2. 运行完整离线测试。
3. 运行 `git diff --check`。
4. 确认 workflow 仍仅支持手动触发并强制 `--dry-run`。
5. 更新进度文档，记录 2026-07-30 Artifact 和本轮根因修复。
6. 提交并推送当前开发分支。
7. 下一次真实 dry-run 由用户手动触发。

## 验收

- 光邮星空结构化投资方为空但摘要含“中关村科学城”时生成
  `site:zgccity.com`。
- 标题和摘要只影响查询，不改变事实或核验状态。
- 同页“北京光邮星空科技有限公司”形成有效 `in_china` 引文。
- 单目标连续两次无新增时只调用 2 次弹性搜索。
- 实际切换和同目标重试标签准确。
- 基础不超过 12、弹性不超过 3、总计不超过 15。
- 严格来源和字段门槛保持不变。
