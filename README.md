# 激光与商业航天情报日报

这是一个面向激光通信与商业航天产业的日度情报管道：收集公开线索、核验来源、关联历史项目，并生成可追溯的 Markdown 报告。它只处理本项目范围内的产业与采购情报，**独立于 AI日报**，不包含任何 AI 新闻内容。

## 架构与目录

命令行程序负责按北京时间窗口运行发现、抓取、分析、核验、匹配和报告渲染。`config.yaml` 只保存可提交的运行参数；三个密钥只从环境变量读取。主要目录如下：

- `src/laser_space_daily/`：CLI、管道和领域逻辑。
- `config/official_sources.yaml`：官方来源及分级规则。
- `data/`：项目、融资、事件与待核验状态；状态 JSON/JSONL 会由定时任务自动提交。
- `reports/`：每日 Markdown 报告；GitHub Actions 会上传为 artifact。
- `.github/workflows/daily-intelligence.yml`：每日运行、测试、报告归档和状态提交。

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
- [ ] 待在私有 GitHub 远端配置 `DEEPSEEK_API_KEY`、`BOCHA_API_KEY`、`DINGTALK_WEBHOOK`、`DINGTALK_SECRET` Secrets。
- [ ] 待首次运行 `workflow_dispatch` 且保持 `dry_run=true`，下载并人工审核报告与状态 artifact。
- [ ] 待审核通过后运行一次 `dry_run=false`，验收仅一条钉钉消息及其来源链接。

本地离线验收环境为 Python 3.12；项目要求的 Python 3.13 以 GitHub Actions 工作流为权威兼容性门禁。在上述三个外部步骤实际完成前，不代表 GitHub 定时运行或钉钉实发已经成功。

## GitHub Actions 首次启用

1. 创建一个**私有** GitHub 仓库并推送本项目。
2. 在仓库 **Settings → Secrets and variables → Actions** 中添加四个名称完全一致的 Secrets：`DEEPSEEK_API_KEY`、`BOCHA_API_KEY`、`DINGTALK_WEBHOOK`、`DINGTALK_SECRET`。钉钉机器人使用加签安全模式时，`DINGTALK_SECRET` 应填写以 `SEC` 开头的完整加签密钥。
3. 在 Actions 页面手动运行工作流，保持 `dry_run=true`。下载包含 `reports/` 与 `data/` 的 artifact，核对报告、来源链接和状态。
4. 审核通过后，手动以 `dry_run=false` 运行一次，确认钉钉投递可接受。

工作流随后每天在 07:30（北京时间；UTC `30 23 * * *`）运行。计划任务会先跑完整测试，成功后生成报告、提交变更的 `data/` 与 `reports/`，并安全地 rebase 后推送。手动 `dry_run=true` 仅生成报告和状态，不发送通知。

同一个 `data_dir` 必须遵守**单写入者**约束。Actions 的 `concurrency` 组负责串行化云端任务；CLI 同时持有 `data/.laser-space-daily.lock` 操作系统锁，本地第二个进程会直接以退出码 4 结束。锁文件可以保留，进程退出或崩溃时操作系统会释放锁；不要用不同工作目录绕过同一份状态的串行要求。

## 排障与迁移

CLI 的退出码 2 表示配置错误，退出码 3 表示通知失败，退出码 4 表示管道处理失败。排障时可检查 Actions 日志、artifact 和来源链接；不要在 issue、聊天或日志中粘贴任何密钥。

当前持久化格式由 `data/state.json` 中的 `schema_version=2` 标识。升级前没有该文件的状态视为 v1：加载时会保留既有记录 ID，以公告时间补齐首次发现/更新时间，并根据已存元数据生成确定性的兼容内容哈希；下一次成功提交会原子写入 v2 元数据及补齐后的字段。由于 v1 未保存网页正文，这个兼容哈希不是历史正文哈希。迁移前应备份整个 `data/`，且不要让旧版本程序回写已升级状态。

以后迁移到服务器时，使用同一个 CLI 放在 cron 下运行即可。既有 JSON/JSONL 数据可导入 SQLite 或 PostgreSQL；迁移时保留现有记录 ID，以维持报告、匹配和状态之间的引用关系。
