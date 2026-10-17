# 安全说明（SECURITY）

## 报告漏洞

- GitHub：请使用仓库的 **Security Advisories**（Private vulnerability reporting），勿直接开公开 issue。
- Gitee / 其他渠道：通过仓库站内私信联系维护者。
- 请给足复现步骤与影响面描述；我们会在 72 小时内确认收到。

## 安全模型（v1.0）

系统假设运行者本人是可信主体，AI Agent 是**不可自行越权**的操作方：

| 层 | 机制 |
|---|---|
| 身份 | CLI / MCP 默认 `agent` 角色；`owner` 需运行者显式设置环境变量 |
| 预览纪律 | agent 确认任何写入前，引擎要求存在同参数的预览记录，且预览后目标未被修改 |
| 人工闸门 | 闸门行动（成交 / 结项 / 确认知识等）由审批码 + 终端 `autobiz approve` 把守，Agent 无交互终端无法自批；启用 PIN 后所有角色一致 |
| 字段 | 生命周期字段（status / received 等）只能由流程行动修改 |
| 出口 | 对外内容经脱敏出口检查器（客户名 / 手机号 / 证件号 / 地址 / 金额） |

## 运行环境要求

- 数据库仅绑定 `127.0.0.1`；不要把端口暴露到公网。
- `.env` 含 API Key，已被 .gitignore 忽略——**永远不要提交或整目录打包分发**。
  官方发布物一律由 `git archive` 生成。
- 分发 / 多人使用前务必 `autobiz setup-pin`（`autobiz doctor` 会检查）。
