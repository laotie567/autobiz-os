"""命令行：autobiz <命令>

  init                初始化数据库（迁移 + 按本体编译视图）
  migrate             应用数据库迁移（升级后运行）
  seed                导入种子数据（ontology/seed/*.yaml）
  import-feishu FILE  导入旧飞书素材库导出的 ndjson（record_id / 标题 / 原文内容 …）
  import-obsidian     导入 Obsidian 剪藏目录
  extract [N]         对未处理的来源文档执行 AI 抽取（默认 10 篇）
  dedupe              刷新疑似重复对象对（写入 dup_candidates，overview 读它）
  setup-pin           设置闸门审批 PIN（设置后所有角色确认闸门都要先 approve）
  approve <审批码>    老板本人在终端审批闸门行动（Agent 不得代批）
  doctor              环境体检（连接 / 迁移 / 本体 / 向量 / 备份 / PIN / 磁盘）
  onto-check-data     数据与本体一致性检查（只读：未知属性 / 非法枚举 / 孤儿关系）
  status              打印系统总览
  brief [--weekly] [--send]  每日简报 / 每周复盘（--send 推送到飞书群机器人，见 .env FEISHU_BOT_*）
  draft [N] [--revise]  批量起草已排产的内容件（默认 10 条，4 路并发；--revise 批量改"待修改"的稿）
  eval                跑抽取回归评测（换模型 / 改提示词前必跑）

给 AI Agent（豆包工作 / Codex / Claude Code）用的命令，输出一律为 JSON：
  q <函数> ['<JSON参数>']           只读查询，如 q industry_map '{"industry":"实体门店"}'
  act <行动> '<JSON参数>' [--confirm] 写操作；不带 --confirm 只预览不写入
  note "<文字>"                     随手记（直接写入）
  req "<客户名或线索id>" "<需求原文>" [渠道]   丢需求（直接写入）
  actions [域]                      列出可执行行动及参数
  describe <类型>                   查看对象类型的属性与关系
"""
from __future__ import annotations

import glob
import json
import os
import sys

import logging

import yaml
from pgvector.psycopg import register_vector

from autobiz import actions, db, functions, ontology


def _obsidian_dir() -> str:
    from autobiz.profile import profile
    d = profile().get("paths", {}).get("clippings_dir") or ""
    if not d:
        raise SystemExit("未配置剪藏目录：在 $AUTOBIZ_HOME/config/profile.yaml 的 paths.clippings_dir 填写后再导入")
    return os.path.expanduser(d)


def _conn(onto):
    c = db.connect()
    register_vector(c)
    return c


def _run(c, onto, name, params, role="system", actor="cli", strict=False):
    r = actions.run(c, onto, name, params, actor=actor, role=role)
    if strict and not r["ok"]:
        raise SystemExit(f"✗ {name} 失败：{r['error']}")
    return r


def cmd_init(onto):
    c = db.connect()
    db.init(c, onto)
    print(f"✓ 数据库就绪；已编译 {len(onto.object_types)} 张对象视图（v_<类型>）")


