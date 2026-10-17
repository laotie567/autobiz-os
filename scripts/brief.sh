#!/usr/bin/env bash
# 每日简报 / 每周复盘推送到飞书（ADR-0015）。定时任务调用：
#   scripts/brief.sh            日报（建议每天 08:00）
#   scripts/brief.sh --weekly   周报（建议每周一 08:30）
# 需要 .env 里配好 FEISHU_BOT_WEBHOOK / FEISHU_BOT_SECRET；数据库容器在运行。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
# 定时任务环境的 PATH 很短：补上 uv 常见安装位置
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
LOG_DIR="${AUTOBIZ_HOME:-$HOME/AutoBiz}/logs"
mkdir -p "$LOG_DIR"
{
  echo "=== $(date '+%F %T') brief $*"
  uv run autobiz brief "$@" --send
} >> "$LOG_DIR/brief.log" 2>&1
