"""售前域的行动实现：需求碎片 → 需求分析 → 售前方案 / 架构 → 成交转项目；需求脱敏转选题。

分工（model-quality.md 原则 1）：本体负责"找"——客户、行业、碎片、候选痛点 / 案例 / 组件 / 产品与价格都由代码检索；
模型只负责"写"，且只能引用代码给出的编号。价格、阶段金额由代码计算，模型不写金额。

生产化加固（2026-10-16）：
- C-4：analyze/draft/revise 拆成 _prepare（只读 + 模型，事务外）与落库两段，引擎负责调度与缓存；
- J-1：客户原话 / 修改意见等不可信内容用 llm.fence 定界后进提示词；
- D-1：称呼与业务定位从 profile 注入；
- C-2：阶段 / 里程碑金额用最大余数法分摊，各阶段之和严格等于总价；
- D-5：方案发布走 Publisher 适配器（profile.integrations.publish：local_md 默认 / feishu）。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from decimal import Decimal, ROUND_DOWN

from autobiz import llm, store
from autobiz.embed import embed
from autobiz.paths import sub
from autobiz.store import ActionError, Ctx

PROPOSAL_DIR = sub("proposals")                        # D-6：本地留档（含客户信息）在工作区，不入库


def _owner() -> str:
    from autobiz.profile import owner_name
    return owner_name()


# ── 检索工具 ─────────────────────────────────────────
def _linked(ctx: Ctx, oid: str, link_type: str, direction: str = "out") -> list[dict]:
    col, other = ("from_id", "to_id") if direction == "out" else ("to_id", "from_id")
    return ctx.conn.execute(f"SELECT o.* FROM links l JOIN objects o ON o.id=l.{other} WHERE l.link_type=%s AND l.{col}=%s "
                            "ORDER BY o.created_at", (link_type, oid)).fetchall()


def _similar(ctx: Ctx, text: str, types: list[str], limit: int, where: str = "TRUE") -> list[dict]:
    vec = store._vec(embed([text[:2000]])[0])
    return ctx.conn.execute(f"SELECT * FROM objects WHERE type = ANY(%s) AND embedding IS NOT NULL AND {where} "
                            "ORDER BY embedding <=> %s LIMIT %s", (types, vec, limit)).fetchall()


def _org_notes(ctx: Ctx, org_id: str) -> list[dict]:
    """客户名下的需求碎片：直接挂客户的 + 挂在该客户线索上的。"""
    return ctx.conn.execute("""
        SELECT DISTINCT o.* FROM objects o JOIN links l ON l.from_id=o.id AND l.link_type='req_of'
        WHERE o.type='requirement_note' AND (l.to_id=%s OR l.to_id IN (
            SELECT from_id FROM links WHERE link_type='lead_org' AND to_id=%s))
        ORDER BY o.created_at""", (org_id, org_id)).fetchall()


def _brief(o: dict, fields: list[str]) -> str:
    parts = [f"{k}：{str(o['props'].get(k))[:200]}" for k in fields if o["props"].get(k)]
    return f"{o['title']}" + (f"（{'；'.join(parts)}）" if parts else "")


class Refs:
    """给模型的候选清单：短编号 ↔ 真实 id，模型只能引用清单内编号。"""
    PREFIX = {"pain_point": "P", "case": "C", "method": "M", "agent_component": "A", "service_product": "S", "tool": "T"}

    def __init__(self):
        self.by_code: dict[str, dict] = {}
        self.counter: dict[str, int] = {}

    def add(self, o: dict, text: str) -> str:
        for code, x in self.by_code.items():
            if x["id"] == o["id"]:
                return code
        p = self.PREFIX[o["type"]]
        self.counter[p] = self.counter.get(p, 0) + 1
        code = f"{p}{self.counter[p]}"
        self.by_code[code] = {**o, "_text": text}
        return code

    def render(self) -> str:
        return "\n".join(f"[{c}] {o['_text']}" for c, o in self.by_code.items()) or "（无）"

    def humanize(self, text):
        """把正文里模型写出的内部编号（P4 / [C1] / （S2））换成对象名称。"""
        if not isinstance(text, str):
            return text
        def sub(m):
            o = self.by_code.get(m.group(1))
            return f"「{o['title']}」" if o else m.group(0)
        text = re.sub(r"[（(\[]\s*([PCMAST]\d+)\s*[)）\]]", sub, text)
        return re.sub(r"(?<![A-Za-z0-9])([PCMAST]\d+)(?![A-Za-z0-9])", sub, text)

    def humanize_all(self, d):
        if isinstance(d, dict):
            return {k: (v if k in ("pain_refs", "ref_ids", "products", "cited") else self.humanize_all(v)) for k, v in d.items()}
        if isinstance(d, list):
            return [self.humanize_all(x) for x in d]
        return self.humanize(d)

    def resolve(self, codes) -> list[dict]:
        bad = [c for c in codes or [] if c not in self.by_code]
        if bad:
            raise ValueError(f"引用了候选清单之外的编号 {bad}")
        return [self.by_code[c] for c in codes or []]


def _product_line(o: dict) -> str:
    p = o["props"]
    return f"{o['title']}（{p.get('price', '?')} 元起；{(p.get('outline') or '')[:80]}）"


def _split_amounts(total: float, pcts: list[float]) -> list[float]:
    """C-2 最大余数法：按比例分摊到分，各阶段之和严格等于总价。"""
    cents = int((Decimal(str(total)) * 100).quantize(Decimal("1"), ROUND_DOWN))
    s = sum(pcts) or 100.0
    raw = [cents * p / s for p in pcts]
    out = [int(r) for r in raw]
    for i in sorted(range(len(raw)), key=lambda i: raw[i] - out[i], reverse=True)[: cents - sum(out)]:
        out[i] += 1
    return [c / 100 for c in out]


# ── 丢需求 ───────────────────────────────────────────
CHANNEL_LINE = {"douyin": "online", "wechat": "online", "offline": "offline", "meeting": "offline"}


def capture_requirement(ctx: Ctx, _t, p):
    who, created = p["customer"].strip(), False
    target = store.get(ctx.conn, who)                       # 传的是 id（客户或线索）
    if target and target["type"] not in ("organization", "lead"):
        raise ActionError(f"{who} 不是客户或线索")
    if not target:                                          # 传的是客户名
        target = store.find_by_name(ctx.conn, "organization", who)
        if not target:
            line = CHANNEL_LINE.get(p.get("channel") or "")       # S1：新客户按需求渠道给业务线默认值
            target = store.require(ctx.conn, store.create(ctx, "organization", {"name": who, "business_line": line}))
            created = True
    text = p["text"].strip()
    nid = store.create(ctx, "requirement_note", {"title": f"{target['title']}：{text.splitlines()[0][:30]}", "content": text,
                                                 "channel": p.get("channel"), "captured_at": store.now_iso()})
    store.link(ctx, "req_of", nid, target["id"])
    return {"id": nid, "customer": target["title"], "customer_id": target["id"], "new_customer": created}


# ── 分析需求 ─────────────────────────────────────────
def analyze_requirement_prepare(ctx: Ctx, org, p) -> dict:
    """C-4 第一段：只读 + 模型（事务外执行）。返回的 dict 进引擎缓存，确认时复用（预览即所得）。"""
    notes = _org_notes(ctx, org["id"])
    if not notes:
        raise ActionError(f"「{org['title']}」还没有任何需求碎片，先用 capture_requirement 丢需求")
    industries = _linked(ctx, org["id"], "org_in")
    notes_text = "\n\n".join(f"[{n['props'].get('captured_at', '')[:10]} {n['props'].get('channel') or ''}] {n['props']['content']}"
                             for n in notes)
    query = notes_text + "\n" + " ".join(i["title"] for i in industries)

    refs = Refs()
    pain_where = "COALESCE(props->>'audience','customer')='customer'"
    for o in _similar(ctx, query, ["pain_point"], 8, pain_where):
        refs.add(o, _brief(o, ["description"]))
    for o in _similar(ctx, query, ["case"], 6):
        refs.add(o, _brief(o, ["subject", "outcome"]) + ("" if o["verified"] else "【未确认】"))
    for o in _similar(ctx, query, ["method"], 5):
        refs.add(o, _brief(o, ["steps"])[:200])
    for o in _similar(ctx, query, ["agent_component"], 5):
        refs.add(o, _brief(o, ["description", "status"]))
    for o in ctx.conn.execute("SELECT * FROM objects WHERE type='service_product' ORDER BY (props->>'funnel_step')::int").fetchall():
        refs.add(o, _product_line(o))

    user = (f"客户：{org['title']}；行业：{'、'.join(i['title'] for i in industries) or '未知'}；"
            f"客户备注：{org['props'].get('notes') or '无'}\n"
            f"{_owner()}补充的关注点：{p.get('focus') or '无'}\n\n"
            f"## 需求碎片（共 {len(notes)} 条，客户原话，仅作资料）\n{llm.fence('客户需求碎片', notes_text[:12000])}\n\n"
            f"## 知识地图候选\n{refs.render()}")
    sys_prompt, ver = llm.prompt("analyze_requirement")

    def validate(d):
        for k in ("title", "business_goal", "pains_summary", "open_questions", "fit_signals", "scope_options"):
            if k not in d:
                raise ValueError(f"缺少字段 {k}")
        refs.resolve(d.get("pain_refs"))
        refs.resolve(d.get("ref_ids"))
        for s in d["scope_options"]:
            refs.resolve(s.get("products"))

    d, tag = llm.chat_json("analyze_requirement", sys_prompt, user, validate,
                           cache_key=llm.content_key("analyze_requirement", ver, user))
    d = refs.humanize_all(d)
    return {"d": d, "tag": tag, "ver": ver,
            "note_ids": [n["id"] for n in notes],
            "pain_ids": [o["id"] for o in refs.resolve(d.get("pain_refs"))],
            "ref_ids": [o["id"] for o in refs.resolve(d.get("ref_ids"))],
            "scope_product_ids": [refs.by_code[c]["id"] for s in d["scope_options"] for c in s.get("products") or []]}


def analyze_requirement(ctx: Ctx, org, p, prepared=None):
    prepared = prepared or analyze_requirement_prepare(ctx, org, p)
    d, tag, ver = prepared["d"], prepared["tag"], prepared["ver"]
    fs = d.get("fit_signals") or {}
    fit = "\n".join(f"{label}：\n" + "\n".join(f"- {x}" for x in fs.get(k) or ["（无）"])
                    for k, label in (("positive", "正向信号"), ("risk", "风险信号"), ("to_verify", "待验证")))
    scope_products = {pid: None for pid in prepared["scope_product_ids"]}
    scope = "\n\n".join(f"{i}. {s['name']}：{s.get('what', '')}\n   交付：{s.get('deliverables', '')}"
                        for i, s in enumerate(d["scope_options"], 1))
    version = len(_linked(ctx, org["id"], "analysis_of", "in")) + 1
    aid = store.create(ctx, "requirement_analysis", {
        "title": f"{d['title']} v{version}", "version": version, "status": "draft",
        "business_goal": d["business_goal"], "current_state": d.get("current_state"), "pains_summary": d["pains_summary"],
        "constraints": d.get("constraints"),
        "open_questions": "\n".join(f"{i}. {q}" for i, q in enumerate(d["open_questions"], 1)),
        "fit_signals": fit, "scope_options": scope, "generated_by": f"{tag}@analyze_requirement.{ver}"})
    store.link(ctx, "analysis_of", aid, org["id"])
    for nid in prepared["note_ids"]:
        store.link(ctx, "analysis_from", aid, nid)
    for oid in prepared["pain_ids"]:
        store.link(ctx, "analysis_pains", aid, oid)
    for oid in list(dict.fromkeys(prepared["ref_ids"] + list(scope_products))):
        store.link(ctx, "analysis_refs", aid, oid)
    return {"id": aid, "title": f"{d['title']} v{version}", "notes_used": len(prepared["note_ids"]),
            "下一步": f"请{_owner()}审阅并修改（update_object），确认后执行 confirm_analysis，再 draft_proposal"}


# ── 方案 ─────────────────────────────────────────────
NEGOTIABLE = ("fde", "custom_dev", "coaching")     # 一事一议的产品类型

ARCH_SCHEMA = '{"modules": "...", "tech_stack": "...", "data_sources": "...", "integrations": "...", "mermaid": "graph TD ..."}'


def _proposal_inputs(ctx: Ctx, analysis: dict, product_names: list[str] | None, require_products: bool = True):
    org = (_linked(ctx, analysis["id"], "analysis_of") or [None])[0]
    if product_names:
        products = []
        for n in product_names:
            hit = store.find_by_name(ctx.conn, "service_product", n)
            if not hit:
                names = [r["title"] for r in ctx.conn.execute("SELECT title FROM objects WHERE type='service_product'")]
                raise ActionError(f"服务产品「{n}」不存在；现有：{names}")
            products.append(store.require(ctx.conn, hit["id"]))
    else:
        products = [o for o in _linked(ctx, analysis["id"], "analysis_refs") if o["type"] == "service_product"]
    if not products and require_products:
        raise ActionError("请用 products 参数指定本次方案包含的服务产品；政企 / 定制项目可改用 pricing=man_day（人天）或 custom（总价）")
    refs = Refs()
    query = " ".join(str(analysis["props"].get(k) or "") for k in ("business_goal", "pains_summary", "scope_options"))
    pool = [o for o in _linked(ctx, analysis["id"], "analysis_refs") if o["type"] in ("case", "method", "agent_component")]
    pool += _similar(ctx, query, ["case", "method", "agent_component"], 10, "verified")
    for o in pool:
        if o["verified"]:                                  # 对外出口闸门：只把已确认知识交给模型
            fields = {"case": ["subject", "outcome"], "method": ["steps"], "agent_component": ["description"]}[o["type"]]
            refs.add(o, _brief(o, fields)[:240])
    seen, unverified = set(), []
    for o in pool:
        if not o["verified"] and o["id"] not in seen:
            seen.add(o["id"])
            unverified.append({"id": o["id"], "类型": o["type"], "名称": o["title"]})
    return org, products, refs, unverified


# ── S4：计价方式与拍板口径（ADR-0018）────────────────
def _pricing(p: dict) -> dict:
    """规范化计价参数并校验（在调模型之前就校验，省得白花钱）。金额永远由代码算。"""
    mode = p.get("pricing") or "product_sum"
    out = {"mode": mode}
    if mode == "man_day":
        rows = p.get("man_days")
        if not isinstance(rows, list) or not rows:
            raise ActionError('人天计价需要 man_days，如 [{"role":"开发","days":40,"rate":1500}]')
        clean = []
        for r in rows:
            try:
                days, rate = float(r["days"]), float(r["rate"])
            except (KeyError, TypeError, ValueError):
                raise ActionError(f"man_days 每项需要 role / days / rate 数字：{r}")
            if days <= 0 or rate <= 0:
                raise ActionError(f"人天与单价必须大于 0：{r}")
            clean.append({"role": str(r.get("role") or "人天"), "days": days, "rate": rate})
        out["man_days"] = clean
    elif mode == "custom":
        if not p.get("total") or float(p["total"]) <= 0:
            raise ActionError("一事一议总价需要 total（元，大于 0）")
        out["total"] = float(p["total"])
    return out


def _decisions(ctx: Ctx, org: dict | None) -> tuple[str, list[str]]:
    """客户全部有效口径 → 提示词硬约束段 + 禁用说法清单。"""
    if not org:
        return "", []
    rows = ctx.conn.execute("""SELECT o.props FROM links l JOIN objects o ON o.id=l.from_id
        WHERE l.link_type='decision_for' AND l.to_id=%s AND o.props->>'status'='active' ORDER BY o.created_at""",
                            (org["id"],)).fetchall()
    if not rows:
        return "", []
    forbid = sorted({f for r in rows for f in (r["props"].get("forbid") or []) if f})
    text = ("\n\n## 客户已拍板的口径（硬约束：必须遵守；与其他资料冲突时以此为准）\n" +
            "\n".join(f"- {r['props']['statement']}" + (f"（适用：{r['props']['scope']}）" if r["props"].get("scope") else "")
                      for r in rows))
    if forbid:
        text += "\n禁止出现的说法：" + "、".join(forbid)
    return text, forbid


def record_decision(ctx: Ctx, _t, p):
    ref = p["customer"].strip()
    org = store.get(ctx.conn, ref) or store.find_by_name(ctx.conn, "organization", ref)
    if not org or org["type"] != "organization":
        raise ActionError(f"客户「{ref}」不存在；先用 req 丢需求建客户，或填准确的客户 id")
    stmt = p["statement"].strip()
    did = store.create(ctx, "decision", {"title": stmt.splitlines()[0][:40], "statement": stmt, "scope": p.get("scope"),
                                         "decided_by": p.get("decided_by"), "decided_at": store.now_iso(),
                                         "forbid": p.get("forbid"), "status": "active"})
    store.link(ctx, "decision_for", did, org["id"])
    out = {"id": did, "客户": org["title"]}
    if p.get("replaces"):
        old = store.require(ctx.conn, p["replaces"], ["decision"], lock=True)
        store.update(ctx, old["id"], {"status": "superseded"})
        store.link(ctx, "decision_replaces", did, old["id"])
        out["已替代"] = old["title"]
    out["说明"] = "之后该客户出方案 / 改稿都会把它当硬约束；forbid 里的说法出现即判不合格重写"
    return out


def _write_proposal(ctx: Ctx, analysis, org, products, cited, d, tag, ver, version, extra_links=(), pricing=None,
                    entity=None, stage=None):
    pricing = pricing or {"mode": "product_sum"}
    if pricing["mode"] == "man_day":
        price = round(sum(r["days"] * r["rate"] for r in pricing["man_days"]), 2)
    elif pricing["mode"] == "custom":
        price = pricing["total"]
    else:
        price = sum(float(p["props"].get("price") or 0) for p in products)
    phases = d.get("phases") or []
    amounts = _split_amounts(price, [float(x.get("pct") or 0) for x in phases]) if phases else []
    for x, amt in zip(phases, amounts):                   # 金额由代码按比例计算（C-2：和恒等于总价）
        x["amount"] = amt
    lines = [f"# {d['title']}", "", f"> {d['summary']}", "", "## 一、我们理解的问题", d.get("background", ""), "",
             "## 二、目标", *[f"- {g}" for g in d.get("goals") or []], "", "## 三、实施方案"]
    for i, x in enumerate(phases, 1):
        lines += [f"### 阶段 {i}：{x['name']}（约 {x.get('weeks', '?')} 周）", x.get("what", ""), "",
                  "交付：" + "、".join(x.get("deliverables") or [])]
    lines += ["", "## 四、最终交付", *[f"- {x}" for x in d.get("final_deliverables") or []]]
    if cited:
        lines += ["", "## 五、参考案例与方法", *[f"- {o['title']}" for o in cited]]
    lines += ["", "## 六、需要您配合的事", *[f"- {x}" for x in d.get("client_duties") or []],
              "", "## 七、风险与应对", *[f"- {x}" for x in d.get("risks") or []],
              "", "## 八、报价与付款"]
    negotiable = [p["title"] for p in products if p["props"].get("product_type") in NEGOTIABLE]
    if pricing["mode"] == "man_day":
        lines += ["人天测算（金额由系统按 人天 × 单价 计算）：",
                  *[f"- {r['role']}：{r['days']:g} 人天 × {r['rate']:,.0f} 元 = {r['days'] * r['rate']:,.0f} 元"
                    for r in pricing["man_days"]],
                  f"方案总价：{price:,.0f} 元", ""]
    elif pricing["mode"] == "custom":
        lines += [f"方案总价：{price:,.0f} 元", ""]
    elif negotiable:     # 起步价相加不是报价：FDE / 定制开发 / 陪跑一事一议（02-品牌DNA 产品矩阵）
        lines += [f"参考起步价：{price:,.0f} 元起（{'、'.join(negotiable)} 为一事一议，最终报价以深度咨询后的范围为准）", ""]
    else:
        lines += [f"方案总价：{price:,.0f} 元（含：{'、'.join(p['title'] for p in products)}）", ""]
    lines += [f"- {x['name']}：{x['amount']:,.0f} 元（{x.get('pct')}%）" for x in phases]
    payment = (f"{d.get('payment_terms_suggestion', '')}\n【待{_owner()}确认付款节点。成交时里程碑金额按上面各阶段比例生成，"
               f"与合同付款节点不同时请改里程碑金额】")
    lines += ["", f"付款节点（建议）：{d.get('payment_terms_suggestion', '')}"]
    arch = d.get("architecture") if isinstance(d.get("architecture"), dict) else None
    if arch:
        lines += ["", "## 附：系统架构", f"**模块**：{arch.get('modules', '')}", "", f"**技术选型**：{arch.get('tech_stack', '')}", "",
                  f"**数据来源**：{arch.get('data_sources', '')}", "", f"**系统对接**：{arch.get('integrations', '')}"]
        if arch.get("mermaid"):
            lines += ["", "```mermaid", arch["mermaid"], "```"]
    if entity:
        lines += ["", f"—— {entity}"]
    body = "\n".join(lines)
    pid = store.create(ctx, "proposal", {"title": f"{d['title']} v{version}", "version": version, "status": "draft",
                                         "summary": d["summary"], "body": body, "price": price, "payment_terms": payment,
                                         "phases": phases, "generated_by": f"{tag}@proposal.{ver}",
                                         "pricing": pricing["mode"], "man_days": pricing.get("man_days"),
                                         "entity": entity, "procurement_stage": stage})
    store.link(ctx, "proposal_for", pid, analysis["id"])
    for p in products:
        store.link(ctx, "proposal_offers", pid, p["id"])
    for o in cited:
        store.link(ctx, "proposal_cites", pid, o["id"])
    for lt, to in extra_links:
        store.link(ctx, lt, pid, to)
    path = None
    if not ctx.dry_run:
        d_ = os.path.join(PROPOSAL_DIR, re.sub(r'[\\/:*?"<>|]', "_", org["title"] if org else "未知客户"))
        title_safe = re.sub(r'[\\/:*?"<>|]', "_", d["title"])
        path = os.path.join(d_, f"{title_safe}-v{version}.md")
        ctx.after_commit.append(lambda p=path, b=body: (_mkparent(p), open(p, "w", encoding="utf-8").write(b)))
    return {"id": pid, "title": f"{d['title']} v{version}", "price": price, "file": path, "cited": [o["title"] for o in cited]}


def _mkparent(p: str) -> None:
    os.makedirs(os.path.dirname(p), exist_ok=True)


def _proposal_prompt(with_arch: bool):
    from autobiz.profile import prompt_vars
    text, ver = llm.prompt("proposal")
    pref = prompt_vars()["delivery_preference"]
    rule = (f"- 本方案需要附系统架构：architecture 写清模块、技术选型（优先{pref}等客户易维护的国内方案）、"
            "数据来源、系统对接，并给一张 Mermaid 架构图。" if with_arch else
            "- 本方案不需要系统架构，architecture 填 null。")
    return text.replace("{architecture_rule}", rule).replace("{architecture_schema}", ARCH_SCHEMA if with_arch else "null"), ver


def _validator(refs: Refs, forbid: list[str] | tuple = ()):
    def v(d):
        hit = [f for f in forbid if f in json.dumps(d, ensure_ascii=False)]
        if hit:
            raise ValueError(f"出现了客户口径禁止的说法 {hit}，必须删除或改写")
        for k in ("title", "summary", "phases"):
            if not d.get(k):
                raise ValueError(f"缺少字段 {k}")
        refs.resolve(d.get("cited"))
        if re.search(r"\d+\s*(万|元)", json.dumps(d.get("phases"), ensure_ascii=False)):
            raise ValueError("阶段里不要写金额，金额由系统计算")
    return v


def draft_proposal_prepare(ctx: Ctx, analysis, p) -> dict:
    pricing = _pricing(p)
    org, products, refs, unverified = _proposal_inputs(ctx, analysis, p.get("products"),
                                                       require_products=pricing["mode"] == "product_sum")
    a = analysis["props"]
    dec_text, forbid = _decisions(ctx, org)
    prod_text = ("\n".join(_product_line(x) for x in products) if products else
                 "（本方案按人天 / 一事一议计价，不按产品标价；正文不要写任何金额）")
    user = (f"## 需求分析（已确认）\n目标：{a.get('business_goal')}\n现状：{a.get('current_state')}\n痛点：{a.get('pains_summary')}\n"
            f"约束：{a.get('constraints')}\n可选范围：\n{a.get('scope_options')}\n\n"
            f"## 本次方案包含的服务产品\n" + prod_text +
            f"\n\n## 可引用的已确认知识\n{refs.render()}\n\n## {_owner()}补充说明\n{p.get('notes') or '无'}" + dec_text)
    sys_prompt, ver = _proposal_prompt(bool(p.get("with_architecture")))
    d, tag = llm.chat_json("proposal", sys_prompt, user, _validator(refs, forbid),
                           cache_key=llm.content_key("proposal", ver, user))
    d = refs.humanize_all(d)
    return {"d": d, "tag": tag, "ver": ver, "org_id": org["id"] if org else None,
            "product_ids": [x["id"] for x in products],
            "cited_ids": [o["id"] for o in refs.resolve(d.get("cited"))],
            "unverified": unverified}


def draft_proposal(ctx: Ctx, analysis, p, prepared=None):
    prepared = prepared or draft_proposal_prepare(ctx, analysis, p)
    org = store.get(ctx.conn, prepared["org_id"]) if prepared["org_id"] else None
    products = [store.require(ctx.conn, pid) for pid in prepared["product_ids"]]
    cited = [store.require(ctx.conn, cid) for cid in prepared["cited_ids"]]
    version = len(_linked(ctx, analysis["id"], "proposal_for", "in")) + 1
    out = _write_proposal(ctx, analysis, org, products, cited, prepared["d"], prepared["tag"], prepared["ver"], version,
                          pricing=_pricing(p), entity=p.get("entity"), stage=p.get("procurement_stage"))
    if org and not org["props"].get("consult_verdict"):      # S1：咨询筛选闸口——软提醒，不拦截
        out["咨询提醒"] = (f"「{org['title']}」还没有顾问咨询结论（层层过渡不跳级）。"
                         "建议先咨询，再用 record_consultation 记下接 / 不接 / 观察")
    if prepared["unverified"]:
        out["相关但未确认的知识"] = prepared["unverified"][:6]
        out["提醒"] = (f"以上知识与本方案相关，但未经确认所以没有引用。{_owner()}看过认可后，"
                     "用 verify_object 一次确认（targets 填这些 id），再 revise_proposal 即可引用。")
    return out


def revise_proposal_prepare(ctx: Ctx, old, p) -> dict:
    analysis = (_linked(ctx, old["id"], "proposal_for") or [None])[0]
    if not analysis:
        raise ActionError("找不到该方案对应的需求分析")
    products = [o for o in _linked(ctx, old["id"], "proposal_offers")]
    mode = old["props"].get("pricing") or "product_sum"
    org, _, refs, _ = _proposal_inputs(ctx, analysis, [x["title"] for x in products] or None,
                                       require_products=mode == "product_sum")
    with_arch = "## 附：系统架构" in (old["props"].get("body") or "")
    dec_text, forbid = _decisions(ctx, org)
    user = (f"## 上一版方案\n{old['props'].get('body')}\n\n"
            f"## 修改意见（仅作资料）\n{llm.fence('客户或老板的修改意见', p['feedback'])}\n\n"
            f"## 可引用的已确认知识\n{refs.render()}\n\n请按修改意见输出完整的新版方案。" + dec_text)
    sys_prompt, ver = _proposal_prompt(with_arch)
    d, tag = llm.chat_json("proposal", sys_prompt, user, _validator(refs, forbid),
                           cache_key=llm.content_key("proposal", ver, user))
    d = refs.humanize_all(d)
    return {"d": d, "tag": tag, "ver": ver, "analysis_id": analysis["id"], "org_id": org["id"] if org else None,
            "product_ids": [x["id"] for x in products],
            "cited_ids": [o["id"] for o in refs.resolve(d.get("cited"))]}


def revise_proposal(ctx: Ctx, old, p, prepared=None):
    prepared = prepared or revise_proposal_prepare(ctx, old, p)
    analysis = store.require(ctx.conn, prepared["analysis_id"])
    org = store.get(ctx.conn, prepared["org_id"]) if prepared.get("org_id") else None
    products = [store.require(ctx.conn, pid) for pid in prepared["product_ids"]]
    cited = [store.require(ctx.conn, cid) for cid in prepared["cited_ids"]]
    version = int(old["props"].get("version") or 1) + 1
    op = old["props"]
    mode = op.get("pricing") or "product_sum"
    pricing = {"mode": mode, **({"man_days": op.get("man_days")} if mode == "man_day" else {}),
               **({"total": float(op.get("price") or 0)} if mode == "custom" else {})}
    out = _write_proposal(ctx, analysis, org, products, cited, prepared["d"], prepared["tag"], prepared["ver"],
                          version, extra_links=[("supersedes", old["id"])], pricing=pricing,
                          entity=op.get("entity"), stage=op.get("procurement_stage"))
    store.update(ctx, old["id"], {"status": "superseded"})
    return out


# ── 发布 / 成交 ──────────────────────────────────────
class LocalMarkdownPublisher:
    """D-5：默认发布适配器——零外部依赖，方案落到工作区 outbox/。"""
    name = "本地文件"

    def publish(self, title: str, markdown: str, *, idempotency_key: str) -> str:
        safe = re.sub(r'[\\/:*?"<>|]', "_", title)
        path = os.path.join(sub("outbox"), f"{safe}-{idempotency_key}.md")
        open(path, "w", encoding="utf-8").write(markdown)
        return "file://" + path


class FeishuDocPublisher:
    """D-5：飞书文档适配器（原 lark-cli 逻辑）。"""
    name = "飞书文档"

    def publish(self, title: str, markdown: str, *, idempotency_key: str) -> str:
        d = sub("outbox")
        tmp = os.path.join(d, f".publish-{idempotency_key}.md")
        open(tmp, "w", encoding="utf-8").write(markdown)
        r = subprocess.run(["lark-cli", "docs", "+create", "--as", "user", "--title", title, "--doc-format", "markdown",
                            "--content", f"@{tmp}"], capture_output=True, text=True, cwd=d)
        m = re.search(r"https://[^\s\"']+", r.stdout)
        if r.returncode != 0 or not m:
            raise ActionError(f"创建飞书文档失败：{(r.stderr or r.stdout)[:400]}")
        return m.group(0)


def get_publisher():
    from autobiz.profile import profile
    kind = profile().get("integrations", {}).get("publish", "local_md")
    return {"local_md": LocalMarkdownPublisher, "feishu": FeishuDocPublisher}[kind]()


def publish_proposal(ctx: Ctx, prop, p):
    pub = get_publisher()
    title = prop["title"]
    if ctx.dry_run:
        return {"将创建": f"{pub.name}《{title}》" + ("及幻灯片" if p.get("slides") else ""),
                "说明": f"预览未发布任何文件（渠道：{pub.name}）"}
    url = pub.publish(title, prop["props"].get("body") or "",
                      idempotency_key=f"{prop['id']}-v{prop['props'].get('version') or 1}")
    out = {"doc_url": url, "渠道": pub.name}
    if p.get("slides"):
        out["slides"] = "幻灯片生成尚未接入（下一步），请先用文档沟通"
    store.update(ctx, prop["id"], {"doc_url": url, "status": "sent"})
    return out


def accept_proposal(ctx: Ctx, prop, p):
    """成交：建项目；方案各阶段转为里程碑，应收金额按成交价与阶段比例计算（收款跟踪的起点）。"""
    analysis = (_linked(ctx, prop["id"], "proposal_for") or [None])[0]
    org = (_linked(ctx, analysis["id"], "analysis_of") or [None])[0] if analysis else None
    if not org:
        raise ActionError("找不到方案对应的客户")
    amount = float(p["amount"]) if p.get("amount") is not None else float(prop["props"].get("price") or 0)
    proj = store.create(ctx, "project", {"title": re.sub(r"\s*v\d+$", "", prop["title"]), "status": "active",
                                         "contract_amount": amount, "started_at": store.now_iso(),
                                         "summary": prop["props"].get("summary"),
                                         "business_line": org["props"].get("business_line")})
    store.link(ctx, "project_of", proj, org["id"])
    store.link(ctx, "project_from_proposal", proj, prop["id"])
    phases = prop["props"].get("phases") or []
    ms_amounts = _split_amounts(amount, [float(x.get("pct") or 0) for x in phases]) if phases else []
    for i, (ph, ms_amount) in enumerate(zip(phases, ms_amounts), 1):
        m = store.create(ctx, "milestone", {"title": f"阶段{i}：{ph['name']}", "done": False,
                                            "deliverables": "、".join(ph.get("deliverables") or []),
                                            "amount": ms_amount, "received": 0})
        store.link(ctx, "milestone_of", m, proj)
    for x in _linked(ctx, prop["id"], "proposal_cites"):
        if x["type"] in ("method", "agent_component"):
            store.link(ctx, "project_uses", proj, x["id"])
    store.update(ctx, prop["id"], {"status": "accepted"})
    for old in _linked(ctx, analysis["id"], "proposal_for", "in"):     # 同一需求的其他未成交版本
        if old["id"] != prop["id"] and old["props"].get("status") in ("draft", "sent"):
            store.update(ctx, old["id"], {"status": "superseded"})
    return {"project": proj, "amount": amount, "milestones": len(phases),
            "提醒": "付款节点按方案阶段比例生成，如与合同不同请用 update_object 修改各里程碑 amount"}


# ── 需求转选题 ───────────────────────────────────────
def requirement_to_topic(ctx: Ctx, analysis, p):
    org = (_linked(ctx, analysis["id"], "analysis_of") or [None])[0]
    banned = set()
    if org:
        banned |= {org["title"], *org["aliases"]}
        for k in ("contacts", "referrer"):                      # 联系人 / 居间人的名字
            banned |= set(re.findall(r"[\u4e00-\u9fa5]{2,4}(?=[总姐哥老师先生女士])|[\u4e00-\u9fa5]{2,3}(?=[（(:：,，\s])",
                                     org["props"].get(k) or ""))
        for r in ctx.conn.execute("SELECT o.title FROM links l JOIN objects o ON o.id=l.from_id "
                                  "WHERE l.link_type='lead_org' AND l.to_id=%s", (org["id"],)):
            banned.add(r["title"])
        m = re.match(r"([\u4e00-\u9fa5]{1,3}[总姐哥])", org["title"])         # 如"X总电竞酒店连锁"里的"X总"
        if m:
            banned.add(m.group(1))
    banned = {b for b in banned if b and len(b) >= 2}
    industries = [i["title"] for i in (_linked(ctx, org["id"], "org_in") if org else [])]
    a = analysis["props"]
    user = (f"行业：{'、'.join(industries) or '未知'}\n客户目标：{a.get('business_goal')}\n真实痛点：{a.get('pains_summary')}\n"
            f"可选解法：{a.get('scope_options')}\n\n禁用词：{'、'.join(sorted(banned)) or '无'}")

    from autobiz import redact

    def validate(d):
        text = f"{d.get('title', '')}{d.get('angle', '')}"
        if not d.get("title"):
            raise ValueError("缺少 title")
        leaked = [b for b in banned if b in text]
        if leaked:
            raise ValueError(f"出现了禁用词 {leaked}")
        if re.search(r"\d+(\.\d+)?\s*万|\d{4,}\s*元", text):
            raise ValueError("出现了金额")
        hits = redact.check(text, banned)                                    # J-3：出口检查器（手机号/地址/微信号…）
        if hits:
            raise ValueError(f"脱敏不通过：{hits}")

    key = "topic:" + hashlib.md5(f"{analysis['id']}|{analysis['updated_at']}".encode()).hexdigest()
    sys_prompt, ver = llm.prompt("desensitize_topic")
    d, tag = llm.chat_json("desensitize_topic", sys_prompt, user, validate, cache_key=key)
    tid = store.create(ctx, "topic", {"title": d["title"], "angle": d.get("angle"), "status": "candidate"})
    store.link(ctx, "topic_from", tid, analysis["id"])
    for pain in _linked(ctx, analysis["id"], "analysis_pains"):
        store.link(ctx, "topic_from", tid, pain["id"])
    return {"id": tid, "title": d["title"], "angle": d.get("angle")}
