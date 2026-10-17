# 排障指南（TROUBLESHOOTING）

> 按 `autobiz doctor` 的检查项逐条给解法。docker 模式命令前缀是 `./bin/autobiz`。

## 数据库连接 ✗

- `docker compose up -d db`（local 模式）或 `docker compose -f docker-compose.app.yml up -d db`
- 端口冲突：`.env` 设 `AUTOBIZ_DB_PORT=5434`，local 模式同步改 `DATABASE_URL`
- 密码不对（一直能跑突然连不上）：多半是 `.env` 的 `POSTGRES_PASSWORD` 与建库时不一致；
  数据卷在则密码以首次初始化为准——改回 `.env` 或重置卷（会丢数据，先备份）

## 扩展 ✗（vector / pg_trgm）

镜像必须是 `pgvector/pgvector:pg16` 而不是官方 `postgres`。用错镜像只能重建
（`docker compose down -v` 后换镜像 `up -d`，**先备份**）。

## 迁移待应用

`autobiz migrate`。报某条 SQL 失败：看 `autobiz/migrations/` 对应文件是否被改动过——
迁移文件一旦发布不可修改，只能新增编号。

## 本体编译 ✗

- 报「重复定义」：你工作区 `ontology/` 里有与内核同名的定义，加 `extend: true` 或改名
- 报「不允许修改已有定义」：覆盖层想改内核已有字段类型/值，这被禁止——改用追加
- 报枚举/属性不存在：YAML 缩进或字段名笔误，对照 `ontology/shared.yaml` 头部格式约定

## 向量维度不一致

换过 embedding 模型（models.yaml 的 dim）后需要全量重算：当前版本需重建库
（后续版本提供 `autobiz reembed`）。没换过模型却报警 → 数据库是旧库，跑 `autobiz migrate`。

## 模型 API Key ✗

`.env` 里 `DEEPSEEK_API_KEY`（或你路由用的 provider 对应的 Key）。报「余额不足 /
Insufficient Balance」：充值后重跑 `autobiz extract` 会从失败处继续。

## 今日花费超预算

`config/models.yaml` 的 `budget.daily_cny` 调高，或等明天。这是防跑飞的保险丝，不是 bug。

## 备份 ✗

- `bash scripts/backup.sh` 手动跑一次看报错
- 「工作区 backups/ 为空」：还没跑过备份，跑一次并配定时任务（`scripts/schedule/`）
- 恢复演练：`bash scripts/restore_check.sh`（建议每月一次）

## 审批 PIN ✗

`autobiz setup-pin`。忘了 PIN：删除 `$AUTOBIZ_HOME/config/approver.json` 后重新设置
（进行中的待审批码会作废，让 Agent 重新预览生成）。

## Agent 确认闸门被拒（"必须先预览" / "审批码"）

这是设计不是故障：先不带 `--confirm` 预览 → 你在终端 `autobiz approve <码>` →
参数里加 `"approval":"<码>"` 再 `--confirm`。详见 QUICKSTART 第 3 步。
