# 激光与商业航天情报日报

这是一个面向激光通信与商业航天产业的日度情报管道：收集公开线索、核验来源、关联历史项目，并生成可追溯的 Markdown 报告。它只处理本项目范围内的产业与采购情报，**独立于 AI日报**，不包含任何 AI 新闻内容。

> 工作流每天北京时间 07:50 执行主投递，并在 08:20 提供独立兜底；两次均使用
> `daily + 12 + dingtalk_live`。当天已有成功正式投递时，后续运行在采集前跳过。
> 手动运行仍默认 `delivery_mode=dry_run`；显式选择
> `dingtalk_test` 会执行严格测试门禁并添加“【测试】”标题，选择
> `dingtalk_live` 才会发送不带测试标识的正式短报。

## 架构与目录

命令行程序负责按北京时间窗口运行发现、抓取、分析、核验、匹配和报告渲染。`config.yaml` 只保存可提交的运行参数；四个密钥只从环境变量读取。主要目录如下：

- `src/laser_space_daily/`：CLI、管道和领域逻辑。
- `config/official_sources.yaml`：官方来源及分级规则。
- `data/`：项目、融资、事件与待核验状态。
- `reports/`：每日 Markdown 报告；GitHub Actions 会上传为 artifact。
- `.github/workflows/daily-intelligence.yml`：当前仅用于手动、小规模采集验收。

来源按 A/B/C 分级；`pending` 表示线索尚待核验，不能被当作已确认的正式记录。

钉钉与 `reports/` 使用业务短报：固定先展示“商业航天融资新闻”，再展示
“招标采购情况”，融资金额与项目预算/中标金额互不混算。短报只保留可信状态、
关键事实和可点击来源；采集漏斗、缺失字段、失败域、研究轨迹等完整技术诊断继续
保存在 `data/` Artifact。

## 本地运行

安装 Python 3.13，创建虚拟环境后安装开发依赖：

```bash
python -m pip install -c constraints.txt -e ".[dev]"
```

`constraints.txt` 固定运行、构建与测试的直接依赖版本；更新依赖时应同步运行完整测试后再提交约束文件。

在终端设置 `DEEPSEEK_API_KEY`、`BOCHA_API_KEY`、`DINGTALK_WEBHOOK` 和 `DINGTALK_SECRET` 后，先进行不发送钉钉消息的演练：

```bash
laser-space-daily --config config.yaml --dry-run
```

日常增量最多执行 12 次博查搜索。标准 12 次预算会先覆盖三类招标和一条融资
综合查询，再使用四个已注册融资来源进行定向种子检索，因此商业航天融资至少
获得 5 次基础覆盖；剩余预算用于模型追查：

```bash
laser-space-daily --config config.yaml --dry-run --discovery-mode daily --max-queries 12
```

一次性近 90 天历史回填最多执行 40 次，并且必须保持 dry-run：

```bash
laser-space-daily --config config.yaml --dry-run --discovery-mode backfill --max-queries 40
```

DeepSeek 通过受控的 `search_web` Tool Calling 提出后续查询。本地预算守卫负责执行博查调用，模型不能突破日常 12 次或回填 40 次的硬上限。研究轨迹写入 `data/research-trace.json`，不包含密钥、认证头、完整网页正文或模型隐藏推理。

正常运行会写入 `data/run-result.json` 和脱敏的
`data/delivery-status.json`。若分析后续阶段失败，本轮候选检查点会生成明确标注的
“【降级】”快报；检查点产生前失败时只生成“【异常】”告警，绝不回退到旧日报。
请检查 `reports/` 中的报告与每条来源链接，再决定是否进行真实推送。不要把密钥写入
`config.yaml`、日志或问题反馈中。

## 上线验收边界

当前验收状态明确区分离线证据与尚未执行的外部验收：

