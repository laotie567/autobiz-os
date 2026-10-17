#!/usr/bin/env bash
# 每日备份（H-1/E-1 重写，2026-10-16）：
#   数据库 dump + 工作区文件（config/ontology/proposals/outbox/seed）一起打包；
#   本机滚动保留 30 份；远端按日期留存（不再原地覆盖）；可选 age 公钥加密。
# 由 scripts/schedule/ 里的定时模板调用，也可手动运行。恢复：见 docs/backup.md
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck disable=SC1091
[ -f "$ROOT/.env" ] && set -a && . "$ROOT/.env" && set +a

HOME_DIR="${AUTOBIZ_HOME:-$HOME/AutoBiz}"
DEST="${AUTOBIZ_BACKUP_DIR:-$HOME_DIR/backups}"
mkdir -p "$DEST"
TS=$(date +%Y%m%d-%H%M)
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# 1. 数据库
docker exec autobiz-db pg_dump -U autobiz -Fc autobiz > "$TMP/db.dump"
[ -s "$TMP/db.dump" ] || { echo "✗ 数据库备份为空" >&2; exit 1; }

# 2. 工作区文件（忽略不存在的目录——不阻断备份）
tar -C "$HOME_DIR" -cf "$TMP/files.tar" --ignore-failed-read config ontology prompts overrides proposals outbox seed videos 2>/dev/null || true

# 3. 打包 + 可选加密（设置了 AUTOBIZ_BACKUP_RECIPIENT 且装有 age 时启用；私钥离线保管）
if [ -n "${AUTOBIZ_BACKUP_RECIPIENT:-}" ] && command -v age >/dev/null 2>&1; then
  OUT="$DEST/autobiz-$TS.tar.gz.age"
  tar -C "$TMP" -czf - db.dump files.tar | age -r "$AUTOBIZ_BACKUP_RECIPIENT" > "$OUT"
else
  OUT="$DEST/autobiz-$TS.tar.gz"
  tar -C "$TMP" -czf "$OUT" db.dump files.tar
fi
echo "✓ 本机备份 $OUT ($(du -h "$OUT" | cut -f1))"

# 4. 本机滚动 30 份
ls -1t "$DEST"/autobiz-*.tar.gz* 2>/dev/null | tail -n +31 | xargs -r rm -f

# 5. 远端：按日期留存多版本（修复原"原地覆盖只留一份"的问题）
case "${AUTOBIZ_BACKUP_REMOTE:-none}" in
  s3|oss|rclone)
    command -v rclone >/dev/null 2>&1 && rclone copy "$OUT" "autobiz-remote:autobiz-backups/" \
      && echo "✓ 远端(rclone)已留存 $(basename "$OUT")" ;;
  feishu)
    if command -v lark-cli >/dev/null 2>&1; then
      (cd "$DEST" && lark-cli drive +upload --as user --file "$(basename "$OUT")") >/dev/null \
        && echo "✓ 飞书云空间已留存 $(basename "$OUT")"
    fi ;;
  none) ;;
esac
