# 严格核验来源与页面证据修复实施计划

日期：2026-07-30
设计：`docs/superpowers/specs/2026-07-30-strict-verification-source-evidence-repair-design.md`

## 目标

修复真实页面已有发布日期未进入正文证据、国内主体被国外比较对象误拒、同一事件重复消耗
弹性预算，以及高等级来源被较新 C 级转载挤出的链路问题。在不降低严格核验门槛的前提
下，让一组事实一致的投中网与投资界 B 级页面能够端到端晋级为 `verified`。

## 任务 1：页面发布时间证据

修改：

- `src/laser_space_daily/fetcher.py`
- `tests/test_fetch_verify.py`

步骤：

1. 为 `FetchedPage` 增加可选的日期证据原文和来源类型。
2. 保留现有发布元数据及 JSON-LD 提取。
3. 新增文章标题区、署名区和明确“发布日期/发布时间”标签的可见日期提取。
4. 只接受完整年月日，拒绝更新时间、页脚、正文历史日期和相关推荐日期。
5. 当正文抽取遗漏日期时，追加逐字 `页面发布时间：...`。
6. 使用投中网 `.releaseTime`、公告标题区和负面结构 fixture 覆盖回归。

验收：

- 真实结构等价的投中网页面能够返回 `visible_header` 日期证据。
- 日期可以被后续规则分析器逐字 grounding。
- 搜索日期、URL 日期和 HTTP 时间均未参与。

## 任务 2：事件主体级境内外判断

修改：

- `src/laser_space_daily/analyzer.py`
- `src/laser_space_daily/verifier.py`
- `tests/test_analyzer.py`
- `tests/test_fetch_verify.py`

步骤：

1. 提取标题、导语和事件动作句组成主体上下文。
2. 将国内/国外信号判断限定在主体上下文。
3. 国内主体明确时，正文后部 SpaceX 等比较对象不再否决事件。
4. 国外主体仅把中国作为市场背景时仍保持境外。
5. verifier 分类一致性检查复用同一上下文函数。
6. 补充国内融资长文、国外主体和主语冲突测试。

验收：

- 微光启航融资长文提到 SpaceX 时仍通过境内规则判断。
- 真正国外融资事件不会因正文出现“中国市场”而被误收。
- 主分析与 verifier 不再因使用不同全文范围产生规则分歧。

## 任务 3：稳定事件指纹和三目标覆盖

修改：

- `src/laser_space_daily/verification_followup.py`
- `src/laser_space_daily/pipeline.py`
- `tests/test_verification_followup.py`
- `tests/test_pipeline.py`

步骤：

1. 增加确定性的融资主体别名和轮次集合规范化。
2. 生成 `verification_event_key`，统一全称、简称和连续两轮表述。
3. 日期邻近固定为不超过 7 个自然日。
4. 明确轮次集合无交集时不合并；轮次缺失时采用严格标题一致条件。
5. 规划器首次覆盖数量从硬编码 2 改为配置的 `verification_max_targets`。
6. 三个合格事件存在时优先各尝试一次，之后才允许同事件重试。
7. 连续无新增计数和停止逻辑按事件键更新。
8. 排序优先日期单项缺失和高晋升潜力，最后处理多字段缺口。

验收：

- 光邮星空多个 URL 使用同一事件键。
- 微光启航和上海无人机项目使用不同事件键。
- 三次预算在三个事件存在时不会被同一事件占满。
- 同主体不同轮次和不同时间的真实事件不会错误合并。

## 任务 4：来源等级感知的候选选择

修改：

- `src/laser_space_daily/discovery.py`
- `src/laser_space_daily/pipeline.py`
- `src/laser_space_daily/verification_followup.py`
- `config.yaml`
- `config.example.yaml`
- `tests/test_discovery.py`
- `tests/test_pipeline.py`
- `tests/test_config_models.py`

步骤：

1. 为候选及补充来源选择增加可选的高等级域名集合。
2. 同事件按官方、已登记 A/B、其他来源排序，再比较日期和相关性。
3. 补充来源优先选择不同的已登记 B 域名。
4. 从核验规划器公开只读的融资 B 域名集合，供流水线传入筛选器。
5. 在生产和示例配置中新增 `chinaventure.com.cn`。
6. 不新增东方财富、新浪、腾讯或其他转载域名。
7. 验证单一投中网页面仍为 pending。

验收：

- 较新的 C 级转载不能挤掉 `pedaily.cn` 和
  `chinaventure.com.cn`。
- 两个 B 级来源均进入抓取、分析和严格核验链路。
- 来源配置变化不绕过正文证据和独立域名检查。

## 任务 5：诊断和严格晋级端到端回归

修改：

- `src/laser_space_daily/pipeline.py`
- `tests/test_pipeline.py`
- `tests/test_cli.py`

步骤：

1. `CandidateDiagnostic` 增加日期证据来源和事件键。
2. 无日期证据时输出 `null`，不得使用候选日期伪装。
3. 同事件不同 URL 输出相同事件键。
4. 增加投中网与投资界两个独立 B 页面事实一致的端到端 fixture。
5. 验证最终状态为
   `verified_financing_two_independent_sources`。
6. 增加日期、金额、轮次冲突时不得晋级的负面用例。
7. 确认 Artifact 不新增正文、模型输出或敏感数据。

验收：

- 离线端到端回归至少产生 1 条严格 `verified`。
- 单一 B、C 级转载、搜索摘要和搜索日期仍不能晋级。
- 新诊断字段能解释日期和事件合并行为。

## 任务 6：完整验证和进度

修改：

- `docs/PROGRESS.md`

步骤：

1. 运行抓取、分析、发现、核验规划和流水线定向测试。
2. 运行完整离线测试。
3. 运行 `git diff --check`。
4. 检查 workflow 仍仅为 `workflow_dispatch` 且强制 `--dry-run`。
5. 检查配置中基础 12、弹性 3、总上限 15 未变化。
6. 更新进度文档，记录最新 Artifact 的 12+3、已核实 0、待核实 5 和四个根因。
7. 提交实现；公开分支推送前单独取得用户明确授权。
8. 推送后由用户手动触发下一次真实 dry-run。

## 总体验收

- 页面自身可见完整发布时间进入逐字证据，搜索日期仍不写回事实。
- 国内事件不因正文后部国外比较对象被误拒。
- 三次弹性预算优先覆盖三个不同事件。
- B 级来源优先于 C 级转载。
- `chinaventure.com.cn` 单源不能晋级。
- 投中网与投资界两个独立 B 来源事实一致时严格晋级。
- 基础不超过 12、弹性不超过 3、总计不超过 15。
- 完整测试、diff 检查和 workflow 安全检查通过。
- 未触发 Actions、未发送钉钉、未修改 Secrets 或 workflow 状态。