def cmd_seed(onto, example_only=False):
    """A-1/L-3：默认导入真实种子（内核 seed/*.yaml，但不含 example-seed.yaml；再加工作区 seed/）；
    --example 只导虚构示例种子（粉丝 5 分钟看到效果用）。"""
    from autobiz.paths import home
    c = _conn(onto)
    n = 0
    paths = []
    for path in sorted(glob.glob(os.path.join(ontology.ROOT, "ontology", "seed", "*.yaml"))):
        is_example = os.path.basename(path) == "example-seed.yaml"
        if is_example != example_only:
            continue
        paths.append(path)
    if not example_only:
        paths += sorted(glob.glob(os.path.join(home(), "seed", "*.yaml")))
    for path in paths:
        doc = yaml.safe_load(open(path, encoding="utf-8"))
        key = {i["key"]: i["name"] for i in doc.get("industries", []) if "key" in i}   # 种子里用 key 引用行业
        for ind in doc.get("industries", []):
            if not _exists(c, "industry", ind["name"]):
                _run(c, onto, "create_object", {"type": "industry", "props": {k: ind.get(k) for k in ("name", "stance", "notes")}},
                     role="owner", actor="seed", strict=True)
                n += 1
        for p in doc.get("pain_points", []):
            if _exists(c, "pain_point", p["title"]):
                continue
            ind = _id(c, "industry", key.get(p["industry"], p["industry"]))
            _run(c, onto, "create_object", {"type": "pain_point",
                 "props": {k: p.get(k) for k in ("title", "description", "willingness_to_pay")},
                 "links": [{"link_type": "pain_in", "to": ind}]}, role="owner", actor="seed", strict=True)
            n += 1
        for cs in doc.get("cases", []):
            if _exists(c, "case", cs["title"]):
                continue
            links = [{"link_type": "case_in", "to": _id(c, "industry", key.get(i, i))} for i in cs.get("industries", [])]
            _run(c, onto, "create_object", {"type": "case",
                 "props": {k: cs.get(k) for k in ("title", "case_origin", "subject", "problem", "solution", "outcome")},
                 "links": links}, role="owner", actor="seed", strict=True)
            n += 1
        for sp in doc.get("service_products", []):
            if _exists(c, "service_product", sp["name"]):
                continue
            sp = dict(sp)
            links = [{"link_type": "solves", "to": _id(c, "pain_point", t)} for t in sp.pop("solves", [])]
            # solves 的方向是 产品 → 痛点，新对象在 from 端
            _run(c, onto, "create_object", {"type": "service_product", "props": sp, "links": links}, role="owner", actor="seed", strict=True)
            n += 1
    print(f"✓ 种子数据导入 {n} 个对象")


def _exists(c, type_, name):
    from autobiz import store
    return store.find_by_name(c, type_, name) is not None


def _id(c, type_, name):
    from autobiz import store
    o = store.find_by_name(c, type_, name)
    if not o:
        raise SystemExit(f"种子数据引用了不存在的{type_}：{name}")
    return o["id"]


def cmd_import_feishu(onto, path):
    c = _conn(onto)
    new = dup = 0
    for line in open(path, encoding="utf-8"):
        r = json.loads(line)
        content = r.get("原文内容") or r.get("正文前1500字") or ""
        if not content.strip():
            continue
        if _exists(c, "source_doc", (r.get("标题") or "").strip()):   # 飞书素材多由 Obsidian 同步而来，按标题跳过
            dup += 1
            continue
        res = _run(c, onto, "ingest_source", {"title": (r.get("标题") or content[:30]).strip(), "source_channel": "x_clip",
                                              "content": content, "source_url": r.get("来源链接") or None,
                                              "author": r.get("原作者") or None})
        if res["ok"] and res["result"]["duplicate"]:
            dup += 1
        elif res["ok"]:
            new += 1
        else:
            print("  ✗", res["error"])
    print(f"✓ 飞书素材导入：新增 {new}，重复 {dup}")


def cmd_import_obsidian(onto):
    sys.path.insert(0, os.path.join(ontology.ROOT, "adapters"))
    from ingest_obsidian import parse_frontmatter
    obsidian_dir = _obsidian_dir()
    c = _conn(onto)
    new = dup = 0
    for path in sorted(glob.glob(os.path.join(obsidian_dir, "**", "*.md"), recursive=True)):
        meta, body = parse_frontmatter(open(path, encoding="utf-8").read())
        if not body.strip():
            continue
        author = meta.get("author")
        res = _run(c, onto, "ingest_source", {
            "title": meta.get("title") or os.path.basename(path)[:-3], "source_channel": "x_clip", "content": body,
            "source_url": meta.get("source") or None,
            "author": ", ".join(author) if isinstance(author, list) else author,
            "captured_at": meta.get("created") or None})
        if res["ok"] and res["result"]["duplicate"]:
            dup += 1
        elif res["ok"]:
            new += 1
        else:
            print("  ✗", os.path.basename(path), res["error"])
    print(f"✓ Obsidian 剪藏导入：新增 {new}，重复 {dup}")


