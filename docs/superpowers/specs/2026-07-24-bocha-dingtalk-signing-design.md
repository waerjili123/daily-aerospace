# 博查搜索与钉钉加签设计

日期：2026-07-24

## 目标

将发现阶段的 Tavily 搜索完整替换为博查 Web Search API，并为钉钉机器人通知增加“加签”安全模式支持。替换不得改变中国境内情报范围、项目生命周期串联、三个月项目池、日报模块顺序或每日北京时间 07:30 的调度。

当博查 API 暂时不可用时，流水线继续使用官方种子来源和已有项目池生成日报。日报必须说明安全、具体的失败类别，而不是只显示笼统的“搜索覆盖降级”。

## 非目标

- 不保留 Tavily 运行时兼容或供应商切换开关。
- 不引入博查 AI Search 的模态卡能力；本次只使用 Web Search API。
- 不改变 DeepSeek 分析、证据核验、项目匹配、报告结构或钉钉消息数量。
- 不在仓库、日志、报告或测试快照中保存真实密钥。

## 方案选择

采用“完整替换”：

- `TavilyProvider` 替换为 `BochaProvider`。
- `TAVILY_API_KEY` 替换为 `BOCHA_API_KEY`。
- Tavily 专属配置、指标、日志事件、测试名称和文档全部改为供应商中立或博查名称。
- 不保留已弃用别名，缺少新环境变量时快速失败。

该方案比在 Tavily 命名下只替换请求地址更清晰，也避免了用户不需要的双供应商复杂度。

## 组件设计

### 博查搜索适配器

`BochaProvider` 继续实现现有流水线使用的 `search(SearchQuery) -> list[Candidate]` 行为，并保留累计调用次数。

请求：

- 地址：`https://api.bochaai.com/v1/web-search`
- 方法：`POST`
- 鉴权：`Authorization: Bearer <BOCHA_API_KEY>`
- 请求体：
  - `query`：查询文本
  - `freshness`：`noLimit`
  - `summary`：`true`
  - `count`：`10`

响应从 `webPages.value` 读取。每条结果映射为：

- `title` ← `name`
- `url` ← `url`
- `summary` ← 非空 `summary`，否则使用 `snippet`，两者均缺失时使用空字符串
- `discovery_source` ← `bocha`

标题或 URL 缺失、根对象类型错误、`webPages.value` 不是列表或结果字段类型错误，均视为响应格式异常。适配器不记录响应正文。

### 搜索故障分类

流水线继续执行，但在运行指标中记录去重后的安全故障类别：

| 条件 | 内部类别 | 日报文本 |
|---|---|---|
| HTTP 401/403 | `authentication` | 博查 API 认证失败 |
| HTTP 429 | `quota_or_rate_limit` | 博查 API 配额不足或触发限流 |
| 连接错误或超时 | `network_or_timeout` | 博查 API 网络连接或请求超时 |
| HTTP 5xx | `server_error` | 博查 API 服务端异常 |
| 其他非 2xx | `request_rejected` | 博查 API 请求被拒绝 |
| 成功响应无法安全解析 | `invalid_response` | 博查 API 返回格式异常 |

相同类别一天只展示一次。官方种子域失败信息继续与博查 API 故障并列展示。异常字符串、响应正文、请求头和带凭证 URL 不进入运行指标、日志或报告。

原 `tavily_usage` 指标改为 `search_api_usage`，反映博查调用次数。运行指标不是持久化项目状态的一部分，因此不增加项目数据迁移。

### 钉钉加签通知

`DingTalkNotifier` 新增必需的 `secret` 参数。每次发送时：

1. 生成当前 Unix 时间的毫秒时间戳。
2. 构造待签名字符串：`<timestamp>\n<DINGTALK_SECRET>`。
3. 使用密钥执行 HMAC-SHA256。
4. 对摘要进行 Base64 编码。
5. 将 `timestamp` 和 `sign` 作为查询参数添加到原 Webhook，同时保留 `access_token` 及其他既有参数。
6. 发送原有的单条 Markdown 消息体。

时间提供器可注入，以便测试生成确定性签名。最终请求 URL、Webhook、`access_token`、加签密钥和签名不会写入日志或异常消息。

## 配置与工作流

运行时必需的 GitHub Repository secrets：

- `DEEPSEEK_API_KEY`
- `BOCHA_API_KEY`
- `DINGTALK_WEBHOOK`
- `DINGTALK_SECRET`

配置模型将 `tavily_api_key`、`TavilySettings` 和 `tavily` 配置段替换为对应的博查名称。GitHub Actions 将四个 Secrets 传入流水线。README、示例配置和运维步骤同步更新，不再出现 Tavily 运行说明。

## 数据流

1. 查询规划器生成增量检索及项目跟踪查询。
2. 博查适配器返回未核验候选链接。
3. 官方种子采集器并行提供 A 级候选来源。
4. 候选链接统一去重、抓取、分析、证据核验和项目串联。
5. 博查失败时记录安全故障类别，继续处理官方候选及项目池。
6. 报告渲染器在“覆盖”区域输出具体故障说明。
7. `dry_run=true` 只生成报告和状态 artifact。
8. 正式运行时，钉钉通知器生成一次性签名并发送一条完整 Markdown 消息。

## 测试

新增或更新以下测试：

- 博查请求地址、Bearer 头和请求体。
- `webPages.value` 的标题、URL、摘要和 snippet 回退映射。
- 调用计数。
- 401/403、429、连接/超时、5xx、其他非 2xx 和畸形成功响应的分类。
- 日报展示去重后的具体 API 故障原因及失败域。
- 所有搜索错误路径均不泄露 API Key 或响应正文。
- 钉钉固定时间戳下的 HMAC-SHA256 签名。
- 已有 Webhook 查询参数的保留和编码。
- 钉钉请求、HTTP 错误、超时和业务错误均不泄露 Webhook、密钥或签名。
- 配置模型要求四个环境变量。
- GitHub Actions 和 README 使用四个新 Secrets，且不再要求 Tavily。
- 完整测试套件、覆盖率、Python 编译和敏感信息扫描继续通过。

## 发布与验收

1. 在 `codex/bocha-dingtalk-signing` 分支实现并运行完整测试。
2. 推送分支并创建 PR 到 `main`。
3. 用户在 GitHub 添加 `BOCHA_API_KEY`；其他三个 Secrets 已配置。
4. 合并后手动运行 `dry_run=true`，检查 Actions 日志和 artifact，不发送钉钉。
5. 确认博查结果、原始来源链接、具体降级原因和日报结构正确。
6. 再运行一次 `dry_run=false`，确认钉钉群只收到一条带有效加签的 Markdown 日报。

