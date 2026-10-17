#!/usr/bin/env bash
# 恢复校验（H-1）：把最新备份恢复到临时库，比对行数与生产库差异。
# 建议每周跑一次（cron / launchd）；失败说明备份不可用——备份只有能恢复的才算备份。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck disable=SC1091
[ -f "$ROOT/.env" ] && set -a && . "$ROOT/.env" && set +a
HOME_DIR="${AUTOBIZ_HOME:-$HOME/AutoBiz}"
DEST="${AUTOBIZ_BACKUP_DIR:-$HOME_DIR/backups}"

LATEST=$(ls -1t "$DEST"/autobiz-*.tar.gz* 2>/dev/null | head -1 || true)
[ -n "$LATEST" ] || { echo "✗ 没有可校验的备份"; exit 1; }
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

case "$LATEST" in
  *.age)
    command -v age >/dev/null 2>&1 || { echo "✗ 备份已加密但未装 age / 未配置私钥"; exit 1; }
    age -d -i "${AUTOBIZ_BACKUP_KEY:-$HOME/.config/age/key.txt}" -o "$TMP/bak.tar.gz" "$LATEST" ;;
  *) ln -s "$LATEST" "$TMP/bak.tar.gz" ;;
esac
tar -C "$TMP" -xzf "$TMP/bak.tar.gz" db.dump

docker exec -i autobiz-db psql -U autobiz -d postgres -c "DROP DATABASE IF EXISTS autobiz_restore_check" >/dev/null
docker exec -i autobiz-db psql -U autobiz -d postgres -c "CREATE DATABASE autobiz_restore_check" >/dev/null
docker exec -i autobiz-db pg_restore -U autobiz -d autobiz_restore_check --no-owner < "$TMP/db.dump"

for t in objects links action_log; do
  PROD=$(docker exec autobiz-db psql -U autobiz -d autobiz -tAc "SELECT count(*) FROM $t")
  CHK=$(docker exec autobiz-db psql -U autobiz -d autobiz_restore_check -tAc "SELECT count(*) FROM $t")
  echo "$t: 生产 $PROD / 备份 $CHK"
done
docker exec -i autobiz-db psql -U autobiz -d postgres -c "DROP DATABASE autobiz_restore_check" >/dev/null
echo "✓ 恢复校验完成（$LATEST）——行数应为生产 ≥ 备份，差值在备份后新增范围内即正常"
