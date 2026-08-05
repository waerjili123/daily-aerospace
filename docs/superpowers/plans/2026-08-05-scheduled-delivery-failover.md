# 双定时与每日幂等投递实施计划

1. 新增独立的投递门禁模块，负责按北京时间判断当天成功的正式运行，并通过 GitHub
   Actions 输出决定是否继续。
2. 将 workflow 改为 07:50 主 cron 和 08:20 兜底 cron，增加可识别的运行标题、
   `actions: read` 权限和门禁步骤。
3. 让安装、测试、采集、报告摘要和 Artifact 步骤只在门禁放行时执行；重复运行快速
   成功退出且不读取钉钉 Secrets、不调用博查或 DeepSeek。
4. 添加门禁单元测试和 workflow 静态测试，运行完整测试及 `git diff --check`。
5. 推送分支、创建并合并 PR，从 `main` 手动运行一次 `dingtalk_live` 补发当天日报，
   核对运行标题、报告日期、来源和钉钉 accepted 语义。
