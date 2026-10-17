# 15 分钟上手（QUICKSTART）

> 用虚构的"社区烘焙店"示例数据走通全链路：看地图 → 丢需求 → 分析 → 方案 → 成交 → 收款。
> 命令以 docker 模式为例（`./bin/autobiz`）；本机 Python 模式换成 `uv run autobiz`。

## 0. 确认就绪（1 分钟）

```bash
./bin/autobiz doctor          # 全 ✓ 再继续
./bin/autobiz q overview      # 能看到示例行业地图的对象数量
```

## 1. 看懂你的行业地图（2 分钟）

```bash
./bin/autobiz q industry_map '{"industry":"社区烘焙"}'
```

痛点 → 有没有案例 / 方法 / 产品可解，以及空白在哪。示例数据里有两条虚构痛点
（会员复购难 / 每日报废损耗）。换成你自己的行业：见 docs/CUSTOMIZE.md。

## 2. 丢一个真实需求（2 分钟）

客户（或你自己）说了句模糊的话，原样丢进去——不要改写、不要总结：

```bash
./bin/autobiz req "示例客户" "我是开烘焙店的，客人买完就走，想让他们常回来，预算不多" wechat
```

## 3. 让 AI 分析需求（3 分钟）

```bash
./bin/autobiz act analyze_requirement '{"target":"<客户id>"}'    # 上一条命令的返回里有 id
```

看输出里的**待澄清问题**（下次沟通要问什么）和**适配度信号**（正向 / 风险）。
不满意就 `update_object` 改，满意就确认——注意这是**人工闸门**：

```bash
./bin/autobiz act confirm_analysis '{"target":"<分析id>"}'       # 预览 → 返回审批码
./bin/autobiz approve <审批码>                                    # 你本人在终端批准
./bin/autobiz act confirm_analysis '{"target":"<分析id>","approval":"<审批码>"}' --confirm
```

## 4. 出方案（4 分钟）

```bash
./bin/autobiz q search '{"query":"烘焙 会员 复购","types":["service_product","case","method"]}'
./bin/autobiz act draft_proposal '{"target":"<分析id>","products":["示例产品：门店私域起步包（虚构）"]}'
```

方案要点：**只能引用已确认的知识、金额由系统按产品价格算**（模型编不了数）。
结果里若有「相关但未确认的知识」：认可就 `verify_object` 确认后 `revise_proposal` 引用。

## 5. 成交与收款（3 分钟）

```bash
./bin/autobiz act accept_proposal '{"target":"<方案id>","approval":"<审批码>"}' --confirm   # 闸门：先预览拿码
# 方案各阶段自动变成带应收金额的里程碑
./bin/autobiz act record_payment '{"target":"<里程碑id>","amount":500}'                    # 先预览再 --confirm
./bin/autobiz q receivables                                                                 # 该催谁的款
```

## 6. 接上你的 AI Agent（可选）

见 docs/AGENTS-SETUP.md——之后你说"看看总览""分析一下这个客户的需求"，Agent 替你跑上面这些命令。

---

**记住三条规矩**（系统会强制，这里只是提前说）：

1. 任何写入先预览、用户同意后用**完全相同的参数**加 `--confirm`；
2. 人工闸门要审批码：预览 → 你在终端 `approve` → 带 `approval` 确认；
3. `已确认: false` 的知识不能进对外内容，先 `verify_object`。
