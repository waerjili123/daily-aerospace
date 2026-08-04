# 分类证据合并与独立来源查询实施计划

日期：2026-07-29  
设计依据：`docs/superpowers/specs/2026-07-29-classification-evidence-independent-source-design.md`

## 目标

修复同一页面内类别证据和事件证据分句出现时的误判，并避免定向核验重复搜索
目标自身的 B 级媒体域名。严格来源、日期、主体、字段和多来源门槛保持不变。

## 实施步骤

### 1. 先补核验器失败测试

文件：

- `tests/test_fetch_verify.py`

新增覆盖：

- 同页不同引文分别证明商业航天类别和融资动作时，分类证据通过；
- 缺少类别或事件任一证据时仍然失败；
- 页面级分类通过但只有一个 B 级来源时仍为待核实。

### 2. 实现页面级分类证据集合

文件：

- `src/laser_space_daily/verifier.py`

修改：

- 保留确定性规则一致性和四个证据字段存在性检查；
- 在同一页面的 `in_scope`、`category`、`event_type` 原文集合中分别确认类别
  和事件类型；
- 不允许跨页面拼接，也不改变后续来源等级和独立性检查。

### 3. 先补查询规划器失败测试

文件：

- `tests/test_verification_followup.py`

新增覆盖：

- `pedaily.cn`、`stcn.com` 等目标自身域名被排除；
- `www` 和子域名正确归一化；
- 非 B 级目标仍可使用配置中的 B 级域名；
- 排除后不足三条时不生成重复查询填充预算。

### 4. 实现目标自身域名排除

文件：

- `src/laser_space_daily/verification_followup.py`

修改：

- 从 `target.candidate.url` 提取规范化主机名；
- 生成 B 级 `site:` 查询时跳过相同基础域名及其子域名；
- 官方/投资机构查询保留；
- 查询去重和 3 次弹性预算保持不变。

### 5. 流水线与报告回归

文件：

- `tests/test_pipeline.py`
- `tests/test_report_notifier.py`

确认：

- 新独立来源继续进入抓取、分析和再次核验；
- 基础 12 加弹性最多 3；
- 指标继续准确显示新增来源和重复来源；
- 不新增钉钉发送路径。

### 6. 完整验证与进度记录

执行：

- 相关定向测试；
- 完整 `pytest -q`；
- `git diff --check`；
- 更新 `docs/PROGRESS.md`；
- 提交并推送当前开发分支。

完成离线验证后，再执行一次当前分支、强制 dry-run 的手动 Actions；不会在未经
用户明确授权时发送钉钉。
