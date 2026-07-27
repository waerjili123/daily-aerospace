# “有无信息”修复实施计划

**目标：** 在不恢复定时任务、不发送钉钉的前提下，使 4 个核心博查查询稳定产出至少 5 条近期、相关、带标题/摘要/链接的候选，并让正文抓取失败不再导致空日报。

**设计依据：** `docs/superpowers/specs/2026-07-27-information-availability-prd.md`

**分支策略：** 开发分支 `codex/information-availability` 基于 PR #4 的提交 `c150877`，包含候选摘要展示能力，但不合并 PR #4。PRD 提交以 cherry-pick 方式进入开发分支。

**技术约束：**

- 先写或调整测试，再实现最小代码；
- 只修改模型、发现层、流水线、报告和对应测试；
- 不修改 workflow 触发器、Secrets、仓库可见性或钉钉配置；
- 不关闭 TLS 校验；
- 不触发 GitHub Actions；
- 真实 dry-run 必须在用户另行确认后执行。

## Task 1：扩展候选与指标模型

**修改文件：**

- `src/laser_space_daily/models.py`
- `tests/test_config_models.py`

**步骤：**

1. 为 `Candidate` 增加可选的 `category_hint` 与 `source_published_at`。
2. 为 `PendingItem` 增加相同的可选字段，保证正文抓取失败后仍能展示板块和来源日期。
3. 为 `RunMetrics` 增加采集漏斗字段：
   - `raw_search_count`
   - `valid_shape_count`
   - `relevance_pass_count`
   - `recent_7d_count`
   - `fallback_8_30d_count`
   - `unknown_date_count`
   - `final_candidate_count`
   - `fetch_failure_count`
   - `information_available`
4. 用默认值保持旧状态文件和现有测试兼容。
5. 运行：

```text
python -m pytest tests/test_config_models.py -q
```

## Task 2：收紧查询并解析博查来源日期

**修改文件：**

- `src/laser_space_daily/discovery.py`
- `tests/fixtures/bocha_search.json`
- `tests/test_discovery.py`

**步骤：**

1. 为 `SearchQuery` 增加可选分类提示。
2. 将四个核心查询拆为：
   - 激光通信招采；
   - 激光武器/反无人机招采；
   - 光电转塔/吊舱招采；
   - 商业航天融资，不再混入采购词。
3. 项目跟进查询继承项目分类；融资站点查询标记为融资分类。
4. 将博查请求的 `freshness` 从 `noLimit` 改为 `oneMonth`。
5. 从 `data.webPages.value[*].datePublished` 解析带时区日期。
6. 无时区、无效或缺失日期保留为 `None`；不假定为当天。
7. 摘要按 `summary → snippet → 空字符串` 回退。
8. 更新 fixture，覆盖包装响应、日期和摘要回退。
9. 运行：

```text
python -m pytest tests/test_discovery.py -q
```

## Task 3：实现确定性候选门槛和时间分桶

**修改文件：**

- `src/laser_space_daily/discovery.py`
- `tests/test_discovery.py`

**新增接口：**

```text
select_search_candidates(
    rows: Iterable[Candidate],
    now: datetime,
    minimum: int = 5,
    maximum: int = 10,
) -> CandidateSelection
```

`CandidateSelection` 返回最终候选及所有漏斗计数。

**步骤：**

1. 校验非空标题、非空摘要和有效 HTTP(S) URL。
2. 使用 `title + summary` 做规范化匹配。
3. 非融资分类要求至少一个对应主题锚点。
4. 融资分类同时要求商业航天主体锚点和融资事件锚点。
5. 明确排除：
   - 打印机、硒鼓、墨盒；
   - 激光雕刻、切割、打标机；
   - 激光美容、脱毛、祛斑、医美；
   - 无关通用医疗用品；
   - 无关通用 AI 算法、算力、模型或软件采购。
6. 分桶：
   - A：0–7 天；
   - B：8–30 天；
   - C：日期未知；
   - 丢弃：超过 30 天或未来超过 24 小时。
