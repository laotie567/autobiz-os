# 业务线、经营目标与每日简报（S1）

2026-10-16。触发：能力缺口评估指出系统"缺时间与目标维度"——能回答"现在有什么"，回答不了"今天先做什么、这个月离目标差多少"；
同时用户拍板把三类收入都纳入本系统，但**月收入目标只算线上博主业务**。

## 决定

- **业务线是公共维度**：枚举 `business_line` = `online` 线上获客业务 / `offline` 线下项目 / `partner` 合伙业务，
  挂在客户、线索、项目、订单上。默认值按获客来源推断：有平台来源的线索、抖音 / 微信渠道的新客户 → `online`；
  线下 / 会议 → `offline`；其余不猜，标"未标注"并提醒补标（未标注不计入目标——宁可少算，不可混算）。
  项目在成交时继承客户的业务线，订单继承客户；无客户的现成产品 / 咨询 / 工作台 / 会员订单默认 `online`。
- **收入口径 = 实收**：订单按 `paid_at` 计、退款按 `refunded_at` 冲减；里程碑回款按 **action_log** 里每次
  `record_payment` / `reverse_payment` 计——里程碑的 `received` 是累计值，按月拆分只能读日志，这也让收入天然可审计。
- **目标写在 profile（不建对象）**：`goals.monthly_revenue / revenue_lines / monthly_leads / lead_lines / weekly_posts /
  daily_live_talk / format_mix`。驾驶舱只显示"实际 vs 目标 vs 时间进度"，落后时间进度 10 个百分点标 ⚠。
  **不预设转化率**：数据满 30 天后再由周报给出实测值（ADR-0011：先跑起来）。
- **新增**：订单 `order`（小额高频成交，不走方案 → 项目）+ `record_order`（direct）/ `refund_order`（闸门）；
  线索跟进 `quick_lead` / `log_contact` / `mark_lead_lost`（direct）；顾问咨询结论 `record_consultation`（direct，
  **软提醒**：没有咨询结论照样能出方案，只在方案结果与日报里提醒）；发布记录加 `format` 内容形态（8 种，数字人预留、配比 0）。
- **每日简报 / 每周复盘**：`autobiz brief [--weekly] [--send]`，只读汇总本体函数；推送用飞书自定义机器人 Webhook + 签名，
  建议发到只有自己的群；本机定时任务触发（`scripts/brief.sh` + `scripts/schedule/`）。

## Consequences

- 全部是 jsonb 属性与新类型，**无数据库迁移**；升级后跑 `uv run autobiz init` 重编译视图即可。
- 同时修复一个整改期回归：视图编译曾改成 `CREATE OR REPLACE VIEW`，给已有类型加属性会直接报错
  （违背 ADR-0009"本体演进不需要迁移"）；恢复为先 DROP 再 CREATE，并加回归测试。
- 历史数据（S1 之前的客户 / 项目）没有业务线，会以"未标注"出现在收入汇总与日报提醒里，需要手动补标一次。
- 简报依赖电脑开机；后续若迁云服务器，定时任务随之迁移，代码不变。
