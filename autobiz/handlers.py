"""复杂 Action 的实现（本体 action_types 中 handler 指向这里的同名函数）。

签名统一为 (ctx, target, params)；只能通过 store 的写函数改数据，引擎负责事务、日志与预览回滚。
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

from autobiz import store
from autobiz.store import ActionError, Ctx

MASTERY_ORDER = ["heard", "studied", "practiced", "delivered"]


# ── 通用行动 ─────────────────────────────────────────
def _guard_managed(ctx: Ctx, type_: str, props: dict, *, creating: bool):
    """B-1：生命周期字段只能由流程行动修改。流程行动内部直接调 store，不经过这里。"""
    t = ctx.onto.object_types[type_]
    hit = set(props) & t.managed
    if creating:                       # 新建时只允许写与声明初始值相同的字段
        hit = {k for k in hit if props[k] != t.initial.get(k)}
    if hit:
        raise ActionError(f"{t.label}的 {sorted(hit)} 由流程行动维护，不能用通用的新建/修改直接写；"
                          f"请用对应行动（autobiz actions 查看）")


def create_object(ctx: Ctx, _t, p):
    _guard_managed(ctx, p["type"], p.get("props") or {}, creating=True)
    origin = "seed" if ctx.actor == "seed" else "human"    # B-2：种子导入经内部通道标记 origin=seed
    oid = store.create(ctx, p["type"], p["props"], origin=origin)
    for lk in p.get("links") or []:
        if "to" in lk:
            store.link(ctx, lk["link_type"], oid, lk["to"])
        elif "from" in lk:
            store.link(ctx, lk["link_type"], lk["from"], oid)
        else:
            raise ActionError("links 每项需要 link_type 以及 to 或 from")
    return {"id": oid}


def update_object(ctx: Ctx, _t, p):
    o = store.require(ctx.conn, p["target"])
    _guard_managed(ctx, o["type"], p["props"], creating=False)
    store.update(ctx, p["target"], p["props"])
    return {"id": p["target"]}


def link_objects(ctx: Ctx, _t, p):
    store.link(ctx, p["link_type"], p["from"], p["to"])


def unlink_objects(ctx: Ctx, _t, p):
    store.unlink(ctx, p["link_type"], p["from"], p["to"])


def verify_object(ctx: Ctx, _t, p):
    done = []
    for oid in sorted(p["targets"]):                 # C-1：按 id 排序加锁，避免交叉死锁
        o = store.require(ctx.conn, oid, lock=True)
        if not o["verified"]:
            store.set_system(ctx, o["id"], verified=True, verified_at=store.now_iso())
        done.append(o["title"])
    return {"verified": done}


def merge_objects(ctx: Ctx, _t, p):
    locked = {i: store.require(ctx.conn, i, lock=True) for i in sorted({p["keep"], p["absorb"]})}  # C-1：排序加锁
    keep, absorb = locked[p["keep"]], locked[p["absorb"]]
    if keep["type"] != absorb["type"]:
        raise ActionError(f"只能合并同类对象：{keep['type']} ≠ {absorb['type']}")
    if keep["id"] == absorb["id"]:
        raise ActionError("不能与自身合并")
    rows = ctx.conn.execute("SELECT link_type, from_id, to_id FROM links WHERE from_id=%s OR to_id=%s",
                            (absorb["id"], absorb["id"])).fetchall()
    moved = 0
    for r in rows:
        f = keep["id"] if r["from_id"] == absorb["id"] else r["from_id"]
        t = keep["id"] if r["to_id"] == absorb["id"] else r["to_id"]
        # 经 store.link 迁移：关系类型 / 已确认校验照常生效，迁移进 changes 审计
        if f != t and store.link(ctx, r["link_type"], f, t):
            moved += 1
    aliases = sorted({*keep["aliases"], absorb["title"], *absorb["aliases"]} - {keep["title"]})
    store.set_system(ctx, keep["id"], aliases=aliases)
    store.delete(ctx, absorb["id"])
    return {"kept": keep["id"], "aliases": aliases, "moved_links": moved}


# ── 知识域 ───────────────────────────────────────────
def ingest_source(ctx: Ctx, _t, p):
    if p.get("source_url"):
        dup = ctx.conn.execute("SELECT id FROM objects WHERE type='source_doc' AND props->>'source_url'=%s",
                               (p["source_url"],)).fetchone()
    else:
        dup = ctx.conn.execute("SELECT id FROM objects WHERE type='source_doc' AND md5(props->>'content')=%s",
                               (hashlib.md5(p["content"].encode()).hexdigest(),)).fetchone()
    if dup:
        return {"id": dup["id"], "duplicate": True}
    props = {k: p.get(k) for k in ("title", "source_channel", "source_url", "author", "content", "captured_at")}
    props["captured_at"] = props["captured_at"] or store.now_iso()
    props["status"] = "unprocessed"
    return {"id": store.create(ctx, "source_doc", props, origin="human"), "duplicate": False}


def extract_knowledge(ctx: Ctx, target, p):
    from autobiz.extract import extract
    return extract(ctx, target)


KNOWLEDGE_TYPES = ["concept", "tool", "industry", "pain_point", "case", "method", "source_doc", "insight"]


def capture_note(ctx: Ctx, _t, p):
    text = p["text"].strip()
    title = text.splitlines()[0][:40]
    oid = store.create(ctx, "insight", {"title": title, "body": text})
    about = p.get("about") or []
    if not about:  # 自动关联：语义最接近且足够相似的知识对象（最多 3 个）
        vec = ctx.conn.execute("SELECT embedding FROM objects WHERE id=%s", (oid,)).fetchone()["embedding"]
        rows = ctx.conn.execute(
            "SELECT id FROM objects WHERE type = ANY(%s) AND type <> 'insight' AND embedding IS NOT NULL "
            "AND 1 - (embedding <=> %s) >= 0.75 ORDER BY embedding <=> %s LIMIT 3",
            (KNOWLEDGE_TYPES, vec, vec)).fetchall()
        about = [r["id"] for r in rows]
    for a in about:
        store.link(ctx, "insight_about", oid, a)
    return {"id": oid, "linked": about}


def _raise_mastery(ctx: Ctx, oid: str, level: str):
    o = store.require(ctx.conn, oid)
    if o["type"] not in ("tool", "concept"):
        return
    cur = o["props"].get("mastery")
    if cur is None or MASTERY_ORDER.index(cur) < MASTERY_ORDER.index(level):
        store.update(ctx, oid, {"mastery": level})


def log_practice(ctx: Ctx, _t, p):
    props = {k: p.get(k) for k in ("title", "what_i_did", "result", "lessons", "publishable")}
    props["done_at"] = store.now_iso()
    oid = store.create(ctx, "practice_log", props)
    for t in p.get("targets") or []:
        store.link(ctx, "practice_on", oid, t)
        _raise_mastery(ctx, t, "practiced")
    return {"id": oid}


# ── 市场域 ───────────────────────────────────────────
def propose_topic(ctx: Ctx, _t, p):
    oid = store.create(ctx, "topic", {"title": p["title"], "angle": p.get("angle"), "status": "candidate",
                                      "target_platforms": p.get("target_platforms")})
    for s_ in p.get("sources") or []:
        store.link(ctx, "topic_from", oid, s_)
    return {"id": oid}


STAGES = {"24h": "views_24h", "72h": "views_72h", "7d": "views_7d"}


def backfill_metrics(ctx: Ctx, target, p):
    stage = p["stage"].lower().replace("小时", "h").replace("天", "d").replace(" ", "")
    if stage not in STAGES:
        raise ActionError(f"stage 只能是 {list(STAGES)}，收到 {p['stage']}")
    props = {k: p[k] for k in ("likes", "comments", "favorites", "new_followers", "completion_rate", "leads_dm", "leads_contact")
             if p.get(k) is not None}
    if p.get("views") is not None:
        props[STAGES[stage]] = p["views"]
    if not props:
        raise ActionError("没有任何要回填的数据")
    store.update(ctx, target["id"], props)
    return {"id": target["id"], "stage": stage}


# ── 客户域 ───────────────────────────────────────────
def _days_later(n: int | None, default: int = 1) -> str:
    n = default if n is None else n
    return (datetime.now(timezone.utc) + timedelta(days=n)).isoformat(timespec="seconds")


def _new_lead(ctx: Ctx, p: dict, title: str, intent: str | None) -> dict:
    """create_lead / quick_lead 共用：有平台来源的线索默认算线上获客业务（ADR-0015）。"""
    line = p.get("business_line") or ("online" if p.get("platform") else None)
    oid = store.create(ctx, "lead", {"title": title, "platform": p.get("platform"), "intent": intent,
                                     "contact": p.get("contact"), "status": "new", "keyword": p.get("keyword"),
                                     "business_line": line, "next_followup_at": _days_later(p.get("next_in_days"))})
    attributed = None
    if p.get("source_publish"):
        store.link(ctx, "lead_from", oid, p["source_publish"])
        store.update(ctx, oid, {"attribution": "manual"})
    else:                                          # S3：按私信关键词自动归因到近 14 天的发布
        from autobiz.feedback import auto_attribute
        attributed = auto_attribute(ctx, oid, p.get("keyword"), p.get("platform"))
    out = {"id": oid, "业务线": ctx.onto.enum_label("business_line", line) if line else "未标注"}
    if attributed:
        out["来源内容"] = attributed["发布"] + ("（关键词命中多条，取同平台最近一条）" if attributed["候选数"] > 1 else "")
    elif not p.get("source_publish"):
        out["来源内容"] = "未归因（周报会列出待认领）"
    if not line:
        out["提醒"] = "没有平台来源，业务线未标注；请补 business_line（online / offline / partner），否则不计入目标"
    return out


def create_lead(ctx: Ctx, _t, p):
    return _new_lead(ctx, p, p["title"], p.get("intent"))


def quick_lead(ctx: Ctx, _t, p):
    """S1：一句话录入。title = 称呼（+关键词），intent = 原话。"""
    title = p["who"].strip() + (f"【{p['keyword'].strip()}】" if p.get("keyword") else "")
    return _new_lead(ctx, p, title, p.get("text"))


def log_contact(ctx: Ctx, target, p):
    """S1：记一次跟进——追加沟通记录、更新最近联系时间、定下次跟进；新线索自动转"跟进中"。"""
    log = list(target["props"].get("contact_log") or [])
    log.append({"at": store.now_iso(), "by": ctx.actor, "note": p["note"].strip()})
    props = {"contact_log": log[-50:], "last_contact_at": store.now_iso(),
             "next_followup_at": p.get("next_at") or _days_later(p.get("next_in_days"), default=3)}
    if target["type"] == "lead" and target["props"].get("status") == "new":
        props["status"] = "following"
    store.update(ctx, target["id"], props)
    return {"id": target["id"], "下次跟进": props["next_followup_at"][:10], "沟通记录条数": len(log)}


def record_consultation(ctx: Ctx, org, p):
    """S1：顾问咨询 = 筛选闸口（品牌 DNA：层层过渡不跳级）。只记录、软提醒，不拦方案。"""
    store.update(ctx, org["id"], {"consult_verdict": p["verdict"], "consult_reason": p.get("reason"),
                                  "consulted_at": store.now_iso()})
    out = {"客户": org["title"], "结论": ctx.onto.enum_label("consult_verdict", p["verdict"])}
    fee = float(p.get("fee") or 0)
    if fee > 0:
        hours = p.get("hours")
        oid = store.create(ctx, "order", {"title": f"顾问咨询·{org['title']}" + (f"（{hours:g} 小时）" if hours else ""),
                                          "amount": fee, "kind": "consulting", "status": "paid",
                                          "business_line": org["props"].get("business_line"),
                                          "paid_at": p.get("paid_at") or store.now_iso()})
        store.link(ctx, "order_of", oid, org["id"])
        out["咨询费订单"] = oid
    if p["verdict"] == "no_go":
        out["提醒"] = "结论为不接：后续若仍要出方案，系统只做软提醒，不会拦截"
    return out


def _find_or_create(ctx: Ctx, type_: str, name: str, extra: dict | None = None) -> str:
    hit = store.find_by_name(ctx.conn, type_, name)
    if hit:
        return hit["id"]
    title_field = ctx.onto.object_types[type_].title
    return store.create(ctx, type_, {title_field: name, **(extra or {})})


def convert_lead(ctx: Ctx, lead, p):
    hit = store.find_by_name(ctx.conn, "organization", p["org_name"])
    if hit:
        org = hit["id"]
    else:
        near = store.name_candidates(ctx.conn, "organization", p["org_name"])   # C-6：先问，不默默建重复客户
        if near and not p.get("confirm_new"):
            raise ActionError(f"已有名字相近的客户：{'、'.join(x['title'] for x in near)}。"
                              f"若是其中之一请改用它的准确名字；确认是新客户请带 confirm_new: true 重新执行")
        org = _find_or_create(ctx, "organization", p["org_name"], {"org_type": p.get("org_type"),
                                                                   "contacts": lead["props"].get("contact"),
                                                                   "business_line": lead["props"].get("business_line")})
    store.link(ctx, "lead_org", lead["id"], org)
    if p.get("industry"):
        ind = _find_or_create(ctx, "industry", p["industry"], {"stance": "watching"})
        store.link(ctx, "org_in", org, ind)
    store.update(ctx, lead["id"], {"status": "converted"})
    return {"organization": org, "下一步": "用 capture_requirement 把这个客户的需求丢进来"}


# ── 交付域 ───────────────────────────────────────────
def record_payment(ctx: Ctx, ms, p):
    amount = float(p["amount"])
    if amount <= 0:
        raise ActionError("收款金额必须大于 0（C-2）；登记冲正/退款请走 reverse_payment")
    received = float(ms["props"].get("received") or 0) + amount
    store.update(ctx, ms["id"], {"received": received, "received_at": p.get("received_at") or store.now_iso()})
    due = float(ms["props"].get("amount") or 0)
    return {"milestone": ms["title"], "received": received, "outstanding": max(due - received, 0)}


def reverse_payment(ctx: Ctx, ms, p):
    """收款冲正（C-2）：正数登记，从已收中扣减；单独闸门留痕，不许用负数收款。"""
    amount = float(p["amount"])
    if amount <= 0:
        raise ActionError("冲正金额必须大于 0（正数登记，从已收中扣减）")
    received = float(ms["props"].get("received") or 0)
    if amount > received:
        raise ActionError(f"冲正金额 {amount:,.0f} 超过已收 {received:,.0f}，请核对")
    received -= amount
    store.update(ctx, ms["id"], {"received": received})
    return {"milestone": ms["title"], "received": received, "冲正": amount, "事由": p["reason"]}


ONLINE_KINDS = ("product", "consulting", "membership", "workbench")   # 无客户时默认算线上获客（ADR-0015）；定制/其他须明确


def record_order(ctx: Ctx, _t, p):
    """S1：小额高频成交（skill、咨询、工作台、会员）。收款即登记；业务线 = 参数 > 客户 > 按类型推断。"""
    amount = float(p["amount"])
    if amount <= 0:
        raise ActionError("订单金额必须大于 0；退款请走 refund_order（闸门）")
    cust, buyer = None, p.get("buyer")
    if p.get("customer"):
        ref = p["customer"].strip()
        cust = store.get(ctx.conn, ref) or store.find_by_name(ctx.conn, "organization", ref)
        if cust and cust["type"] not in ("organization", "lead"):
            raise ActionError(f"{ref} 不是客户或线索")
        if not cust:
            buyer = buyer or ref                      # 个人买家不强制建客户，只记称呼
    line = p.get("business_line") or (cust["props"].get("business_line") if cust else None) \
        or ("online" if p["kind"] in ONLINE_KINDS else None)
    oid = store.create(ctx, "order", {"title": p["title"], "amount": amount, "kind": p["kind"], "status": "paid",
                                      "business_line": line, "buyer": buyer or (cust["title"] if cust else None),
                                      "channel": p.get("channel"), "paid_at": p.get("paid_at") or store.now_iso()})
    if cust:
        store.link(ctx, "order_of", oid, cust["id"])
    if p.get("product"):
        prod = store.get(ctx.conn, p["product"]) or store.find_by_name(ctx.conn, "service_product", p["product"])
        if not prod or prod["type"] != "service_product":
            raise ActionError(f"服务产品「{p['product']}」不存在；先 q search 查准确名字，或不填 product")
        store.link(ctx, "order_product", oid, prod["id"])
    out = {"id": oid, "金额": amount, "业务线": ctx.onto.enum_label("business_line", line) if line else "未标注"}
    if not line:
        out["提醒"] = "业务线未标注，这笔不会计入收入目标；请补 business_line"
    return out


def complete_milestone(ctx: Ctx, ms, p):
    props = {"done": True}
    if p.get("deliverables"):
        props["deliverables"] = p["deliverables"]
    store.update(ctx, ms["id"], props)
    due, got = float(ms["props"].get("amount") or 0), float(ms["props"].get("received") or 0)
    return {"milestone": ms["title"], "提醒": f"本阶段应收 {due - got:,.0f} 元未到账，记得催款" if due > got else None}


def complete_project(ctx: Ctx, proj, p):
    """结项 → 自动沉淀"自己交付"的案例，连回知识域（飞轮闭环）。"""
    q = ctx.conn.execute
    orgs = q("SELECT o.id, o.title FROM links l JOIN objects o ON o.id=l.to_id WHERE l.link_type='project_of' AND l.from_id=%s",
             (proj["id"],)).fetchall()
    store.update(ctx, proj["id"], {"status": "done", "retrospective": p["retrospective"]})
    case = store.create(ctx, "case", {"title": proj["title"], "case_origin": "own",
                                      "subject": "、".join(o["title"] for o in orgs) or None,
                                      "problem": proj["props"].get("summary"), "solution": p["retrospective"],
                                      "outcome": p["outcome"]}, origin="human", verified=True)
    store.link(ctx, "project_produced", proj["id"], case)
    org_ids = [o["id"] for o in orgs]
    for r in q("SELECT DISTINCT link_type, to_id FROM links WHERE from_id = ANY(%s) AND link_type IN ('org_in','org_has_pain')",
               (org_ids,)).fetchall():
        store.link(ctx, "case_in" if r["link_type"] == "org_in" else "case_solves", case, r["to_id"])
    for r in q("SELECT l.to_id, o.type FROM links l JOIN objects o ON o.id=l.to_id WHERE l.link_type='project_uses' AND l.from_id=%s",
               (proj["id"],)).fetchall():
        if r["type"] in ("tool", "method"):
            store.link(ctx, "case_uses", case, r["to_id"])
        if r["type"] == "tool":
            _raise_mastery(ctx, r["to_id"], "delivered")
    return {"case": case}


# ── 售前域（实现见 presales.py）─────────────────────
from autobiz.presales import (accept_proposal, analyze_requirement, capture_requirement,  # noqa: E402,F401
                              draft_proposal, publish_proposal, record_decision, requirement_to_topic,
                              revise_proposal)

# ── 数据回流（S3，实现见 feedback.py）──────────────
from autobiz.feedback import attribute_lead, backfill_batch  # noqa: E402,F401

# ── 内容快产线（S2，实现见 content.py）─────────────
from autobiz.content import (draft_piece, draft_piece_prepare, finalize_topics, mark_produced,  # noqa: E402,F401
                             plan_week, plan_week_prepare, publish_piece, review_piece, revise_piece,
                             revise_piece_prepare, schedule_pieces)

# ── OPC 基地招商（私有模块，实现见 opc.py；发布物不含）──
try:
    from autobiz.opc import (add_spaces, end_residency, log_event, release_space,  # noqa: E402,F401
                             reserve_space, sign_residency)
except ImportError:  # pragma: no cover
    pass

# ── 视频生产（可选板块，实现见 video.py）──────────────
# 开源发布物不含视频板块（autobiz/video.py 与 ontology/video.yaml 不随包），
# 私有实例与后续插件安装后自动生效。
try:
    from autobiz.video import (check_video_production, lock_video_creative,  # noqa: E402,F401
                               lock_video_facts, start_video_production, sync_video_sop)
except ImportError:  # pragma: no cover - 发布物构建时验证
    pass