def cmd_extract(onto, n, workers=8):
    """模型调用并行（慢的部分），写库逐篇经 Action 引擎串行（避免并发重复建对象）。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from autobiz import extract, store
    c = _conn(onto)
    rows = c.execute("SELECT id FROM objects WHERE type='source_doc' AND props->>'status' IN ('unprocessed','failed') "
                     "ORDER BY created_at LIMIT %s", (n,)).fetchall()
    docs = [store.get(c, r["id"]) for r in rows]
    c.commit()

    def work(doc):
        wc = db.connect()
        try:
            return extract.analyze(wc, doc)
        finally:
            wc.close()

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(work, d): d for d in docs}
        for f in as_completed(futs):
            d, done = futs[f], done + 1
            head = f"[{done}/{len(docs)}] {d['title'][:30]}"
            try:
                extract.precomputed[d["id"]] = f.result()
                res = _run(c, onto, "extract_knowledge", {"target": d["id"]})
            except Exception as e:
                print(f"{head} ✗ {e}", flush=True)
                # status 是托管字段，走抽取流程落失败状态（见 extract.extract 的 __failed__ 分支）
                extract.precomputed[d["id"]] = {"__failed__": str(e)[:300]}
                mark = _run(c, onto, "extract_knowledge", {"target": d["id"]})
                if not mark["ok"]:
                    print(f"↳ 落失败状态也被拒绝：{mark['error']}", flush=True)
                if "Insufficient Balance" in str(e) or "402" in str(e):     # 计费错误：立即停止，不再空打请求
                    for other in futs:
                        other.cancel()
                    print("✗ 模型账户余额不足，已停止。充值后重跑 `autobiz extract` 会从失败处继续。", flush=True)
                    break
                continue
            x = res.get("result") or {}
            if not res["ok"]:
                print(f"{head} ✗ {res['error']}", flush=True)
            elif x.get("status") == "irrelevant":
                print(f"{head} · 相关度 {x['relevance']:.0f}，跳过", flush=True)
            else:
                print(f"{head} ✓ 相关度 {x['relevance']:.0f}，新建 {x['created']}，合并 {x['merged']}，"
                      f"无引文丢弃 {len(x['dropped_no_quote'])}", flush=True)
    cmd_dedupe(onto)          # I-1：抽取后刷新疑似重复对（overview 读 dup_candidates）


_VIDEO_SECTION = '**视频生产**（做片头 / 宣传片 / 纪录片）：**四块，两道人工闸门**\n1. 立项：`act start_video_production \'{"title":"片名","kind":"内容获客","dir":"<绝对路径>"}\'`。建项目文件夹、把规则手册复制成项目根 `AGENTS.md`（**不是 CLAUDE.md——生产期跑国内模型，AGENTS.md 是跨厂商通用名**），回四块清单。\n2. **① 创作块（人主导）**：编剧写 `SCRIPT.md`（写**情绪的画面成因**，不写情绪）→ 导演定 `BRIEF.md`（一句话命题 + 三刻度盘 + 反模式）→ 编排写 `SHOTLIST.md`（先算镜数、像勘景师描述、每镜填五要素）。另有 `INTAKE.md`、`style_guide.md`。\n3. **闸门一**：用户确认后 `act lock_video_creative`。\n4. **② 事实块**：`FACTS.md` 五列——`陈述 | 上屏 | 信源 | 引文 | 状态`。**引文必须是原文原句**；给不出原句就不能上屏；核不出来回创作块改稿，不许把数字改到"看起来对"。\n5. **闸门二**：用户核验后 `act lock_video_facts`。缺信源/缺引文/状态非"已确认"→ 代码直接拒绝。\n6. **③ 组件块**：`COMPONENTS.md` 从本体注册表选积木（已有 18 块：地图动线、数据卡、情绪重音…）；**缺口显式写明**（如"图片类：缺，需新建 `ink-illustration`"），不许悄悄不做。\n7. **④ 技术块（机器）**：`beats.json` → 渲染 → `check/contact-sheet.jpg` 自查 → 成片。\n8. 随时体检：`act check_video_production \'{"target":"<视频项目id>"}\'`（代码判定，不靠模型自述）；建于四块改造之前的项目会标 `legacy_without_lock`。\n\n方法论：`docs/video-production/` 下 `creative-block.md`（编剧/导演/编排）、`fact-block.md`（信源/引文/确认）、`component-block.md`（动效积木）。\n\n'


_OPC_SECTION = ("**OPC 基地招商（合伙业务线 partner，不计入线上收入 / 线索目标）**\n"
    "1. 登记空间（一次性）：`act add_spaces '{\"prefix\":\"A-W\",\"count\":300,\"kind\":\"desk\",\"floor\":\"一期\",\"area\":4}'`（工位 desk / 独立办公室 office / 会议室 meeting）。\n"
    "2. 招商线索：`quick_lead` 带 `\"business_line\":\"partner\"`；办了活动 `act log_event '{\"title\":\"开园路演\",\"kind\":\"roadshow\",\"attendees\":60,\"leads\":[\"线索id\"]}'`。\n"
    "3. 有意向先 `reserve_space`（可取消 `release_space`）；签约 `sign_residency`（**闸门**，target 是空间，org 填企业 / 线索 id 或名称，"
    "可带 `extra_spaces` 一次签多个、`rent_policy` 如\"首年工位租金全免\"）；退租 `end_residency`。\n"
    "4. 看情况：`q occupancy`（出租率、到期入驻、过期预留）、`q opc_pipeline`（活动 → 线索 → 预留 → 签约）。代运营服务费等收入用 `record_order` 记 `business_line: partner`。\n\n")


def cmd_agents_md(onto, path=None):
    """K-2：从模板 + 本体 + profile 生成 AGENTS.md（数字与清单不再手写）。"""
    from string import Template
    from autobiz.profile import owner_name
    tpl = open(os.path.join(ontology.ROOT, "templates", "agents-md.template"), encoding="utf-8").read()
    types_line = "、".join(f"{n} {t.label}" for n, t in onto.object_types.items())
    gates = [n for n, a in onto.action_types.items() if a.gate]
    flow = " → ".join(list(onto.domains.values()) if onto.domains else ["知识", "内容获客", "客户", "售前", "交付"])
    # 视频板块为可选组件：行动不在本体里（发布物未随包）时整段省略
    video_section = _VIDEO_SECTION if "start_video_production" in onto.action_types else ""
    opc_section = _OPC_SECTION if "sign_residency" in onto.action_types else ""
    body = Template(tpl).safe_substitute({
        "owner_name": owner_name(),
        "types_line": types_line, "types_count": len(onto.object_types),
        "gate_names": "、".join(gates), "gates_count": len(gates),
        "domains_flow": flow, "domains_count": len(onto.domains) or 5,
        "video_section": video_section, "opc_section": opc_section,
    })
    if path:
        open(path, "w", encoding="utf-8").write(body)
        print(f"✓ 已生成 {path}")
    else:
        print(body)


def cmd_dedupe(onto):
    """I-1：每个对象只查最近邻一个，结果写 dup_candidates；overview 不再做平方级自连接。"""
    c = _conn(onto)
    try:
        c.execute("SET hnsw.iterative_scan = relaxed_order")
        c.execute("""
            INSERT INTO dup_candidates (a, b, dist)
            SELECT a.id, nn.id, nn.d FROM objects a
            CROSS JOIN LATERAL (
              SELECT b.id, a.embedding <=> b.embedding AS d FROM objects b
              WHERE b.type = a.type AND b.id <> a.id AND b.embedding IS NOT NULL
              ORDER BY a.embedding <=> b.embedding LIMIT 1) nn
            WHERE a.type IN ('concept','tool','pain_point','method','case') AND a.embedding IS NOT NULL
              AND nn.d < 0.08 AND a.id < nn.id
            ON CONFLICT DO NOTHING""")
        c.execute("DELETE FROM dup_candidates WHERE found_at < now() - interval '30 days'")
        n = c.execute("SELECT count(*) n FROM dup_candidates").fetchone()["n"]
        c.commit()
        print(f"✓ 疑似重复对象对：{n}（autobiz q overview 查看）")
    finally:
        c.close()


def cmd_draft(onto, n=10, revise=False, workers=4):
    """S2：批量起草 / 改稿。每条各用一个连接经 Action 引擎执行（模型调用在事务外，互不阻塞）。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    actor, role = _actor()
    status, action = ("changes_requested", "revise_piece") if revise else ("planned", "draft_piece")
    c = _conn(onto)
    rows = c.execute("""SELECT id, title FROM objects WHERE type='content_piece' AND props->>'status'=%s
                        ORDER BY props->>'planned_date' NULLS LAST, created_at LIMIT %s""", (status, n)).fetchall()
    c.close()
    if not rows:
        print(f"没有{'待修改' if revise else '已排产待起草'}的内容件")
        return

    def one(r):
        wc = _conn(onto)
        try:
            return r, actions.run(wc, onto, action, {"target": r["id"]}, actor=actor, role=role)
        finally:
            wc.close()

    ok = fail = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i, f in enumerate(as_completed([pool.submit(one, r) for r in rows]), 1):
            try:
                r, res = f.result()
            except Exception as e:
                print(f"[{i}/{len(rows)}] ✗ {e}", flush=True)
                fail += 1
                continue
            x = res.get("result") or {}
            if res.get("ok") and x.get("状态") == "待审":
                ok += 1
                print(f"[{i}/{len(rows)}] ✓ {r['title'][:28]} → 待审", flush=True)
            else:
                fail += 1
                print(f"[{i}/{len(rows)}] ✗ {r['title'][:28]}：{res.get('error') or x.get('体检问题')}", flush=True)
    print(f"完成：进审稿队列 {ok} 条，未通过 {fail} 条（q review_queue 查看）")


