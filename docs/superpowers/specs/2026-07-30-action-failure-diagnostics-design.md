# Actions 失败诊断与产物分流设计

日期：2026-07-30  
状态：用户已批准设计方向，待书面规格复核  
开发分支：`codex/verification-promotion-20260728`

## 1. 背景

手动运行 Actions #24 时，测试和 dry-run 分支保护均通过，但主程序步骤
`Run daily pipeline` 在约 2 分 35 秒后失败：

```text
cli_failure code=pipeline error=TypeError
Process completed with exit code 4.
```

现有 CLI 只记录异常类型，不记录失败阶段和代码位置，因此无法判断
`TypeError` 发生在流水线执行、报告渲染还是文件写入。

workflow 的报告摘要和 Artifact 上传均使用 `if: always()`。失败后，它们会继续读取并
上传仓库中原有的 `reports/`、`data/`，导致 Actions 页面显示 2026-07-26 的旧日报，
Artifact 也只包含旧数据。这会让“执行失败”和“旧产物仍可下载”同时出现，容易误判。

## 2. 目标

1. 未捕获异常发生时，明确记录失败阶段、异常类型和仓库代码位置。
2. 诊断不得记录 Secrets、HTTP 请求头、异常消息、局部变量、网页正文或模型响应。
3. 成功运行只展示、上传本次成功生成的日报与数据。
4. 失败运行不展示或上传旧日报，只展示、上传本次失败诊断。
5. 保持退出码语义不变：配置失败为 2、通知失败为 3、流水线失败为 4。
6. 保持 workflow 仅手动触发、强制 `--dry-run`、基础预算 12 次和弹性预算最多 3 次。

## 3. 非目标

- 不在本轮直接修复 Actions #24 中尚未定位的 `TypeError`。
- 不吞掉 `TypeError` 或把程序缺陷降级成业务成功。
- 不记录完整异常消息、完整 traceback 文本或任何局部变量。
- 不更新 GitHub Actions 依赖版本，也不处理 Node.js 20 弃用警告。
- 不处理 checkout 收尾阶段的 Git 128 警告。
- 不修改 Secrets、仓库可见性、workflow 触发/启停、定时配置或钉钉配置。
- 不设置 `dry_run=false`，不发送钉钉，不合并 PR。

## 4. 方案选择

采用“结构化诊断 + 成功/失败产物分流”。

未采用：

- **只开启完整 traceback**：虽然改动最少，但可能输出异常消息，并且无法解决旧摘要和
  旧 Artifact 误导问题。
- **捕获 `TypeError` 后继续运行**：会把程序错误伪装成采集成功，违反
  “Actions 成功不代表业务成功”的验收原则。

## 5. CLI 诊断设计

### 5.1 阶段划分

CLI 将当前大范围的 `pipeline` 异常阶段拆分为稳定阶段名：

- `pipeline_build`
- `pipeline_run`
- `report_render`
- `report_write`
- `diagnostics_write`
- `notification`

退出码保持不变。上述前五个运行阶段发生异常时仍返回 4；通知阶段仍返回 3。

### 5.2 安全堆栈

失败日志和诊断文件只包含：

- `stage`
- `error_type`
- 发生时间
- traceback 中属于本仓库 Python 文件的相对路径、行号和函数名

明确禁止包含：

- `str(error)` 或异常消息
- traceback 局部变量
- 环境变量
- HTTP URL 查询参数或请求头
- 网页正文、搜索响应、模型请求或响应

如果 traceback 中没有可识别的仓库帧，记录空帧列表并保留阶段与异常类型。

### 5.3 失败诊断文件

失败时原子写入：

```text
data/failure-diagnostics.json
```

结构固定为：

```json
{
  "schema_version": 1,
  "status": "failure",
  "stage": "pipeline_run",
  "error_type": "TypeError",
  "occurred_at": "2026-07-30T20:42:00+08:00",
  "frames": [
    {
      "path": "src/laser_space_daily/pipeline.py",
      "line": 123,
      "function": "run"
    }
  ]
}
```

