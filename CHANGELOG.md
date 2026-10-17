# 更新日志（CHANGELOG）

本项目的语义化版本：主版本 = 本体模型破坏性变更；次版本 = 新行动 / 新域；修订 = 修复。
每个版本的迁移（`autobiz/migrations/`）会在此标注，升级步骤见 docs/UPGRADE.md。

## 1.0.1（2026-10-17）安装链补全：ghcr 镜像发布

### 修复
- Dockerfile 补齐运行时资产（`ontology/`、`prompts/`、`config/models.yaml`、`templates/`）——此前镜像构建后容器里 `autobiz check` 直接失败

### 新增
- 应用镜像发布到 `ghcr.io/laotie567/autobiz-os`（国际仓，内置向量模型）；`install.sh` docker 模式改为默认拉镜像，拉取失败自动回退本地构建

## 1.0.0（2026-10-17）首个正式版（含 S1–S4 与发布前加固）

### 修复（2026-10-17 评审整改）
- `merge_objects` 迁移关系改经 store 层（关系类型 / 已确认校验 + 变更入审计），不再裸写 links
- 角色提权（`AUTOBIZ_ROLE=owner`）只在交互式终端生效：Agent 的非交互 shell 一律 agent，闸门无条件走审批码（不看 PIN 是否配置）
- 抽取失败状态写入修复：失败路径原用 `update_object` 写 status 被 B-1 托管字段守卫拒绝（失败文档永远留在待抽取队列），改由抽取流程自身落 `status=failed`
- 成本闸门 fail-closed：记账查询失败时模型调用拒绝执行（此前静默放行）；用量记账失败改为 stderr 告警；overview 花费查询失败露出错误
- `cli` / `migrate` 冒烟测试补齐（tests/test_ops_smoke.py）；allowlist 移除不存在文件的残留排除项

### 迁移
无新数据库迁移（0001–0005）；升级后运行 `uv run autobiz init` 重编译视图。

### 新增（S4）线下政企项目 + OPC 招商（ADR-0018）
- 拍板口径台账 `decision` + `record_decision`（替代留痕）+ 查询 `decision_ledger`；方案生成 / 改稿注入口径、禁用说法自动重写
- 方案计价方式 `pricing`（产品标价 / 人天 / 总价）、`man_days`、签约主体 `entity`、采购阶段 `procurement_stage` + `set_procurement_stage`
- 客户类型增加国企 / 平台公司、政府 / 事业单位；会议 / 线下新客户默认线下项目线
- 私有模块 OPC 基地招商：空间 / 入驻 / 招商活动、出租率与招商漏斗、日报 OPC 一行（不随发布）

### 新增（S3）数据回流（ADR-0017）
- `backfill_batch`：截图识别后批量回填（id / 链接 / 标题匹配，不唯一不写）
- 发布记录 `keyword`；线索按私信关键词自动归因到近 14 天的发布（同平台优先）；`attribute_lead` 人工认领
- 查询 `format_performance`（形态 / 关键词线索率 + 配比建议）、`unattributed_leads`、`conversion_stats`（实测转化）
- 周报新增形态线索率、关键词带客、配比建议、实测转化、待认领线索；日报回填提示

### 新增（S2）内容快产线（ADR-0016）
- 内容件 `content_piece` 与状态机；行动 `plan_week`（AI 出候选选题）、`finalize_topics`（批量定档闸门）、
  `schedule_pieces`（按配比排产，真人口播每日硬指标）、`draft_piece` / `revise_piece`（AI 稿 + 代码体检，自动重写一次）、
  `review_piece`、`mark_produced`、`publish_piece`（多平台各登记一次）、`drop_piece`
- 8 种形态写法提示词 `prompts/format_*.v1.md` + `content_draft.v1` + `plan_topics.v1`；模型任务 `content_draft` / `plan_topics`
- 个人可宣称事实台账 `config/claims.yaml`（示例 `claims.example.yaml`）；profile 新增 `brand`、`avatar`
- 查询 `review_queue` / `content_plan` / `piece_package`；命令 `autobiz draft [N] [--revise]`；日报加待审 / 今日排期
- doctor 增加"个人事实台账"检查

#### 修复
- 提示词加载在用户覆盖目录不存在时报错（未固定版本的提示词才会触发）

### 新增（S1）经营驾驶舱（ADR-0015）
- 业务线维度 `business_line`（online / offline / partner），客户 / 线索 / 项目 / 订单按来源自动给默认值
- 订单 `order`：`record_order`（直接写入）、`refund_order`（闸门）
- 线索跟进：`quick_lead`、`log_contact`、`mark_lead_lost`；查询 `followup_today`
- 顾问咨询结论 `record_consultation`（软提醒：没咨询就出方案会提示，不拦截）
- 发布记录内容形态 `format`（8 种，数字人预留）
- 驾驶舱查询 `goal_progress`、`revenue`；`receivables` 增加"已逾期 / 7 天内到期"
- `autobiz brief [--weekly] [--send]` 每日简报 / 每周复盘，飞书群机器人推送（签名校验）+ 定时模板
- profile 新增 `goals`、`locale.timezone`；`.env` 新增 `FEISHU_BOT_WEBHOOK` / `FEISHU_BOT_SECRET`；doctor 增加两项检查

#### 修复
- 视图编译 `CREATE OR REPLACE VIEW` 在给已有类型加属性时报错 → 恢复先 DROP 再 CREATE

#### 升级
- 无数据库迁移；升级后运行 `uv run autobiz init` 重编译视图

## 1.0.0-rc1（2026-10-16）首个开源候选版

首个对外发布版本，自私有实例（v0.3→v0.4 演进）去个人化而来。

### 安全与治理（相对私有版的核心加固）

- 人工闸门引擎强制：agent 默认身份、确认前必须预览、闸门审批码 + `autobiz approve` + 可选 PIN
- 生命周期字段保护（status / received 等只能由流程行动修改）
- 可抽取知识一律未确认入库，确认必须走 `verify_object`
- 并发行锁（收款不丢账）、金额最大余数分摊（和恒等于总价）
- 脱敏出口检查器（客户名 / 手机号 / 证件号 / 地址 / 金额）
- 数据库编号迁移（0001–0005）、`autobiz doctor` 体检、`onto-check-data` 一致性检查

### 已知限制

- 视频生产板块未随包分发（后续以可选插件形式提供）
- `privacy.send_customer_names_to_llm` 为提示性质，自动假名化在路线图中
- 托管 / 多租户形态暂不提供（架构已预留内核 / 工作区分离）

### 迁移

新装无迁移负担（`autobiz init` 自动应用 0001–0005）。
