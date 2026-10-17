# 升级指南（UPGRADE）

> 原则：你的所有个性化都在工作区（`~/AutoBiz`），升级只换内核。每版迁移见 CHANGELOG.md。

```bash
# 1. 先备份（升级前必做）
bash scripts/backup.sh

# 2. 拉新版本
git pull                       # 或下载新版解包覆盖内核目录

# 3. 应用数据库迁移
autobiz migrate                # docker 模式先 docker compose -f docker-compose.app.yml build app 重建镜像

# 4. 数据与本体一致性检查（改过本体的版本必跑）
autobiz onto-check-data

# 5. 刷新工作说明（类型 / 闸门清单变了）
autobiz agents-md ~/AutoBiz/AGENTS.md

# 6. 体检收尾
autobiz doctor
```

## 出问题时

- 迁移是编号幂等的（`schema_migrations` 记录），重跑无害；
- `doctor` 红灯按提示逐条处理；数据库坏了用最近备份恢复（`docs/BACKUP.md`）；
- 大版本（本体破坏性变更）会在 CHANGELOG 顶部给出专门的迁移说明。

## Docker 用户

`./bin/autobiz` 的每次执行都用当前代码构建的镜像；升级后记得
`docker compose -f docker-compose.app.yml build app` 再跑上面 3–6 步。
