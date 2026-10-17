<div align="center">

# AutoBiz OS

**本体驱动的一人公司业务操作系统**

**中文** | [English](README.en.md) | [日本語](README.ja.md)

知识 → 内容获客 → 客户 → 售前 → 交付 → 沉淀回知识，五个域连成一张网。

</div>

---

## 这是什么

一个人 + AI Agent 跑一门咨询/服务生意的完整后台。不是 Notion 模板，是一个**带业务规则引擎的数据库系统**：

- **本体即唯一权威**：你的客户、痛点、案例、产品、收款都是本体里的对象，`ontology/*.yaml` 一处定义，视图 / 行动 / AI 工具全部由它编译出来
- **Action 引擎是唯一写入口**：每次写入先预览、留行动日志；**人工闸门由引擎强制**——AI 自己确认不了成交和结项，审批码必须你本人在终端敲
- **模型只写、代码负责找和算**：AI 生成的方案只能引用系统给出的已确认知识、金额全部由代码计算——**从机制上防编造**
- **引文校验**：AI 抽取的知识必须给得出原文引文，虚构内容确定性丢弃

```
$ autobiz q overview
$ autobiz req "客户名" "客人买完就走，想让他们常回来" wechat
$ autobiz act analyze_requirement '{"target":"org_xxx"}'
$ autobiz approve K7Q2PX        ← 人工闸门：你本人在终端批准
```

## 15 分钟上手

```bash
git clone https://github.com/laotie567/autobiz-os.git && cd autobiz-os
bash scripts/install.sh         # 只需要装了 Docker
```

安装脚本会导入一套虚构的"社区烘焙店"示例行业地图，走一遍
[快速上手](docs/QUICKSTART.md)：看地图 → 丢需求 → AI 分析 → 出方案 → 成交 → 收款。

| 想了解 | 去哪 |
|---|---|
| 安装（三种路径） | [docs/INSTALL.md](docs/INSTALL.md) |
| 15 分钟全链路 | [docs/QUICKSTART.md](docs/QUICKSTART.md) |
| 改成你的行业 | [docs/CUSTOMIZE.md](docs/CUSTOMIZE.md) |
| 接入豆包工作 / Claude Code / Cursor | [docs/AGENTS-SETUP.md](docs/AGENTS-SETUP.md) |
| 升级 / 排障 / 备份 | [docs/UPGRADE.md](docs/UPGRADE.md) · [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) · [docs/backup.md](docs/backup.md) |
| 设计决策（ADR） | [docs/adr/](docs/adr/) |

## 设计理念（为什么长这样）

- **人工闸门引擎强制**：AI 助手默认最小权限，预览纪律、审批码、生命周期字段保护——信任但要验证
- **内核 / 工作区分离**：你的个性化（档案、行业字段、提示词微调）全在工作区，升级内核零冲突
- **可扩展本体**：`extend: true` 覆盖层加你的行业字段，不用改一行内核代码
- 隐私与数据流向见 [PRIVACY.md](PRIVACY.md)，安全模型见 [SECURITY.md](SECURITY.md)

## 社区与支持

- **问题与功能建议**：开 issue（附 `autobiz doctor` 输出，解决率翻倍）
- **行业包共创**：把你的行业覆盖层 PR 进 `ontology/packs/`，README 致谢
- **手把手安装 / 行业包优先享 / 定期拆解**：知识星球入口即将公布 <!-- TODO：有链接后替换本行 -->
- **深度陪跑 / 定制**：联系渠道即将公布 <!-- TODO：有链接后替换本行 -->

## License

[Apache-2.0](LICENSE) © 2026 AutoBiz OS 作者。**"AutoBiz OS" 及其中文名称为项目作者保留商标**，
二次分发请使用你自己的产品名（详见 [NOTICE](NOTICE)）。方案等 AI 生成内容请人工审核后对外使用。
