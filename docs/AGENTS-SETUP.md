# 接入 AI Agent（AGENTS-SETUP）

> 核心思路：Agent 通过**命令行**操作系统（`AGENTS.md` 是它的工作说明），不需要任何 API 对接。

## 通用（任何能跑 shell 的 Agent）

1. 生成工作说明（在工作区根目录，即你运行命令的目录）：

   ```bash
   autobiz agents-md ~/AutoBiz/AGENTS.md      # docker 模式：./bin/autobiz agents-md，输出粘到工作区
   ```

2. 让 Agent 的**工作目录指向工作区**（`~/AutoBiz`）或仓库根，并把命令前缀告诉它
   （docker 模式是 `./bin/autobiz`，本机模式是 `uv run autobiz`；AGENTS.md 里写的
   是 `uv run autobiz`，docker 用户请让 Agent 全局替换为 `./bin/autobiz`，或做个 alias）。

3. Agent 遵守的规矩写在 AGENTS.md 里，引擎会强制：先预览后确认、闸门要审批码、
   未确认知识不进对外内容。

## 豆包工作（字节）

自定义连接器 → 添加「命令行」工具，工作目录设为仓库根，指令贴 AGENTS.md 全文。

## Claude Code / Codex / 其他 CLI Agent

仓库根已有 `AGENTS.md`，直接在仓库目录启动即可被自动读取。docker 模式建议在
`~/.zshrc` 加 `alias autobiz='./bin/autobiz'`，让 Agent 无感切换。

## Cursor / Windsurf

Rules 里引用 AGENTS.md 内容；终端执行同样加 alias。

## 安全提示

- Agent 默认 `agent` 角色本来就确认不了闸门（审批码必须你本人在终端 `approve`），这是双保险。
- 不要把 `AUTOBIZ_ROLE=owner` 写进 Agent 能看到的环境配置里。
- 多人共用一台机器时，务必已 `setup-pin`。
