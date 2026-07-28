# 智能多轮情报检索实施计划

日期：2026-07-28（北京时间）

对应设计：
`docs/superpowers/specs/2026-07-28-agentic-intelligence-retrieval-design.md`

## 实施原则

- 先写失败测试，再实现最小代码；
- 每个阶段保持完整离线测试可运行；
- 真实 API、Actions 和钉钉验证必须在离线实现完成后单独执行；
- 不修改 schedule、Secrets、仓库可见性和 PR 合并状态。

## 阶段 1：配置、模式与研究轨迹模型

修改：

- `src/laser_space_daily/config.py`
- `src/laser_space_daily/models.py`
- `config.yaml`
- `config.example.yaml`
- `tests/test_config_models.py`

实现：

- `daily` 与 `backfill` 模式；
- 日常预算最大 12、回填预算最大 40；
- 最大智能轮次、单次结果数和连续无新增停止阈值；
- 查询轨迹、预算使用、重复查询、过滤、合并、降级和停止原因指标；
- CLI 模式选择与预算覆盖的安全校验。

## 阶段 2：受控 DeepSeek Tool Calling 编排器

新增：

- `src/laser_space_daily/agentic_discovery.py`
- `tests/test_agentic_discovery.py`

实现：

- 四板块确定性种子查询；
- `search_web` 工具 schema；
- DeepSeek 工具调用循环；
- 本地查询校验、作用域补全和重复查询拦截；
- 日常 12、回填 40 的原子预算守卫；
- 多工具调用不能突破剩余预算；
- 连续无新增提前停止；
- 模型失败时保留种子结果并标注降级；
- 不保存隐藏推理、Secrets 或完整正文。

## 阶段 3：事件意图过滤

修改：

- `src/laser_space_daily/discovery.py`
- `tests/test_discovery.py`
- 固定样本文件

实现：

- 采购类主题＋生命周期双门槛；
- 融资类主体＋股权事件双门槛；
- 排除研报销售页、打印目录、股市行情、荐股和泛化券商观点；
- 保留具体项目公告、状态变化和公司融资公告；
- 对过滤原因进行计数。

## 阶段 4：事件级近重复合并

修改：

- `src/laser_space_daily/discovery.py`
- `tests/test_discovery.py`

实现：

- URL 规范化继续保留；
- 公告编号精确合并；
- 标题、主体、事件类型、日期和摘要的保守相似度合并；
- 普通页与打印页合并；
- 同一项目不同生命周期事件不得合并；
- 保留合并来源 URL 或至少保留可审计的主来源选择依据。

## 阶段 5：流水线与报告集成

修改：

- `src/laser_space_daily/pipeline.py`
- `src/laser_space_daily/report.py`
- `src/laser_space_daily/cli.py`
- `tests/test_pipeline.py`
- `tests/test_report_notifier.py`

实现：

- 生产环境使用智能编排器，测试依赖仍可使用确定性适配器；
- 将研究轨迹指标写入 `RunMetrics`；
- 报告展示预算、轮次、重复查询、过滤、合并、降级类型和停止原因；
- 分类板块附带本板块候选线索且明确“未核实”；
- “今日重点跟进”不重复列出已在分类板块展示的候选；
- 测试发送标题增加“【测试】”的受控入口，但默认 workflow 仍强制 dry-run。

## 阶段 6：回归与安全检查

执行：

- 新模块定向测试；
- 发现、流水线、报告和 CLI 定向测试；
- 完整 `pytest -q`；
- `git diff --check`；
- Secret 模式扫描；
- workflow 只读核对。

验收：

- 所有离线测试通过；
- 日常预算无法超过 12；
- 回填预算无法超过 40；
- 无真实网络请求；
- 无钉钉发送；
- 无 schedule 或权限扩张。

## 阶段 7：真实验证

在离线实现完成并推送后：

1. 手动运行一次 40 查询历史回填 dry-run；
2. 下载并审核 Artifact；
3. 手动运行一次 12 查询日常 dry-run；
4. 审核通过后创建名称明确的一次性钉钉测试分支；
5. 发送一条标题含“【测试】”的钉钉日报；
6. 核对消息和 Artifact；
7. 立即将测试分支恢复为强制 dry-run；
8. 更新 `docs/PROGRESS.md`。