def _json_out(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=1, default=str))


def _actor():
    """B-5：默认身份是 agent（AI 助手）。老板本人在交互终端操作时设 AUTOBIZ_ROLE=owner；
    闸门行动任何角色都不能由 Agent 自行确认（见 approve）。
    角色提权只在交互终端有效（approve.resolve_role）：Agent 的非交互 shell 一律 agent。"""
    from autobiz import approve
    from autobiz.profile import owner_name
    return os.environ.get("AUTOBIZ_ACTOR") or owner_name(), approve.resolve_role(os.environ.get("AUTOBIZ_ROLE", "agent"))


def cmd_q(onto, name, raw):
    from autobiz.store import ActionError
    if name not in onto.functions:
        return _json_out({"错误": f"未知查询 {name}", "可用": {k: f.label for k, f in onto.functions.items()}})
    c = _conn(onto)
    try:
        _json_out(getattr(functions, name)(c, onto, **json.loads(raw or "{}")))
    except (ActionError, TypeError) as e:
        _json_out({"错误": str(e)})


def cmd_act(onto, name, raw, confirm):
    actor, role = _actor()
    r = actions.run(_conn(onto), onto, name, json.loads(raw or "{}"), actor=actor, role=role, confirm=confirm)
    if r.get("ok") and r.get("status") == "preview":
        r["下一步"] = "这是预览，尚未写入。向用户说明变化，用户同意后加 --confirm 用相同参数再执行。"
    _json_out(r)


