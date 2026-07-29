# 严格核验的同页证据重建与独立来源接入实施计划

日期：2026-07-29  
设计：
`docs/superpowers/specs/2026-07-29-strict-verification-evidence-reconstruction-design.md`

## 目标

在不降低官方来源或两个独立 B 级来源门槛的前提下，修复真实页面分类证据
过窄、基础搜索独立来源未能有效核验、弹性查询固定域名和伪航天融资误入问题。

## 任务 1：确定性分类证据补入

修改：

- `src/laser_space_daily/analyzer.py`
- `tests/test_analyzer.py`

步骤：

1. 新增失败测试：模型已给出正确商业航天融资分类，但分类引文过窄；确定性
   分析能够从同一页面分别找到星地激光通信和融资动作原文。
2. 调整规则分析器的分类证据选择，避免用整篇正文作为单条证据。
3. 调整 `ResilientAnalyzer`：主模型与规则分类一致时，合并规则分析器产生的
   同页分类证据；只补证据，不覆盖模型非空事实。
4. 断言所有新增引文逐字存在于标题、正文或页面发布时间元数据。
5. 运行分析器定向测试。

## 任务 2：分类失败精确诊断

修改：

- `src/laser_space_daily/verifier.py`
- `src/laser_space_daily/verification_followup.py`
- `src/laser_space_daily/report.py`
- `tests/test_fetch_verify.py`
- `tests/test_report_notifier.py`

步骤：

1. 新增四类失败测试：境内、类别、事件、范围证据无效。
2. 按固定顺序返回精确机器原因：
   `classification_country_evidence_invalid`、
   `classification_category_evidence_invalid`、
   `classification_event_evidence_invalid`、
   `classification_scope_evidence_invalid`。
3. 保留规则冲突、证据缺失和严格来源门槛。
4. 允许新的分类证据原因进入 90 天弹性核验池。
5. 增加报告中文映射测试。
6. 运行核验器和报告定向测试。

## 任务 3：过滤只有航天履历的融资噪声

修改：

- `src/laser_space_daily/discovery.py`
- `tests/test_discovery.py`
- `tests/fixtures/information_availability_cases.json`（仅在需要固定样本时）

步骤：

1. 新增云幕智造式样本：标题有融资动作，摘要只有创始人航天履历和人形机器人
   业务，必须拒绝。
2. 将商业航天融资的“具体业务”信号收紧为火箭、卫星、航天器、星载、空间
   通信等企业业务词，不让泛化“航天背景/航天基因”单独通过。
3. 增加合法航天融资回归样本，防止误删火箭、卫星和星地激光通信企业。
4. 运行发现层定向测试。

## 任务 4：开放式弹性查询

修改：

- `src/laser_space_daily/verification_followup.py`
- `tests/test_verification_followup.py`

步骤：

1. 将固定 `site:stcn.com`/`site:cls.cn` 测试改为三类查询：
   官方披露、投资方交叉查询、开放式同事件查询。
2. 查询优先使用结构化投资方；没有投资方时使用“领投方/投资方”占位检索词。
3. 保留运行内和持久化查询去重、90 天窗口、连续无新增停止条件。
4. 保持弹性最多 3 次、总计最多 15 次。
5. 运行查询规划器测试。

## 任务 5：同事件来源端到端回归

修改：

- `tests/test_pipeline.py`
- 如测试证明有缺口，再最小修改
  `src/laser_space_daily/pipeline.py` 或 `src/laser_space_daily/discovery.py`

步骤：

1. 构造基础搜索已经包含两个独立 B 级来源的融资事件。
2. 断言第二来源即使作为事件合并的补充候选，也被抓取、分析并参与严格核验。
3. 断言同域、同内容或字段冲突仍不能升级。
4. 断言弹性结果进入同一候选/核验数据流。
5. 运行流水线定向测试。

## 任务 6：完整验证与文档

修改：

- `docs/PROGRESS.md`

步骤：

1. 运行所有定向测试。
2. 运行完整离线测试。
3. 运行 `git diff --check`。
4. 更新进度文档，记录运行 #18 的真实业务结果、根因和本轮修复。
5. 检查 workflow 仍仅手动触发并强制 dry-run。
6. 提交并推送当前分支。
7. 通知用户手动触发一次真实 dry-run。

## 验收

- 新增与既有测试全部通过。
- 光邮星空类同页分句证据可通过分类证据核验。
- 云幕智造类人员履历噪声被拒绝。
- 弹性查询不再默认固定证券时报和财联社域名。
- 基础 12、弹性最多 3、总计最多 15。
- workflow、安全边界和来源等级规则不变。
- 下一次真实 dry-run 以 Artifact 中至少 1 条严格 `verified` 为业务目标。