7. 先取 A；不足 5 条时用 B 补足；仍不足时用 C 补充，C 最多 2 条。
8. 最终最多 10 条，并用规范化 URL 去重。
9. 排序使用日期、官方来源、主题命中数和 URL，保证确定性。
10. 增加正向、噪声、旧闻、日期未知、重复 URL 和补足测试。
11. 运行：

```text
python -m pytest tests/test_discovery.py -q
```

## Task 4：接入流水线和业务成功判定

**修改文件：**

- `src/laser_space_daily/pipeline.py`
- `tests/test_pipeline.py`

**步骤：**

1. 只对博查搜索结果执行 `select_search_candidates`。
2. 官方种子候选继续走现有采集路径，不参与“至少 5 条博查候选”的业务成功计数。
3. 将选择结果的漏斗计数复制到 `RunMetrics`。
4. 将筛选后的博查候选与官方候选合并，再执行现有 URL 去重、抓取、分析和核验。
5. `information_available` 仅在以下条件满足时为真：
   - 4 个核心查询均已尝试；
   - `final_candidate_count >= 5`；
   - 最终候选均具备标题、摘要和链接；
   - 过滤过程无未受控异常。
6. 抓取异常时递增 `fetch_failure_count`。
7. `_put_pending` 保留 `category_hint` 与 `source_published_at`。
8. 保持单个查询、官方来源或单条候选失败不阻断整次运行。
9. 增加流水线测试，验证噪声不会进入 fetcher、候选不足会标记业务失败、正文失败仍进入 pending。
10. 运行：

```text
python -m pytest tests/test_pipeline.py -q
```

## Task 5：报告候选和采集漏斗

**修改文件：**

- `src/laser_space_daily/report.py`
- `tests/test_report_notifier.py`
- `tests/snapshots/daily_report.md`

**步骤：**

1. 待核实候选展示：
   - 板块；
   - 来源发布日期或“发布日期未知”；
   - 时间标签：近 7 天、8–30 天补充、日期未知；
   - 标题；
   - 未核实原因；
   - 原始链接；
   - 未核实搜索摘要。
2. 数据完整性板块展示完整采集漏斗。
3. `information_available=false` 时显示“信息不足”，不以“暂无已核实信息”掩盖采集不足。
4. 保持现有 Markdown 长度控制和链接转义规则。
5. 更新快照和边界测试。
6. 运行：

```text
python -m pytest tests/test_report_notifier.py -q
```

## Task 6：固定样本与全量离线验证

**修改文件：**

- `tests/fixtures/information_availability_cases.json`
- `tests/test_end_to_end_fixtures.py`

**样本要求：**

- 6 条近 7 天相关候选；
- 3 条 8–30 天相关候选；
- 3 条日期未知相关候选；
- 2 条超过 30 天旧闻；
- 打印机、美容、雕刻、医疗、通用 AI 各 1 条；
- 2 条重复 URL；
- 2 条正文抓取失败候选。

**步骤：**

1. 增加固定样本，锁定筛选与排序结果。
2. 验证正文失败候选仍有标题、摘要和链接。
3. 运行定向测试：

```text
python -m pytest tests/test_discovery.py tests/test_pipeline.py tests/test_report_notifier.py tests/test_end_to_end_fixtures.py -q
```

4. 运行完整测试：

```text
python -m pytest -q
```

5. 运行：

```text
git diff --check
```

## Task 7：进度记录与交付

**修改文件：**

- `docs/PROGRESS.md`

**步骤：**

1. 恢复并更新项目进度文档，纠正已过时的 PR #3、workflow 和采集状态。
2. 记录：
   - PR #3 已合并；
   - PR #4 仍未合并；
   - workflow 当前仅手动、强制 dry-run；
   - 真实采集已能返回 39 条候选；
   - 本次实现和离线测试结果；
   - 尚未执行真实 dry-run；
   - 定时、钉钉、Secrets 和仓库可见性均未变更。
3. 提交代码、测试、计划和进度文档。
4. 不推送、不创建新 PR、不触发 Actions，等待用户确认真实 dry-run。
