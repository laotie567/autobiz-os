#!/usr/bin/env bash
# AutoBiz OS 安装脚本（macOS / Linux；Windows 用 WSL 或 Git Bash）。
# 目标：15 分钟内从零到"看到第一张行业地图"。
# 两种模式：
#   docker（默认）：本机只装 Docker，一切在容器里跑，命令走 ./bin/autobiz
#   local：本机装 uv + Python 3.12，直接 uv run autobiz（数据库仍用容器）
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
WORK="${AUTOBIZ_HOME:-$HOME/AutoBiz}"

say() { printf '\n\033[1;36m%s\033[0m\n' "$*"; }
die() { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# ── 0. 前置检查 ────────────────────────────────────────
command -v docker >/dev/null 2>&1 || die "未安装 Docker。请先安装 Docker Desktop（macOS/Win）或 docker engine（Linux）后重跑。"
docker compose version >/dev/null 2>&1 || die "Docker Compose 不可用（需 v2）。"
[ -f .env ] && die "已存在 .env——重复安装请直接运行 ./bin/autobiz doctor 排查，或先备份后删除 .env 重装。"

# ── 1. 五个问题 ────────────────────────────────────────
say "第 1 步 / 5：关于你（写入 $WORK/config/profile.yaml，绝不进任何仓库）"
read -r -p "怎么称呼你（对外的名字，出现在方案落款等）[老板]：" OWNER_NAME
OWNER_NAME=${OWNER_NAME:-老板}
read -r -p "一句话说清你的业务（喂给需求分析和方案生成）[为中小企业提供 AI 咨询与落地服务]：" ONE_LINER
ONE_LINER=${ONE_LINER:-为中小企业提供 AI 咨询与落地服务}
read -r -p "你的目标客户是谁 [中小企业老板]：" AUDIENCE
AUDIENCE=${AUDIENCE:-中小企业老板}
read -r -p "你的 DeepSeek API Key（没有可回车跳过，AI 生成功能暂不可用）： " DK
read -r -p "模式：docker=本机只装 Docker（推荐，回车默认） / local=本机已装 uv+Python3.12：" MODE
MODE=${MODE:-docker}

# ── 2. 工作区与配置 ────────────────────────────────────
say "第 2 步 / 5：创建工作区 $WORK"
mkdir -p "$WORK/config" "$WORK/seed"
if [ ! -f "$WORK/config/profile.yaml" ]; then
  sed -e "s|^  name: 老板|  name: $OWNER_NAME|" \
      -e "s|^  one_liner: .*|  one_liner: $ONE_LINER|" \
      -e "s|^  audience: .*|  audience: $AUDIENCE|" \
      config/profile.example.yaml > "$WORK/config/profile.yaml"
fi
PW=$(openssl rand -hex 8 2>/dev/null || head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')

# ── 3. .env 与数据库 ───────────────────────────────────
say "第 3 步 / 5：写 .env 并启动数据库"
if [ "$MODE" = "docker" ]; then
  cat > .env <<ENV
POSTGRES_PASSWORD=$PW
AUTOBIZ_HOST_HOME=$WORK
DEEPSEEK_API_KEY=$DK
ENV
  AUTOBIZ_HOST_HOME="$WORK" docker compose -f docker-compose.app.yml up -d db
else
  cat > .env <<ENV
DATABASE_URL=postgresql://autobiz:$PW@127.0.0.1:5433/autobiz
POSTGRES_PASSWORD=$PW
AUTOBIZ_HOME=$WORK
DEEPSEEK_API_KEY=$DK
ENV
  POSTGRES_PASSWORD="$PW" docker compose up -d db
  command -v uv >/dev/null 2>&1 || die "local 模式需要 uv（curl -LsSf https://astral.sh/uv/install.sh | sh）"
  uv sync
fi

if [ "$MODE" = "docker" ]; then
  # 预构建镜像在 ghcr.io：优先拉取，拉取失败（网络受限）自动回退本地构建
  docker compose -f docker-compose.app.yml pull app >/dev/null 2>&1 \
    || { say "镜像拉取失败，回退本地构建（首次需几分钟）"; docker compose -f docker-compose.app.yml build app >/dev/null; }
  AB() { docker compose -f docker-compose.app.yml run --rm app "$@"; }
else
  AB() { uv run autobiz "$@"; }
fi

# ── 4. 初始化 ──────────────────────────────────────────
say "第 4 步 / 5：初始化数据库 + 导入示例行业地图"
AB init
AB seed --example

# ── 5. 审批 PIN + 体检 ─────────────────────────────────
say "第 5 步 / 5：设置闸门审批 PIN（人工闸门的最后一把锁；分发 / 交给他人使用前必设）"
if AB setup-pin; then echo "✓ PIN 已设置"; else echo "⚠ PIN 未设置（单人自用可暂缓，doctor 会提醒）"; fi

say "体检（全部 ✓ 即安装成功）："
AB doctor || true

cat <<EOF

✅ 安装完成。接下来：
  $([ "$MODE" = docker ] && echo "./bin/autobiz q overview        # 看示例行业地图与待办" || echo "uv run autobiz q overview        # 看示例行业地图与待办")
  丢一个需求试试：$([ "$MODE" = docker ] && echo "./bin/autobiz req \"示例客户\" \"烘焙店老板想做会员复购\"" || echo "uv run autobiz req \"示例客户\" \"烘焙店老板想做会员复购\"")
  15 分钟上手路线：docs/QUICKSTART.md
  出问题先跑：$([ "$MODE" = docker ] && echo "./bin/autobiz doctor" || echo "uv run autobiz doctor")
EOF