def main():
    logging.getLogger("httpx").setLevel(logging.WARNING)          # 输出保持干净，方便 Agent 解析
    import warnings
    warnings.filterwarnings("ignore", module="huggingface_hub")
    args = sys.argv[1:] or ["help"]
    cmd = args[0]
    if cmd in ("help", "-h", "--help"):
        print(__doc__)
        return
    onto = ontology.load()
    if cmd == "check":
        print(f"✓ 本体 v{onto.version}：{len(onto.object_types)} 类对象，{len(onto.link_types)} 种关系，"
              f"{len(onto.action_types)} 个行动，{len(onto.functions)} 个函数")
    elif cmd == "init":
        cmd_init(onto)
    elif cmd == "migrate":
        from autobiz.migrate import migrate
        applied = migrate(_conn(onto))
        print(f"✓ 迁移已是最新（本次应用 {len(applied)} 个）")
    elif cmd == "seed":
        cmd_seed(onto, example_only="--example" in args)
    elif cmd == "dedupe":
        cmd_dedupe(onto)
    elif cmd == "agents-md":
        cmd_agents_md(onto, args[1] if len(args) > 1 else None)
    elif cmd == "doctor":
        from autobiz import doctor
        for ok, name, note in doctor.run():
            print(f"{'✓' if ok else '✗'} {name}" + (f"：{note}" if note else ""))
    elif cmd == "onto-check-data":
        from autobiz import doctor
        issues = doctor.onto_check_data(onto)
        if issues:
            print(f"发现 {len(issues)} 个数据与本体不一致：")
            for i in issues[:30]:
                print("  -", i)
        else:
            print("✓ 全部对象与当前本体一致")
    elif cmd == "setup-pin":
        from autobiz.approve import setup_pin
        setup_pin()
    elif cmd == "approve":
        if len(args) < 2:
            raise SystemExit("用法：autobiz approve <审批码>")
        from autobiz.approve import approve as _approve
        _approve(args[1])
    elif cmd == "import-feishu":
        cmd_import_feishu(onto, args[1])
    elif cmd == "import-obsidian":
        cmd_import_obsidian(onto)
    elif cmd == "extract":
        cmd_extract(onto, int(args[1]) if len(args) > 1 else 10)
    elif cmd == "q":
        cmd_q(onto, args[1], args[2] if len(args) > 2 else None)
    elif cmd == "act":
        cmd_act(onto, args[1], args[2] if len(args) > 2 and args[2] != "--confirm" else None, "--confirm" in args)
    elif cmd == "note":
        actor, role = _actor()
        _json_out(actions.run(_conn(onto), onto, "capture_note", {"text": " ".join(args[1:])}, actor=actor, role=role))
    elif cmd == "req":
        actor, role = _actor()
        _json_out(actions.run(_conn(onto), onto, "capture_requirement",
                              {"customer": args[1], "text": args[2], "channel": args[3] if len(args) > 3 else None},
                              actor=actor, role=role))
    elif cmd == "actions":
        from autobiz import mcp_server
        print(mcp_server.list_actions(args[1] if len(args) > 1 else None))
    elif cmd == "describe":
        from autobiz import mcp_server
        print(mcp_server.describe_type(args[1]))
    elif cmd == "eval":
        from autobiz import evaluate
        r = evaluate.run()
        for x in r["results"]:
            mark = "✓" if x["pass"] else ("~" if x["passes"] else "✗")
            print(f"{mark} [{x['passes']}/{x['runs']}] {x['doc'][:34]}", *(["\n    - " + f for f in x["fails"]] or [""]))
        print(f"\n稳定通过 {r['passed']}/{r['total']}（每篇 {r['repeat']} 次，~ 表示时过时不过）；"
              f"引文可在原文中找到 {r['quote_grounded']}")
        print("上线门槛：" + ("✓ 通过（无准确性问题）" if r["gate"] else "✗ 未通过（存在抽错，不得上线）"))
    elif cmd == "draft":
        cmd_draft(onto, int(args[1]) if len(args) > 1 and args[1].isdigit() else 10, revise="--revise" in args)
    elif cmd == "brief":
        from autobiz import brief
        c = _conn(onto)
        text = brief.weekly(c, onto) if "--weekly" in args else brief.daily(c, onto)
        print(text)
        if "--send" in args:
            brief.send_feishu(text)
            print("✓ 已推送到飞书")
    elif cmd == "status":
        c = _conn(onto)
        print(json.dumps(functions.overview(c, onto), ensure_ascii=False, indent=2))
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
