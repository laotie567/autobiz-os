# 安装指南（INSTALL）

> 目标：**15 分钟**从零跑到第一张行业地图。三种安装路径按需选择。
> 出问题随时 `autobiz doctor`（docker 模式是 `./bin/autobiz doctor`），逐项会给修复提示。

## 前置：你要准备什么

- 一台能装 Docker 的电脑（macOS / Windows / Linux 均可；Windows 建议 WSL2）
- 一个大模型 API Key（默认 DeepSeek，[platform.deepseek.com](https://platform.deepseek.com) 注册充值即可；
  没有 Key 也能装，AI 分析/方案类功能暂不可用，查询功能照常）

## 路径 A：一键安装（推荐，本机只装 Docker）

```bash
git clone <仓库地址> && cd AutoBiz_OS     # 或下载 zip 解压
bash scripts/install.sh
```

脚本会问 5 个问题（称呼 / 一句话业务 / 目标客户 / API Key / 模式），然后自动：
建工作区（默认 `~/AutoBiz`）→ 生成随机数据库密码 → 起数据库 → 构建应用镜像（内置向量模型，
构建几分钟）→ 初始化 → 导入示例行业地图 → 引导设置审批 PIN → 体检。

之后所有命令用 `./bin/autobiz <命令>`（在容器里执行，本机不需要 Python）。

**国内网络**：向量模型走 hf-mirror.com（默认已配）；Docker 镜像拉取慢时配置国内 registry mirror；
`git clone` 慢可用 Gitee 镜像仓库。

## 路径 B：本机跑 Python（开发者 / 想改代码）

```bash
git clone <仓库地址> && cd AutoBiz_OS
cp .env.example .env                      # 填 POSTGRES_PASSWORD 与 DEEPSEEK_API_KEY
export AUTOBIZ_HOME=$HOME/AutoBiz         # 不设则默认 ~/AutoBiz
docker compose up -d db
curl -LsSf https://astral.sh/uv/install.sh | sh   # 已装可跳过
uv sync                                   # .python-version=3.12，uv 会自动装对应 Python
uv run autobiz init && uv run autobiz seed --example
uv run autobiz setup-pin
uv run autobiz doctor
```

## 路径 C：已有 PostgreSQL（自管数据库）

版本 ≥ 14 且装了 `vector`、`pg_trgm` 扩展即可：`.env` 写 `DATABASE_URL` 指向你的库，
其余同路径 B（跳过 compose）。注意数据库账号需能建扩展。

## 安装后

- `docs/QUICKSTART.md`：15 分钟走通"丢需求 → 分析 → 方案 → 成交 → 收款"
- `docs/AGENTS-SETUP.md`：把豆包工作 / Claude Code / Cursor 接到这套系统
- `docs/CUSTOMIZE.md`：改成你自己的行业与流程

## 常见卡点

| 现象 | 处理 |
|---|---|
| 5433 端口被占 | `.env` 设 `AUTOBIZ_DB_PORT=5434` 并同步改 DATABASE_URL |
| 镜像构建下载模型超时 | 重跑一次（断点续传）；或先 `docker pull python:3.12-slim` |
| doctor 报迁移待应用 | `autobiz migrate` |
| doctor 报审批 PIN 未设 | `autobiz setup-pin`（分发/多人使用必设） |
