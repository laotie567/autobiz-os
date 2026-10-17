# 定制指南（CUSTOMIZE）

> 原则：**内核只读，你的个性化全部放工作区**（默认 `~/AutoBiz`）。升级内核不会冲掉你的修改。

## 1. 改你的档案（profile）

编辑 `$AUTOBIZ_HOME/config/profile.yaml`：称呼、一句话业务、目标客户、技术选型偏好、
发布渠道（local_md / feishu）、备份远端。代码与提示词里的"人设"全部从这里注入——
改完立即生效，不用重启什么。

## 2. 加行业字段 / 枚举值（本体覆盖层）

在 `$AUTOBIZ_HOME/ontology/` 下新建 YAML，同名定义写 `extend: true`（只允许追加）：

```yaml
# ~/AutoBiz/ontology/my-store.yaml
domains:
  inventory: 库存          # 加一个新业务域（可选）

enums:
  platform: { extend: true, kuaishou: 快手 }     # 平台加一个枚举值

object_types:
  organization:
    extend: true
    properties: { store_count: int, city: text }  # 客户加两个字段
```

```bash
autobiz check          # 编译校验通过即可用
```

覆盖已有定义（改类型 / 改值）会直接报错——那是故意的，防止升级内核时你的改动被悄悄覆盖。
行业字段沉淀好了欢迎提 PR 收进 `ontology/packs/`（见 CONTRIBUTING.md）。

## 3. 改提示词

- 内核提示词在 `prompts/*.v*.md`，版本由 `config/models.yaml` 每个任务固定（`prompt: proposal.v1`）。
- 想微调：把文件复制到 `$AUTOBIZ_HOME/prompts/overrides/` 同名放置，**覆盖层优先**。
  想升级版本：内核加 vN 文件 + 改 models.yaml 声明 + 跑 `autobiz eval` 回归。

## 4. 换模型 / 调预算

`config/models.yaml`：每个任务可指 provider / model / temperature；`budget.daily_cny` 是
每日模型花费上限（超出自动拒绝调用）；`overview` 里看本月花费。

## 5. 发布渠道与备份

- `profile.integrations.publish`：`local_md`（默认，方案落 `outbox/`）或 `feishu`（需 lark-cli）。
- 备份：`scripts/backup.sh`（含数据库+工作区文件，滚动 30 份）；定时任务模板见
  `scripts/schedule/`；恢复校验 `scripts/restore_check.sh`；加密备份设 `AUTOBIZ_BACKUP_RECIPIENT`（age 公钥）。

## 6. 种子数据

- `autobiz seed --example`：虚构示例（社区烘焙店），随便玩。
- 你自己的行业起点：写 `$AUTOBIZ_HOME/seed/local-seed.yaml`（格式照抄
  `ontology/seed/example-seed.yaml`），`autobiz seed` 会一并导入。
  **真实种子放工作区，绝不进 git。**