- [x] 已完成离线固定样本验收：四类内容、采购全生命周期、废标后重新招标、同名不同标段、融资 A 级来源/两个独立 B 级来源/单一 B 级待核验/银行授信排除。
- [x] 已完成离线全量测试、核心模块逐文件覆盖率、编译和敏感信息扫描；这些测试使用注入的固定客户端，不访问真实 DeepSeek、博查或钉钉。
- [x] 已配置 `DEEPSEEK_API_KEY`、`BOCHA_API_KEY`、`DINGTALK_WEBHOOK`、`DINGTALK_SECRET` Secrets；仓库当前实际为 public，与原定私有要求不一致。
- [x] 已定位博查真实响应位于 `data.webPages.value`。
- [ ] 待运行一次 40 查询历史回填 dry-run，下载并人工审核三个月项目池。
- [ ] 待运行一次 12 查询日常 dry-run，核对候选质量、去重和研究轨迹。
- [ ] 待审核通过后，从一次性测试分支发送一条标题含“【测试】”的钉钉消息，随后立即恢复 dry-run。

本地离线验收环境为 Python 3.12；项目要求的 Python 3.13 以 GitHub Actions 工作流为权威兼容性门禁。Actions 成功只表示程序没有失败，不代表采集到真实信息。

## GitHub Actions 采集恢复验收

1. 定时任务固定使用 `daily`、12 次基础查询和 `dingtalk_live`；主 cron 为
   `50 23 * * *`（北京时间 07:50），兜底 cron 为 `20 0 * * *`（北京时间
   08:20）。当天已有成功的 `scheduled-live` 或 `manual-live` 时，后续运行跳过。
2. 手动日常验证选择 `daily`、12 次查询和默认 `dry_run`；历史回填选择
   `backfill`、40 次查询并保持 `dry_run`。只有一次性钉钉验收才选择
   `dingtalk_test`。
3. 确认正式补发时选择 `dingtalk_live`；该模式不使用测试门禁，但所有非严格
   内容仍必须明确标注“高可信待核实”或“候选线索”，不得冒充已核实信息。
4. 下载包含 `reports/` 与 `data/` 的 artifact，确认候选数大于 0、原始链接存在，并核对 `research-trace.json` 中的预算、轮次、查询和停止原因。

`dingtalk_test` 仍要求本轮至少 1 条严格已核实信息以及至少一个带来源的业务条目；
`dingtalk_live` 用于定时和经确认的正式补发，即使本轮没有严格已核实信息，也会发送
清晰标注可信状态的可读短报或降级快报。钉钉只有返回 `errcode=0` 才记录为投递成功；
Actions Success 仍只代表流程完成，不等同于采集业务质量达标。

同一个 `data_dir` 必须遵守**单写入者**约束。Actions 的 `concurrency` 组负责串行化云端任务；CLI 同时持有 `data/.laser-space-daily.lock` 操作系统锁，本地第二个进程会直接以退出码 4 结束。锁文件可以保留，进程退出或崩溃时操作系统会释放锁；不要用不同工作目录绕过同一份状态的串行要求。

## 排障与迁移

CLI 的退出码 2 表示配置错误，退出码 3 表示通知失败，退出码 4 表示管道处理失败。排障时可检查 Actions 日志、artifact 和来源链接；不要在 issue、聊天或日志中粘贴任何密钥。

当前持久化格式由 `data/state.json` 中的 `schema_version=2` 标识。升级前没有该文件的状态视为 v1：加载时会保留既有记录 ID，以公告时间补齐首次发现/更新时间，并根据已存元数据生成确定性的兼容内容哈希；下一次成功提交会原子写入 v2 元数据及补齐后的字段。由于 v1 未保存网页正文，这个兼容哈希不是历史正文哈希。迁移前应备份整个 `data/`，且不要让旧版本程序回写已升级状态。

以后迁移到服务器时，使用同一个 CLI 放在 cron 下运行即可。既有 JSON/JSONL 数据可导入 SQLite 或 PostgreSQL；迁移时保留现有记录 ID，以维持报告、匹配和状态之间的引用关系。
