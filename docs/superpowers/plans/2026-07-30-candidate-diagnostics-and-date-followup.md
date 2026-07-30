# 候选诊断与发布日期定向核验实施计划

日期：2026-07-30
设计：`docs/superpowers/specs/2026-07-30-candidate-diagnostics-and-date-followup-design.md`

## 目标

让仅缺网页发布日期的候选能够使用弹性预算寻找带逐字日期的新来源，并为所有进入抓取
链路的候选输出精简、可解释、无正文和敏感信息的诊断 Artifact。

## 任务 1：可解释弹性资格

修改：

- `src/laser_space_daily/verification_followup.py`
- `tests/test_verification_followup.py`

步骤：

1. 增加资格结果模型，统一返回是否合格及拒绝原因。
2. 让现有规划器和候选诊断共用同一资格函数。
3. 精确支持 `missing_required_fields:published_at`。
4. 仍要求境内、范围、类别、事件、主体和 90 天池条件。
5. 候选发布日期只用于资格判断，不写回分析结果。
6. 覆盖状态、原因、分类、主体、时间池、停止阈值和查询耗尽测试。

## 任务 2：发布日期缺口查询

修改：

- `src/laser_space_daily/verification_followup.py`
- `tests/test_verification_followup.py`
- `tests/test_pipeline.py`

步骤：

1. 从精确 missing reason 提取 `published_at` 缺口。
2. 查询加入发布日期、发布时间、公告时间和官方披露关键词。
3. 保持登记官方域名优先和现有三类查询顺序。
4. 新页面仍须通过抓取、分析和日期逐字证据校验。
5. 验证 12+3 上限与连续无新增停止。

## 任务 3：候选诊断模型与生命周期

修改：

- `src/laser_space_daily/pipeline.py`
- `tests/test_pipeline.py`

步骤：

1. 新增精简 `CandidateDiagnostic` 模型和 `RunResult` 输出。
2. 为抓取、分析、拒绝、待核实、已核实及后置 payload 失败记录阶段和原因。
3. 记录 discovery source、是否为报告最终候选、来源等级、缺失字段和首次弹性资格。
4. 按规范化 URL 去重，弹性新增来源也进入诊断。
5. 错误只保存类型，不保存异常消息或正文。
6. 覆盖光邮星空被拒仍可见、微光启航日期缺口可见和敏感文本不落地。

## 任务 4：原子 Artifact 输出

修改：

- `src/laser_space_daily/cli.py`
- `tests/test_cli.py`

步骤：

1. 增加通用或专用 JSON 原子写入函数。
2. dry-run 和正常运行均写入 `data/candidate-diagnostics.json`。
3. 固定 UTF-8、缩进和 URL 排序。
4. 写入失败沿用流水线输出失败语义。
5. 验证文件存在、结构稳定且不包含正文或凭据。

## 任务 5：完整验证与进度

修改：

- `docs/PROGRESS.md`
- 已确认的设计文档状态

步骤：

1. 运行定向测试。
2. 运行完整离线测试。
3. 运行 `git diff --check`。
4. 确认 workflow 仍仅手动触发并强制 `--dry-run`。
5. 更新进度文档，记录运行 #22 的 12+0 调用和新根因。
6. 提交并推送当前开发分支。
7. 下一次真实 dry-run 由用户手动触发。

## 验收

- `missing_required_fields:published_at` 可以触发弹性核验。
- 查询包含发布日期、发布时间、公告时间和官方披露。
- 搜索日期不会写回分析发布日期。
- 每条报告最终候选均有诊断；旁路来源带明确角色标记。
- 光邮星空即使被拒也有精确阶段和原因。
- `candidate-diagnostics.json` 不包含正文、模型原始输出或敏感信息。
- 基础不超过 12、弹性不超过 3、总调用不超过 15。
- 严格来源、日期逐字证据和其他核验门槛保持不变。