每次 CLI 启动已成功加载配置后，先清理上一次生成的
`data/failure-diagnostics.json`。成功运行不得遗留失败诊断文件。

若写诊断文件本身失败，CLI 仍保留原始退出码，并在日志中只记录
`diagnostic_write_failed` 与异常类型，不覆盖最初的失败阶段。

### 5.4 成功运行清单

成功完成报告和候选诊断写入后，CLI 原子写入：

```text
data/run-result.json
```

文件只包含本次运行的稳定元数据：

```json
{
  "schema_version": 1,
  "status": "success",
  "occurred_at": "2026-07-30T20:42:00+08:00",
  "report_path": "reports/2026-07-30.md"
}
```

workflow 只能通过该清单取得本次报告路径，不得再按目录排序选择“最新报告”。
CLI 启动时同时清理旧的 `data/run-result.json`；运行失败时不得产生成功清单。

## 6. Workflow 产物分流

`Run daily pipeline` 增加稳定步骤 ID，供后续步骤判断其结果。

成功路径：

1. 仅当主程序步骤成功时发布日报摘要。
2. 摘要从 `data/run-result.json` 读取精确 `report_path`；清单无效或报告不存在时让摘要
   步骤失败，不回退到“目录中最新旧文件”。
3. 仅成功时上传 `daily-intelligence-report`，内容保持 `reports/` 与 `data/`。

失败路径：

1. 不执行旧的日报摘要步骤。
2. 新增失败摘要步骤，读取 `data/failure-diagnostics.json`，展示阶段、异常类型和安全帧。
3. 新增 `daily-intelligence-failure-diagnostics` Artifact，只上传
   `data/failure-diagnostics.json`。
4. 如果失败发生在诊断文件产生之前，失败摘要明确写
   “未生成结构化诊断，请查看失败步骤日志”，不得展示旧日报。

## 7. 数据流

```text
CLI 启动
  -> 清理旧失败诊断和旧成功清单
  -> pipeline_build
  -> pipeline_run
  -> report_render
  -> report_write / diagnostics_write
  -> 成功：run-result.json + 精确日报摘要 + 成功 Artifact
  -> 失败：安全诊断 JSON + 失败摘要 + 失败 Artifact
```

## 8. 测试

CLI 单元测试：

- 各阶段异常映射到正确的稳定阶段名和既有退出码。
- `TypeError` 诊断只包含相对路径、行号、函数名和异常类型。
- 诊断不包含异常消息、模拟密钥、URL 查询参数或局部变量。
- 失败诊断使用原子写入。
- 成功运行会清理旧失败诊断并写入包含精确报告路径的 `run-result.json`。
- 失败运行不会遗留旧 `run-result.json`。
- 诊断写入失败不会覆盖原始退出码。

workflow 配置测试：

- workflow 仍只有 `workflow_dispatch`。
- 主程序仍强制 `--dry-run --discovery-mode daily --max-queries 12`。
- 成功摘要和成功 Artifact 只在主程序成功时运行。
- 成功摘要只按 `run-result.json` 中的精确路径读取报告。
- 失败摘要和失败 Artifact 只在主程序失败时运行。
- 失败 Artifact 只包含 `data/failure-diagnostics.json`。
- workflow 不包含钉钉 Secrets、提交、推送、定时触发或状态回写。

回归测试：

- 全量测试通过。
- `git diff --check` 通过。

## 9. 验收标准

下一次相同类型失败时：

- Actions 页面能看到 `stage`、`error_type` 及至少一个安全代码帧；
- 页面不再显示 2026-07-26 等旧日报；
- Artifact 名称为 `daily-intelligence-failure-diagnostics`，且只含本次诊断文件；
- 主程序仍以退出码 4 失败，不能被标记为成功；
- 不发送钉钉。

下一次成功时：

- 页面展示本次新日报；
- 上传正常 `daily-intelligence-report`；
- `data/run-result.json` 精确指向本次生成的报告；
- Artifact 中不存在旧的失败诊断文件；
- 业务是否真正成功仍以候选、已核实数量和覆盖状态判断，而不是仅看绿色 Actions 状态。
