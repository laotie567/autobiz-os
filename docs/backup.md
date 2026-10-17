# 备份与恢复（H-1，2026-10-16 重写）

- **内容**：数据库 dump + 工作区文件（config / ontology 覆盖层 / proposals / outbox / seed）一起打包——只备数据库不够，你的个性化配置在工作区
- **本机**：`$AUTOBIZ_HOME/backups/autobiz-日期.tar.gz`，滚动保留 30 份
- **云端（可选）**：`AUTOBIZ_BACKUP_REMOTE=feishu`（lark-cli）或 `s3|oss|rclone`，**按日期留存**（不再原地覆盖——单份覆盖一次坏备份就毁掉唯一云端副本）
- **加密（可选）**：`.env` 设 `AUTOBIZ_BACKUP_RECIPIENT=<age 公钥指纹>` 后备份为 `.age` 密文；**私钥离线保管，私钥丢失 = 备份不可恢复**
- **自动**：定时任务模板见 `scripts/schedule/`（macOS launchd / Linux systemd / Windows 计划任务）
- **手动**：`bash scripts/backup.sh`

## 恢复

```bash
tar -xzf backups/autobiz-日期.tar.gz                      # 得到 db.dump 与 files.tar
# 加密备份先：age -d -i 私钥 -o bak.tar.gz backups/autobiz-日期.tar.gz.age && tar -xzf bak.tar.gz
docker compose up -d db
docker exec autobiz-db psql -U autobiz -d postgres -c "drop database autobiz" -c "create database autobiz"   # ⚠️ 清空当前数据
docker exec -i autobiz-db pg_restore -U autobiz -d autobiz --no-owner < db.dump
tar -C "$AUTOBIZ_HOME" -xf files.tar                       # 工作区文件原位还原
```

## 恢复校验（建议每月一次）

```bash
bash scripts/restore_check.sh
```

把最新备份恢复到临时库比对行数。备份只有**能恢复的**才算备份——本机+云端全丢而
从未演练过恢复，是数据事故的标准剧本。
