# 参与贡献（CONTRIBUTING）

欢迎 issue 和 PR，尤其是**行业包**（见下）。

## 提交代码

1. Fork / 克隆后新建分支；`git config core.hooksPath .githooks` 启用提交前检查
   （本体可编译 + 全量测试通过，任何人任何模型一视同仁）。
2. 改本体 YAML 前先读 `ontology/shared.yaml` 头部的格式约定；改引擎行为前先读
   `docs/adr/` 相关决策。
3. 涉及写路径 / 闸门 / 金额的改动必须带 `tests/test_remediation.py` 风格的回归用例。
4. 提交即表示你同意该贡献以 Apache-2.0 授权本项目（与 LICENSE 一致，无需签署额外文件）。

## 行业包（最欢迎的贡献）

用工作区覆盖层把你所在行业的字段 / 痛点 / 枚举沉淀成一个 YAML：

```yaml
# $AUTOBIZ_HOME/ontology/my-industry.yaml
enums:
  platform: { extend: true, kuaishou: 快手 }     # 只允许追加，写 extend: true
object_types:
  organization:
    extend: true
    properties: { store_count: int, city: text }
```

跑 `uv run autobiz check` 通过后，把这个文件提 PR 到 `ontology/packs/`（或先开 issue
附上文件讨论）。被合入的行业包会在 README 致谢。

## 不接受的贡献

- 含真实客户信息 / 个人隐私的种子数据或截图。
- 绕过人工闸门（让 Agent 能自行确认闸门类行动）的改动——这是本系统的安全底线。
