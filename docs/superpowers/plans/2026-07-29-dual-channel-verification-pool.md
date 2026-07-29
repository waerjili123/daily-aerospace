# 双通道 90 天滚动核验池实施计划

对应设计：
`docs/superpowers/specs/2026-07-29-dual-channel-verification-pool-design.md`

目标：保持日常基础 12 次查询，在高潜力事件出现时追加最多 3 次定向补源，
总调用硬上限 15，并在真实强制 dry-run 中至少产生 1 条严格 `verified`。

## Task 1：核验状态与运行指标

涉及文件：

- `src/laser_space_daily/models.py`
- `src/laser_space_daily/config.py`
- `config.yaml`
- `config.example.yaml`
- `tests/test_config_models.py`

步骤：

1. 为 `PendingItem` 增加向后兼容的补源状态字段，包括补源次数、最后补源时间、
   连续无新增次数和已尝试查询。
2. 为 `RunMetrics` 增加发现通道、核验通道、弹性调用、处理事件数、新增来源数、
   重复来源数和弹性触发原因。
3. 为日常模式增加基础预算 12、弹性预算 3、90 天窗口和停止门槛配置。
4. 配置校验保证基础预算不超过 12、弹性预算不超过 3、日常总硬上限不超过 15。
5. 增加配置和旧状态兼容测试。

## Task 2：确定性核验任务选择与查询生成

涉及文件：

- `src/laser_space_daily/verification_followup.py`
- `tests/test_verification_followup.py`

步骤：

1. 新增独立的 `VerificationFollowupCoordinator`。
2. 接收当前候选的分析结果、核验决定和最近 90 天历史待核实项。
3. 仅选择字段完整且失败原因为缺官方来源、缺第二个 B 级来源或分类证据不足的
   高潜力事件。
4. 按已有 B 级来源、事件日期、补源次数和稳定指纹排序，最多处理 1–3 个事件。
5. 为融资和采购生成确定性官方、投资机构和 B 级媒体查询。
6. 规范化查询并拦截已尝试、重复和超出 90 天范围的请求。
7. 增加排序、模板、重复查询、90 天边界和停止条件测试。

## Task 3：12＋3 预算执行与二次核验

涉及文件：

- `src/laser_space_daily/agentic_discovery.py`
- `src/laser_space_daily/pipeline.py`
- `src/laser_space_daily/cli.py`
- `tests/test_agentic_discovery.py`
- `tests/test_pipeline.py`

步骤：

1. 保持现有 Agentic 基础检索上限 12，不允许普通检索使用弹性预算。
2. 将最近 90 天历史待核实 URL 重新加入本次抓取候选，供重新分析和补源。
3. 第一轮抓取、分析和核验后选出高潜力事件。
4. 只有存在高潜力事件时执行最多 3 次定向补源。
5. 新来源完成正文抓取和分析后，与原来源共同重新执行严格核验。
6. 二次核验成功时正常写入事件或融资状态；失败时更新待核实项的补源状态。
7. 所有搜索调用计入同一硬上限，断言日常模式总数不超过 15。
8. 增加“无高潜力不追加”“追加 1–3 次”“二次 B 来源升级”“同稿不升级”和
   “总预算不超 15”测试。

## Task 4：来源独立性与分类证据

涉及文件：

- `src/laser_space_daily/verifier.py`
- `src/laser_space_daily/discovery.py`
- `tests/test_fetch_verify.py`
- `tests/test_discovery.py`

步骤：

1. 复用现有事件级合并规则识别移动页、打印页、追踪参数和相同稿件。
2. B 级来源仍只来自显式配置白名单。
3. 核验时要求两个 B 级来源的注册域名和内容来源均独立。
4. 修复页面元数据追加后，分类证据验证对页面标题和正文证据的稳定处理。
5. 不修改 C 级媒体的正式核验资格。
6. 增加转载、镜像、同集团和真正独立来源的固定样本。

## Task 5：报告与运行产物

涉及文件：

- `src/laser_space_daily/report.py`
- `tests/test_report_notifier.py`
- `docs/PROGRESS.md`

步骤：

1. 报告展示基础调用、弹性调用、触发事件和触发原因。
2. 展示本次处理的核验事件、新增官方/B 级来源和合并重复来源数量。
3. 待核实项继续展示具体字段或来源缺口。
4. `research-trace.json` 增加不含 Secrets 的补源查询轨迹。
5. 更新进度文档中的测试和真实验证结果。

## Task 6：安全 workflow 与真实验收

涉及文件：

- `.github/workflows/daily-intelligence.yml`
- `tests/test_config_models.py`

步骤：

1. workflow 仍只保留 `workflow_dispatch`。
2. 命令仍硬编码 `--dry-run --discovery-mode daily`。
3. 手动输入保持基础 `--max-queries 12`，弹性 3 次由代码内确定性条件控制。
4. 钉钉环境变量继续使用无效占位地址。
5. 运行完整离线测试和 `git diff --check`。
6. 提交并推送现有开发分支。
7. 触发一次真实日常 dry-run，核对总调用不超过 15、弹性触发原因和
   `verified` 数量。
8. 若仍无 `verified`，报告必须精确给出选中事件、实际找到的来源和最后缺口；
   不降低核验标准。

