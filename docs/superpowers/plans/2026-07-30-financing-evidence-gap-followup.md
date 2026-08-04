# 融资证据缺口定向核验实施计划

日期：2026-07-30
设计：`docs/superpowers/specs/2026-07-30-financing-evidence-gap-followup-design.md`

## 目标

在不改变严格核验结论的前提下，让
`financing_missing_required_evidence` 进入弹性核验，输出具体证据缺口并生成
字段定向查询；同时修正同页多个境内机构时的融资企业主体优先级。

## 任务 1：确定性融资证据缺口

修改：

- `src/laser_space_daily/verifier.py`
- `tests/test_fetch_verify.py`

步骤：

1. 增加无副作用的融资证据缺口函数。
2. 完全复用现有必填条件，区分主体、日期、金额、轮次、融资子类型和投资方缺口。
3. 明确金额存在、明确未披露、单纯省略三种情况。
4. 断言缺口检查不会改变核验状态或分析结果。
5. 运行核验器定向测试。

## 任务 2：弹性资格与字段定向查询

修改：

- `src/laser_space_daily/verification_followup.py`
- `tests/test_verification_followup.py`

步骤：

1. 将 `financing_missing_required_evidence` 加入核验允许原因。
2. 在计划结果中附带有序 `missing_evidence_fields`。
3. 缺金额时加入“融资金额、金额未披露、具体金额、官方披露”等关键词。
4. 保持登记官方域名优先，确保光邮星空生成 `site:zgccity.com`。
5. 保持标题和摘要只影响查询，不回写事实。
6. 覆盖未登记别名、无具体缺口和查询去重回归。

## 任务 3：融资主体证据优先级

修改：

- `src/laser_space_daily/analyzer.py`
- `tests/test_analyzer.py`
- `tests/test_fetch_verify.py`

步骤：

1. 增加同页同时含企业和大学的失败样本。
2. 按企业、采购/军队单位、研究机构、大学排序逐字主体候选。
3. 同级选择最短完整名称。
4. 保持同页逐字和主分析一致性要求。
5. 运行分析器及端到端核验测试。

## 任务 4：流水线轨迹与 Artifact 回归

修改：

- `src/laser_space_daily/pipeline.py`
- `tests/test_pipeline.py`
- `tests/test_report_notifier.py`（仅在报告诊断字段需要时）

步骤：

1. 将计划中的 `missing_evidence_fields` 写入弹性研究轨迹。
2. 使用本次 Artifact 的光邮星空文本构建流水线回归。
3. 断言原先 0 次弹性调用的原因现在可以执行定向查询。
4. 断言官方域名、金额关键词、连续无新增停止和预算计数准确。
5. 保持 pending 顶层原因兼容。

## 任务 5：完整验证与进度

修改：

- `docs/PROGRESS.md`

步骤：

1. 运行所有定向测试。
2. 运行完整离线测试。
3. 运行 `git diff --check`。
4. 确认 workflow 仍仅支持手动触发并强制 `--dry-run`。
5. 更新进度文档，记录本次 Artifact 的 12+0 调用和根因。
6. 提交并推送当前开发分支。
7. 下一次真实 dry-run 由用户手动触发。

## 验收

- `financing_missing_required_evidence` 可以触发弹性核验。
- 光邮星空被识别为金额证据缺口，并优先生成
  `site:zgccity.com` 定向查询。
- 同页境内证据选择“北京光邮星空科技有限公司”，而非“北京邮电大学”。
- 页面只省略金额时仍不能晋升。
- 研究轨迹记录具体缺口、官方域名和停止原因。
- 基础不超过 12、弹性不超过 3、总调用不超过 15。
- 严格来源、逐字证据和字段门槛保持不变。
