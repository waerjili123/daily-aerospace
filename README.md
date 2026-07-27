# 激光与商业航天情报日报

这是一个面向激光通信与商业航天产业的日度情报管道：收集公开线索、核验来源、关联历史项目，并生成可追溯的 Markdown 报告。它只处理本项目范围内的产业与采购情报，**独立于 AI日报**，不包含任何 AI 新闻内容。

> 当前处于采集链路恢复阶段。自动 workflow 已暂停；工作流暂时只允许手动、小规模 `dry_run=true` 验收，不发送钉钉、不提交状态，也不包含定时入口。

## 架构与目录

命令行程序负责按北京时间窗口运行发现、抓取、分析、核验、匹配和报告渲染。`config.yaml` 只保存可提交的运行参数；四个密钥只从环境变量读取。主要目录如下：

- `src/laser_space_daily/`：CLI、管道和领域逻辑。
- `config/official_sources.yaml`：官方来源及分级规则。
- `data/`：项目、融资、事件与待核验状态。
- `reports/`：每日 Markdown 报告；GitHub Actions 会上传为 artifact。
- `.github/workflows/daily-intelligence.yml`：当前仅用于手动、小规模采集验收。

来源按 A/B/C 分级；`pending` 表示线索尚待核验，不能被当作已确认的正式记录。

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

请检查 `reports/` 中的报告与每条来源链接，再决定是否进行真实推送。不要把密钥写入 `config.yaml`、日志或问题反馈中。

## 上线验收边界

当前验收状态明确区分离线证据与尚未执行的外部验收：

- [x] 已完成离线固定样本验收：四类内容、采购全生命周期、废标后重新招标、同名不同标段、融资 A 级来源/两个独立 B 级来源/单一 B 级待核验/银行授信排除。
- [x] 已完成离线全量测试、核心模块逐文件覆盖率、编译和敏感信息扫描；这些测试使用注入的固定客户端，不访问真实 DeepSeek、博查或钉钉。
- [x] 已配置 `DEEPSEEK_API_KEY`、`BOCHA_API_KEY`、`DINGTALK_WEBHOOK`、`DINGTALK_SECRET` Secrets；仓库当前实际为 public，与原定私有要求不一致。
- [x] 已定位博查真实响应位于 `data.webPages.value`。
- [ ] 待运行仅手动、小规模 `workflow_dispatch`，固定保持 `dry_run=true`，下载并人工审核报告与状态 artifact。
- [ ] 待审核通过后运行一次 `dry_run=false`，验收仅一条钉钉消息及其来源链接。

本地离线验收环境为 Python 3.12；项目要求的 Python 3.13 以 GitHub Actions 工作流为权威兼容性门禁。Actions 成功只表示程序没有失败，不代表采集到真实信息。

## GitHub Actions 采集恢复验收

1. 保持自动 workflow 暂停，先合并 `data.webPages.value` 解析修复。
2. 重新启用不含 `schedule` 的仅手动工作流。
3. 首次选择 4 次核心查询运行；命令固定包含 `--dry-run`，无法从页面关闭。
4. 下载包含 `reports/` 与 `data/` 的 artifact，确认候选数大于 0、原始链接存在，并区分抓取、分析和核验失败。

自动运行和 `dry_run=false` 均不在本阶段启用。只有真实信息采集验收通过后，才修复并验证北京时间 07:30 调度；日报人工审核通过后，才进行一次正式钉钉验收。

同一个 `data_dir` 必须遵守**单写入者**约束。Actions 的 `concurrency` 组负责串行化云端任务；CLI 同时持有 `data/.laser-space-daily.lock` 操作系统锁，本地第二个进程会直接以退出码 4 结束。锁文件可以保留，进程退出或崩溃时操作系统会释放锁；不要用不同工作目录绕过同一份状态的串行要求。

## 排障与迁移

CLI 的退出码 2 表示配置错误，退出码 3 表示通知失败，退出码 4 表示管道处理失败。排障时可检查 Actions 日志、artifact 和来源链接；不要在 issue、聊天或日志中粘贴任何密钥。

当前持久化格式由 `data/state.json` 中的 `schema_version=2` 标识。升级前没有该文件的状态视为 v1：加载时会保留既有记录 ID，以公告时间补齐首次发现/更新时间，并根据已存元数据生成确定性的兼容内容哈希；下一次成功提交会原子写入 v2 元数据及补齐后的字段。由于 v1 未保存网页正文，这个兼容哈希不是历史正文哈希。迁移前应备份整个 `data/`，且不要让旧版本程序回写已升级状态。

以后迁移到服务器时，使用同一个 CLI 放在 cron 下运行即可。既有 JSON/JSONL 数据可导入 SQLite 或 PostgreSQL；迁移时保留现有记录 ID，以维持报告、匹配和状态之间的引用关系。
