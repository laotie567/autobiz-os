"""本体函数：只读查询。业务推理在这里用代码 / SQL 算好，模型只负责解读（model-quality.md 原则 1）。

签名统一为 (conn, onto, **params) -> dict，结果里的枚举一律转为中文标签。
"""
from __future__ import annotations

import psycopg

from autobiz import store
from autobiz.embed import embed
from autobiz.ontology import Ontology

MASTERY_OK = ("practiced", "delivered")


def _label(onto: Ontology, type_: str) -> str:
    t = onto.object_types.get(type_)
    return t.label if t else type_


def _display_props(onto: Ontology, type_: str, props: dict) -> dict:
    t = onto.object_types[type_]
    out = {}
    for k, v in props.items():
        pt = t.properties.get(k)
        if pt and pt.base == "enum" and v is not None:
            v = [onto.enum_label(pt.enum, x) for x in v] if pt.is_list else onto.enum_label(pt.enum, v)
        out[k] = v
    return out


def _brief(onto, r) -> dict:
    return {"id": r["id"], "类型": _label(onto, r["type"]), "名称": r["title"], **({"已确认": r["verified"]} if "verified" in r else {})}


def _resolve(conn, onto, ref: str, type_: str) -> dict:
    """接受 id 或名称。"""
    o = store.get(conn, ref) or store.find_by_name(conn, type_, ref)
    if not o:
        raise store.ActionError(f"找不到{_label(onto, type_)}「{ref}」")
    return o


# ── 通用 ────────────────────────────────────────────
def overview(conn: psycopg.Connection, onto: Ontology) -> dict:
    counts = {_label(onto, r["type"]): r["n"] for r in conn.execute(
        "SELECT type, count(*) n FROM objects GROUP BY type ORDER BY n DESC").fetchall()}
    q = lambda sql, *a: conn.execute(sql, a).fetchone()["n"]
    todo = {
        "待抽取的来源文档": q("SELECT count(*) n FROM objects WHERE type='source_doc' AND props->>'status' IN ('unprocessed','failed')"),
        "疑似重复对象对": q("SELECT count(*) n FROM dup_candidates"),   # I-1：由 autobiz dedupe 后台刷新，overview 不再平方级自连接
        "候选选题（待定档）": q("SELECT count(*) n FROM objects WHERE type='topic' AND props->>'status'='candidate'"),
        "已定档待发布": q("SELECT count(*) n FROM objects WHERE type='topic' AND props->>'status'='scheduled'"),
        "缺7天数据的发布": q("""SELECT count(*) n FROM objects WHERE type='publish_record' AND props->>'views_7d' IS NULL
                           AND (props->>'published_at')::timestamptz < now() - interval '7 days'"""),
        "待跟进线索": q("SELECT count(*) n FROM objects WHERE type='lead' AND props->>'status' IN ('new','following')"),
        "待确认的需求分析": q("SELECT count(*) n FROM objects WHERE type='requirement_analysis' AND props->>'status'='draft'"),
        "已发送待成交的方案": q("SELECT count(*) n FROM objects WHERE type='proposal' AND props->>'status'='sent'"),
        "进行中项目": q("SELECT count(*) n FROM objects WHERE type='project' AND props->>'status'='active'"),
        "已完成未收齐款的里程碑": q("""SELECT count(*) n FROM objects WHERE type='milestone' AND (props->>'done')::bool
                                 AND COALESCE((props->>'received')::numeric,0) < COALESCE((props->>'amount')::numeric,0)"""),
    }
    ai = q("SELECT count(*) n FROM objects WHERE origin='ai' AND NOT verified")
    out = {"对象数量": counts, "待办": {k: v for k, v in todo.items() if v},
           "说明": f"知识库中有 {ai} 条 AI 抽取、未确认的知识，用到时再确认即可，无需清理"}
    try:
        spent = conn.execute("SELECT COALESCE(sum(cost_cny),0) s FROM llm_usage WHERE at > date_trunc('month', now())").fetchone()["s"]
        out["本月模型花费"] = f"{float(spent):.2f} 元"
    except Exception as e:
        out["本月模型花费"] = f"（记账表不可用：{type(e).__name__}: {e}）"   # 不静默吞掉
    return out


def search(conn, onto, query: str, types: list[str] | None = None, limit: int | None = None) -> dict:
    vec = store._vec(embed([query])[0])
    types = types or list(onto.object_types)
    rows = conn.execute(
        "SELECT id, type, title, verified, props, 1 - (embedding <=> %s) score FROM objects "
        "WHERE embedding IS NOT NULL AND type = ANY(%s) ORDER BY embedding <=> %s LIMIT %s",
        (vec, types, vec, limit or 10)).fetchall()
    out = []
    for r in rows:
        t = onto.object_types[r["type"]]
        snippet = " / ".join(str(r["props"].get(f))[:80] for f in t.embed if f != t.title and r["props"].get(f))
        out.append({**_brief(onto, r), "相似度": round(float(r["score"]), 3), "摘要": snippet[:160]})
    return {"结果": out}


def get_object(conn, onto, id: str) -> dict:
    o = store.get(conn, id)
    if not o:
        raise store.ActionError(f"对象 {id} 不存在")
    edges = conn.execute("""
        SELECT l.link_type, 'out' dir, o.id, o.type, o.title, o.verified FROM links l JOIN objects o ON o.id=l.to_id WHERE l.from_id=%s
        UNION ALL
        SELECT l.link_type, 'in' dir, o.id, o.type, o.title, o.verified FROM links l JOIN objects o ON o.id=l.from_id WHERE l.to_id=%s
        """, (id, id)).fetchall()
    rel: dict[str, list] = {}
    for e in edges:
        lt = onto.link_types[e["link_type"]]
        key = f"{lt.label}（{'指向' if e['dir'] == 'out' else '来自'}）"
        rel.setdefault(key, []).append(_brief(onto, e))
    sysf = {"已确认": o["verified"], "来源": {"ai": "AI抽取", "human": "人工", "seed": "种子数据"}.get(o["origin"], o["origin"])}
    if o["origin"] == "ai":
        sysf.update({"置信度": o["confidence"], "抽取模型": o["extracted_by"], "原文引文": o["evidence"]})
    if o["aliases"]:
        sysf["别名"] = o["aliases"]
    return {"id": o["id"], "类型": _label(onto, o["type"]), "名称": o["title"],
            "属性": _display_props(onto, o["type"], o["props"]), **sysf, "关系": rel}


def trace(conn, onto, id: str, direction: str | None = None, depth: int | None = None) -> dict:
    """沿关系链广度优先展开，输出"A —关系→ B"的边列表。"""
    direction, depth = direction or "both", min(depth or 4, 8)
    root = store.require(conn, id)
    seen, frontier, edges, seen_edges = {id}, [id], [], set()
    for _ in range(depth):
        nxt = []
        for node in frontier:
            rows = []
            if direction in ("out", "both"):
                rows += conn.execute("SELECT l.link_type, l.from_id, l.to_id, l.to_id other FROM links l WHERE l.from_id=%s", (node,)).fetchall()
            if direction in ("in", "both"):
                rows += conn.execute("SELECT l.link_type, l.from_id, l.to_id, l.from_id other FROM links l WHERE l.to_id=%s", (node,)).fetchall()
            for r in rows:
                key = (r["link_type"], r["from_id"], r["to_id"])
                if key in seen_edges:
                    continue
                seen_edges.add(key)
                edges.append(key)
                if r["other"] not in seen:
                    seen.add(r["other"])
                    nxt.append(r["other"])
        frontier = nxt
    names = {r["id"]: r for r in conn.execute("SELECT id, type, title FROM objects WHERE id = ANY(%s)", (list(seen),)).fetchall()}
    fmt = lambda i: f"[{_label(onto, names[i]['type'])}] {names[i]['title']}"
    lines = [f"{fmt(f)} —{onto.link_types[lt].label}→ {fmt(t)}" for (lt, f, t) in edges]
    return {"起点": fmt(root["id"]), "涉及对象数": len(seen), "关系链": lines[:200]}


# ── 知识域 ──────────────────────────────────────────
def industry_map(conn, onto, industry: str) -> dict:
    ind = _resolve(conn, onto, industry, "industry")
    q = conn.execute
    pains = q("""SELECT p.id, p.title, p.verified, p.props->>'willingness_to_pay' wtp FROM links l JOIN objects p ON p.id=l.from_id
                 WHERE l.link_type='pain_in' AND l.to_id=%s AND COALESCE(p.props->>'audience','customer')='customer'""",
              (ind["id"],)).fetchall()
    result, gaps = [], []
    for p in pains:
        cases = q("""SELECT c.id, c.title, c.verified, c.props->>'case_origin' origin FROM links l JOIN objects c ON c.id=l.from_id
                     WHERE l.link_type='case_solves' AND l.to_id=%s""", (p["id"],)).fetchall()
        methods = q("""SELECT m.id, m.title, m.props->>'maturity' maturity FROM links l JOIN objects m ON m.id=l.from_id
                       WHERE l.link_type='method_targets' AND l.to_id=%s""", (p["id"],)).fetchall()
        offers = q("""SELECT o.id, o.type, o.title FROM links l JOIN objects o ON o.id=l.from_id
                      WHERE l.link_type='solves' AND l.to_id=%s""", (p["id"],)).fetchall()
        sol_ids = [c["id"] for c in cases] + [m["id"] for m in methods]
        tools = q("""SELECT DISTINCT o.id, o.type, o.title, o.props->>'mastery' mastery FROM links l JOIN objects o ON o.id=l.to_id
                     WHERE l.link_type IN ('case_uses','method_uses') AND l.from_id = ANY(%s) AND o.type IN ('tool','concept')""",
                  (sol_ids,)).fetchall()
        entry = {
            "痛点": p["title"], "id": p["id"], "付费意愿": onto.enum_label("willingness", p["wtp"]) if p["wtp"] else "未知",
            "案例": [{"名称": c["title"], "id": c["id"], "来源": onto.enum_label("case_origin", c["origin"]) if c["origin"] else "",
                      "已确认": c["verified"]} for c in cases],
            "方法": [{"名称": m["title"], "id": m["id"]} for m in methods],
            "我的产品 / 组件": [{"名称": o["title"], "类型": _label(onto, o["type"])} for o in offers],
            "涉及工具与概念": [{"名称": t["title"], "掌握度": onto.enum_label("mastery", t["mastery"]) if t["mastery"] else "未标"}
                               for t in tools],
        }
        result.append(entry)
        if not cases:
            gaps.append(f"「{p['title']}」没有任何案例")
        elif not any(c["origin"] == "own" for c in cases):
            gaps.append(f"「{p['title']}」还没有自己交付的案例")
        if not offers:
            gaps.append(f"「{p['title']}」还没有对应的产品或 Agent 组件")
    orgs = q("""SELECT o.id, o.title FROM links l JOIN objects o ON o.id=l.from_id WHERE l.link_type='org_in' AND l.to_id=%s""",
             (ind["id"],)).fetchall()
    return {"行业": ind["title"], "立场": onto.enum_label("industry_stance", ind["props"].get("stance")),
            "痛点全景": result, "空白": gaps, "该行业客户": [o["title"] for o in orgs]}


def learning_gaps(conn, onto, industry: str | None = None, limit: int | None = None) -> dict:
    if industry:
        inds = [_resolve(conn, onto, industry, "industry")["id"]]
    else:
        inds = [r["id"] for r in conn.execute("SELECT id FROM objects WHERE type='industry' AND props->>'stance'='target'").fetchall()]
    rows = conn.execute("""
        WITH pains AS (SELECT from_id id FROM links WHERE link_type='pain_in' AND to_id = ANY(%s)),
             sols AS (SELECT from_id id, to_id pain FROM links WHERE link_type IN ('case_solves','method_targets')
                      AND to_id IN (SELECT id FROM pains))
        SELECT o.id, o.type, o.title, o.props->>'mastery' mastery, count(DISTINCT s.pain) pains,
               array_agg(DISTINCT p.title) pain_titles
        FROM sols s JOIN links u ON u.from_id=s.id AND u.link_type IN ('case_uses','method_uses')
        JOIN objects o ON o.id=u.to_id AND o.type IN ('tool','concept')
        JOIN objects p ON p.id=s.pain
        WHERE COALESCE(o.props->>'mastery','') NOT IN ('practiced','delivered')
        GROUP BY o.id, o.type, o.title, o.props->>'mastery'
        ORDER BY pains DESC, o.title LIMIT %s""", (inds, limit or 10)).fetchall()
    return {"建议优先学习": [{"名称": r["title"], "类型": _label(onto, r["type"]), "id": r["id"],
                              "当前掌握度": onto.enum_label("mastery", r["mastery"]) if r["mastery"] else "未标",
                              "能解决的痛点数": r["pains"], "痛点": r["pain_titles"]} for r in rows],
            "说明": "依据：目标行业痛点的案例 / 方法所用到、而我尚未实操过的工具与概念"}


def industry_coverage(conn, onto) -> dict:
    rows = conn.execute("""
        SELECT i.id, i.title, i.props->>'stance' stance,
          (SELECT count(*) FROM links l JOIN objects p ON p.id=l.from_id WHERE l.link_type='pain_in' AND l.to_id=i.id
             AND COALESCE(p.props->>'audience','customer')='customer') pains,
          (SELECT count(DISTINCT e.to_id) FROM links l JOIN links e ON e.from_id=l.from_id AND e.link_type='extracted_from'
             WHERE l.link_type IN ('pain_in','case_in') AND l.to_id=i.id) sources,
          (SELECT count(*) FROM links l JOIN objects c ON c.id=l.from_id WHERE l.link_type='case_in' AND l.to_id=i.id
             AND c.props->>'case_origin'='external') ext_cases,
          (SELECT count(*) FROM links l JOIN objects c ON c.id=l.from_id WHERE l.link_type='case_in' AND l.to_id=i.id
             AND c.props->>'case_origin'='own') own_cases,
          (SELECT count(DISTINCT s.from_id) FROM links l JOIN links s ON s.to_id=l.from_id AND s.link_type='solves'
             WHERE l.link_type='pain_in' AND l.to_id=i.id) offers
        FROM objects i WHERE i.type='industry' AND i.props->>'stance' IN ('target','watching')
        ORDER BY i.props->>'stance' DESC, i.title""").fetchall()
    out = []
    for r in rows:
        weak = []
        if r["sources"] < 5:
            weak.append("行业资料少于 5 篇")
        if r["ext_cases"] + r["own_cases"] == 0:
            weak.append("没有任何案例")
        if r["own_cases"] == 0:
            weak.append("没有自己交付的案例")
        out.append({"行业": r["title"], "立场": onto.enum_label("industry_stance", r["stance"]), "来源资料": r["sources"],
                    "客户痛点": r["pains"], "外部案例": r["ext_cases"], "自有案例": r["own_cases"], "我的产品": r["offers"],
                    "短板": weak})
    return {"行业覆盖": out, "说明": "来源资料 = 抽出过该行业痛点或案例的文档数；种子数据不计入"}


def capability_map(conn, onto) -> dict:
    rows = conn.execute("SELECT type, title, COALESCE(props->>'mastery','unset') m FROM objects "
                        "WHERE type IN ('tool','concept') ORDER BY title").fetchall()
    groups: dict[str, list] = {}
    for r in rows:
        key = onto.enum_label("mastery", r["m"]) if r["m"] != "unset" else "未标"
        groups.setdefault(key, []).append(f"{r['title']}（{_label(onto, r['type'])}）")
    logs = conn.execute("SELECT title, props->>'done_at' d FROM objects WHERE type='practice_log' "
                        "ORDER BY created_at DESC LIMIT 5").fetchall()
    return {"按掌握度": {k: {"数量": len(v), "列表": v[:30]} for k, v in groups.items()},
            "最近实操": [f"{r['d'][:10] if r['d'] else ''} {r['title']}" for r in logs]}


# ── 市场域 ──────────────────────────────────────────
def content_performance(conn, onto, limit: int | None = None) -> dict:
    rows = conn.execute("""
        SELECT pr.id, pr.title, pr.props p,
          (SELECT count(*) FROM links WHERE link_type='lead_from' AND to_id=pr.id) leads_linked,
          (SELECT t.title FROM links l JOIN objects t ON t.id=l.to_id WHERE l.link_type='published_as' AND l.from_id=pr.id LIMIT 1) topic,
          (SELECT string_agg(src.title, '、') FROM links l JOIN links tf ON tf.from_id=l.to_id AND tf.link_type='topic_from'
             JOIN objects src ON src.id=tf.to_id WHERE l.link_type='published_as' AND l.from_id=pr.id) sources
        FROM objects pr WHERE pr.type='publish_record'""").fetchall()
    out = []
    for r in rows:
        p = r["p"]
        leads = max(int(p.get("leads_dm") or 0) + int(p.get("leads_contact") or 0), r["leads_linked"])
        out.append({"发布": r["title"], "id": r["id"], "平台": onto.enum_label("platform", p.get("platform")) if p.get("platform") else "",
                    "播放24h/72h/7d": [p.get("views_24h"), p.get("views_72h"), p.get("views_7d")],
                    "点赞": p.get("likes"), "涨粉": p.get("new_followers"), "获客": leads, "选题": r["topic"],
                    "选题来源": r["sources"]})
    out.sort(key=lambda x: (x["获客"], max([v or 0 for v in x["播放24h/72h/7d"]])), reverse=True)
    return {"内容表现": out[: limit or 20]}


# ── 客户域 ──────────────────────────────────────────
def pipeline(conn, onto) -> dict:
    leads = conn.execute("SELECT props->>'status' s, count(*) n FROM objects WHERE type='lead' GROUP BY 1").fetchall()
    props = conn.execute("""
        SELECT p.id, p.title, p.props->>'status' st, (p.props->>'price')::numeric price,
          (SELECT o.title FROM links l1 JOIN links l2 ON l2.from_id=l1.to_id AND l2.link_type='analysis_of'
             JOIN objects o ON o.id=l2.to_id WHERE l1.link_type='proposal_for' AND l1.from_id=p.id LIMIT 1) org
        FROM objects p WHERE p.type='proposal' AND p.props->>'status' <> 'superseded' ORDER BY p.created_at DESC""").fetchall()
    by: dict[str, dict] = {}
    for x in props:
        g = by.setdefault(onto.enum_label("proposal_status", x["st"]), {"数量": 0, "金额": 0, "方案": []})
        g["数量"] += 1
        g["金额"] += float(x["price"] or 0)
        g["方案"].append({"名称": x["title"], "id": x["id"], "客户": x["org"]})
    return {"线索": {onto.enum_label("lead_status", r["s"]): r["n"] for r in leads}, "方案": by}


# ── 交付域 ──────────────────────────────────────────
def receivables(conn, onto) -> dict:
    rows = conn.execute("""
        SELECT p.id, p.title, (p.props->>'contract_amount')::numeric contract,
          (SELECT o.title FROM links l JOIN objects o ON o.id=l.to_id WHERE l.link_type='project_of' AND l.from_id=p.id LIMIT 1) org
        FROM objects p WHERE p.type='project' ORDER BY p.created_at""").fetchall()
    out, total_due, total_got = [], 0.0, 0.0
    for p in rows:
        ms = conn.execute("""SELECT m.title, m.props FROM links l JOIN objects m ON m.id=l.from_id
                             WHERE l.link_type='milestone_of' AND l.to_id=%s ORDER BY m.title""", (p["id"],)).fetchall()
        got = sum(float(m["props"].get("received") or 0) for m in ms)
        due = float(p["contract"] or 0) or sum(float(m["props"].get("amount") or 0) for m in ms)
        chase = [f"{m['title']}：应收 {float(m['props'].get('amount') or 0):,.0f}，已收 {float(m['props'].get('received') or 0):,.0f}"
                 for m in ms if m["props"].get("done")
                 and float(m["props"].get("received") or 0) < float(m["props"].get("amount") or 0)]
        total_due, total_got = total_due + due, total_got + got
        out.append({"项目": p["title"], "客户": p["org"], "合同额": due, "已收": got, "未收": due - got, "该催款": chase})
    overdue, soon = _milestones_due(conn)
    return {"合计": {"合同额": total_due, "已收": total_got, "未收": total_due - total_got}, "项目": out,
            "已逾期": overdue, "7天内到期": soon}


def _milestones_due(conn) -> tuple[list, list]:
    """S1：读 milestone.due——未收齐且已过期 / 7 天内到期的里程碑。"""
    rows = conn.execute("""
        SELECT m.id, m.title, (m.props->>'due')::timestamptz due,
               COALESCE((m.props->>'amount')::numeric,0) - COALESCE((m.props->>'received')::numeric,0) left_amt,
               (SELECT p.title FROM links l JOIN objects p ON p.id=l.to_id WHERE l.link_type='milestone_of' AND l.from_id=m.id LIMIT 1) proj
        FROM objects m WHERE m.type='milestone' AND m.props->>'due' IS NOT NULL
          AND COALESCE((m.props->>'received')::numeric,0) < COALESCE((m.props->>'amount')::numeric,0)
          AND (m.props->>'due')::timestamptz < now() + interval '7 days'
        ORDER BY due""").fetchall()
    fmt = lambda r: {"里程碑": r["title"], "项目": r["proj"], "id": r["id"], "到期": str(r["due"])[:10],
                     "未收": float(r["left_amt"])}
    now = conn.execute("SELECT now() n").fetchone()["n"]
    return [fmt(r) for r in rows if r["due"] < now], [fmt(r) for r in rows if r["due"] >= now]


def reuse_candidates(conn, onto) -> dict:
    rows = conn.execute("""
        SELECT o.id, o.type, o.title,
          count(*) FILTER (WHERE l.link_type='project_uses') used,
          count(*) FILTER (WHERE l.link_type='project_produced') produced,
          count(*) FILTER (WHERE l.link_type='product_includes') in_products
        FROM objects o JOIN links l ON l.to_id=o.id
        WHERE o.type IN ('method','agent_component')
        GROUP BY o.id, o.type, o.title ORDER BY count(*) DESC LIMIT 20""").fetchall()
    return {"可复用资产": [{"名称": r["title"], "类型": _label(onto, r["type"]), "id": r["id"], "被项目使用": r["used"],
                            "由项目沉淀": r["produced"], "已纳入产品": r["in_products"]} for r in rows]}


# ── 经营驾驶舱（S1 / ADR-0015）──────────────────────
def _tz() -> str:
    from autobiz.profile import timezone_name
    return timezone_name()


def _month_bounds(conn, month: str | None):
    """返回本地时区的 [月初, 下月初) 两个 timestamptz。month 形如 2026-10，缺省本月。"""
    tz = _tz()
    if month:
        start = conn.execute("SELECT (%s || '-01')::timestamp AT TIME ZONE %s s", (month, tz)).fetchone()["s"]
    else:
        start = conn.execute("SELECT date_trunc('month', now() AT TIME ZONE %s) AT TIME ZONE %s s", (tz, tz)).fetchone()["s"]
    end = conn.execute("SELECT ((%s::timestamptz AT TIME ZONE %s) + interval '1 month') AT TIME ZONE %s e",
                       (start, tz, tz)).fetchone()["e"]
    return start, end


_LINE_OF_MILESTONE = """COALESCE(
    (SELECT p.props->>'business_line' FROM links l JOIN objects p ON p.id=l.to_id
      WHERE l.link_type='milestone_of' AND l.from_id = m.id LIMIT 1),
    (SELECT o.props->>'business_line' FROM links l JOIN links l2 ON l2.from_id=l.to_id AND l2.link_type='project_of'
       JOIN objects o ON o.id=l2.to_id WHERE l.link_type='milestone_of' AND l.from_id = m.id LIMIT 1))"""


def _revenue_rows(conn, start, end) -> list[dict]:
    """实收明细：订单（按 paid_at，退款按 refunded_at 冲减）+ 里程碑回款（按行动日志，冲正冲减）。
    里程碑的 received 是累计值，按月拆分必须读 action_log 才准确。"""
    rows = []
    for r in conn.execute("""
        SELECT title, (props->>'amount')::numeric amt, props->>'kind' kind, props->>'business_line' line,
               (props->>'paid_at')::timestamptz paid_at, (props->>'refunded_at')::timestamptz refunded_at
        FROM objects WHERE type='order'""").fetchall():
        if r["paid_at"] and start <= r["paid_at"] < end:
            rows.append({"来源": "订单", "名称": r["title"], "金额": float(r["amt"]), "类型": r["kind"] or "other", "业务线": r["line"]})
        if r["refunded_at"] and start <= r["refunded_at"] < end:
            rows.append({"来源": "退款", "名称": r["title"], "金额": -float(r["amt"]), "类型": r["kind"] or "other", "业务线": r["line"]})
    for r in conn.execute(f"""
        SELECT a.action, (a.params->>'amount')::numeric amt, m.title,
               COALESCE((a.params->>'received_at')::timestamptz, a.finished_at) at, {_LINE_OF_MILESTONE} line
        FROM action_log a JOIN objects m ON m.id = a.params->>'target'
        WHERE a.action IN ('record_payment','reverse_payment') AND a.status='ok'""").fetchall():
        if r["at"] and start <= r["at"] < end:
            sign = -1 if r["action"] == "reverse_payment" else 1
            rows.append({"来源": "回款" if sign > 0 else "冲正", "名称": r["title"], "金额": sign * float(r["amt"]),
                         "类型": "project", "业务线": r["line"]})
    return rows


def revenue(conn, onto, month: str | None = None) -> dict:
    start, end = _month_bounds(conn, month)
    rows = _revenue_rows(conn, start, end)
    by_line: dict[str, float] = {}
    by_kind: dict[str, float] = {}
    for x in rows:
        ln = onto.enum_label("business_line", x["业务线"]) if x["业务线"] else "未标注"
        by_line[ln] = by_line.get(ln, 0) + x["金额"]
        kd = "项目回款" if x["类型"] == "project" else onto.enum_label("order_kind", x["类型"])
        by_kind[kd] = by_kind.get(kd, 0) + x["金额"]
    label = conn.execute("SELECT to_char(%s::timestamptz AT TIME ZONE %s, 'YYYY-MM') m", (start, _tz())).fetchone()["m"]
    out = {"月份": label, "合计": sum(x["金额"] for x in rows),
           "按业务线": by_line, "按类型": by_kind, "明细": rows[:50]}
    if "未标注" in by_line:
        out["提醒"] = "有收入未标业务线，不计入目标：给对应客户 / 项目 / 订单补 business_line"
    return out


def goal_progress(conn, onto) -> dict:
    from autobiz.profile import goals
    g, tz = goals(), _tz()
    start, end = _month_bounds(conn, None)
    t = conn.execute("""SELECT now() n,
        date_trunc('week', now() AT TIME ZONE %(tz)s) AT TIME ZONE %(tz)s wk,
        date_trunc('day', now() AT TIME ZONE %(tz)s) AT TIME ZONE %(tz)s today""", {"tz": tz}).fetchone()
    month_pct = (t["n"] - start) / (end - start)
    week_pct = (t["n"] - t["wk"]).total_seconds() / (7 * 86400)

    rev_lines = set(g.get("revenue_lines") or ["online"])
    rows = _revenue_rows(conn, start, end)
    rev = sum(x["金额"] for x in rows if x["业务线"] in rev_lines)
    others: dict[str, float] = {}
    for x in rows:
        if x["业务线"] not in rev_lines:
            k = onto.enum_label("business_line", x["业务线"]) if x["业务线"] else "未标注"
            others[k] = others.get(k, 0) + x["金额"]

    lead_lines = list(g.get("lead_lines") or ["online"])
    leads = conn.execute("""SELECT count(*) FILTER (WHERE props->>'business_line' = ANY(%s)) n,
                                   count(*) FILTER (WHERE props->>'business_line' IS NULL) unlabeled
                            FROM objects WHERE type='lead' AND created_at >= %s AND created_at < %s""",
                         (lead_lines, start, end)).fetchone()

    pub_at = "COALESCE((props->>'published_at')::timestamptz, created_at)"
    posts = conn.execute(f"""SELECT COALESCE(props->>'format','unset') f, count(*) n FROM objects
                             WHERE type='publish_record' AND {pub_at} >= %s GROUP BY 1""", (t["wk"],)).fetchall()
    by_fmt = {r["f"]: r["n"] for r in posts}
    live_today = conn.execute(f"""SELECT count(*) n FROM objects WHERE type='publish_record'
                                  AND props->>'format'='live_talk' AND {pub_at} >= %s""", (t["today"],)).fetchone()["n"]

    def item(actual, target, pct_time):
        d = {"实际": actual}
        if target:
            d.update({"目标": target, "完成率": round(actual / target, 3), "时间进度": round(pct_time, 3),
                      "落后": actual / target < pct_time - 0.1})
        return d

    mix = g.get("format_mix") or {}
    out = {
        "本月收入": {**item(round(rev, 2), g.get("monthly_revenue"), month_pct),
                  "统计口径": [onto.enum_label("business_line", x) for x in rev_lines]},
        "其他业务线收入": others,
        "本月线索": {**item(leads["n"], g.get("monthly_leads"), month_pct), "未标业务线": leads["unlabeled"]},
        "本周发布": {**item(sum(by_fmt.values()), g.get("weekly_posts"), week_pct),
                  "按形态": {(onto.enum_label("content_format", f) if f != "unset" else "未标形态"):
                             {"实际": n, **({"计划": mix[f]} if f in mix else {})} for f, n in by_fmt.items()}},
    }
    if g.get("daily_live_talk"):
        wk_live = by_fmt.get("live_talk", 0)
        out["真人口播"] = {"今日": live_today, "今日目标": g["daily_live_talk"], "本周": wk_live,
                       "本周目标": mix.get("live_talk", g["daily_live_talk"] * 7)}
    return out


# ── 内容快产线（S2 / ADR-0016）──────────────────────
def _piece_brief(onto, r) -> dict:
    p = r["props"]
    pkg = p.get("package") or {}
    d = {"id": r["id"], "标题": r["title"], "形态": onto.enum_label("content_format", p.get("format")),
         "平台": onto.enum_label("platform", p["platform"]) if p.get("platform") else None,
         "排期": (p.get("planned_date") or "")[:10], "状态": onto.enum_label("piece_status", p.get("status")),
         "版本": p.get("version")}
    if pkg:
        d["钩子"] = pkg.get("hook")
        d["封面"] = (pkg.get("cover") or {}).get("main")
        body = pkg.get("body") or {}
        first = (body.get("lines") or body.get("beats") or body.get("scenes") or body.get("steps")
                 or body.get("slides") or body.get("cards") or body.get("sections") or [])[:3]
        d["开头"] = ["".join(str(v) for k, v in x.items() if k in ("say", "text_layer", "narration", "say", "caption",
                                                                     "text", "heading"))[:60]
                     for x in first if isinstance(x, dict)]
        if pkg.get("changes"):
            d["改了什么"] = pkg["changes"]
    rep = p.get("check_report") or {}
    if rep.get("issues"):
        d["体检问题"] = rep["issues"]
    return d


def review_queue(conn, onto, limit: int | None = None) -> dict:
    rows = conn.execute("""SELECT id, title, props FROM objects WHERE type='content_piece' AND props->>'status'='in_review'
                           ORDER BY props->>'planned_date' NULLS LAST, created_at LIMIT %s""", (limit or 5,)).fetchall()
    cnt = {r["s"]: r["n"] for r in conn.execute(
        "SELECT props->>'status' s, count(*) n FROM objects WHERE type='content_piece' GROUP BY 1").fetchall()}
    return {"待审": [_piece_brief(onto, r) for r in rows], "待审总数": cnt.get("in_review", 0),
            "体检未过": cnt.get("check_failed", 0), "待修改": cnt.get("changes_requested", 0),
            "说明": "逐条给用户看要点；用户说过 → review_piece decision=approve；给意见 → decision=changes + feedback，再 revise_piece"}


def content_plan(conn, onto, start: str | None = None) -> dict:
    from autobiz.profile import goals
    tz, g = _tz(), goals()
    st = conn.execute("SELECT COALESCE(%s::date, (now() AT TIME ZONE %s)::date) d", (start, tz)).fetchone()["d"]
    rows = conn.execute("""SELECT id, title, props, ((props->>'planned_date')::timestamptz AT TIME ZONE %s)::date d
        FROM objects WHERE type='content_piece' AND props->>'status' <> 'dropped'
          AND ((props->>'planned_date')::timestamptz AT TIME ZONE %s)::date BETWEEN %s AND %s + 6
        ORDER BY props->>'planned_date'""", (tz, tz, st, st)).fetchall()
    days: dict[str, list] = {}
    fmt_cnt: dict[str, int] = {}
    live_by_day: dict[str, int] = {}
    for r in rows:
        k = r["d"].strftime("%m-%d")
        days.setdefault(k, []).append({"id": r["id"], "标题": r["title"],
                                       "形态": onto.enum_label("content_format", r["props"]["format"]),
                                       "状态": onto.enum_label("piece_status", r["props"].get("status"))})
        fmt_cnt[r["props"]["format"]] = fmt_cnt.get(r["props"]["format"], 0) + 1
        if r["props"]["format"] == "live_talk":
            live_by_day[k] = live_by_day.get(k, 0) + 1
    mix = g.get("format_mix") or {}
    vs = {onto.enum_label("content_format", f): {"已排": fmt_cnt.get(f, 0), "配比": mix.get(f, 0)}
          for f in dict.fromkeys([*mix, *fmt_cnt]) if mix.get(f) or fmt_cnt.get(f)}
    out = {"起始": str(st), "合计": len(rows), "目标": g.get("weekly_posts"), "按天": days, "形态对照配比": vs}
    dl = int(g.get("daily_live_talk") or 0)
    if dl:
        import datetime as _dt
        gaps = {}
        for i in range(7):
            k = (st + _dt.timedelta(days=i)).strftime("%m-%d")
            if live_by_day.get(k, 0) < dl:
                gaps[k] = f"{live_by_day.get(k, 0)}/{dl}"
        out["真人口播缺口"] = gaps or "无"
    unscheduled = conn.execute("""SELECT title FROM objects o WHERE type='topic' AND props->>'status'='scheduled'
        AND NOT EXISTS (SELECT 1 FROM links l WHERE l.link_type='piece_of' AND l.to_id=o.id)""").fetchall()
    if unscheduled:
        out["已定档未排产的选题"] = [r["title"] for r in unscheduled]
    return out


def piece_package(conn, onto, id: str) -> dict:
    o = store.get(conn, id)
    if not o or o["type"] != "content_piece":
        raise store.ActionError(f"内容件 {id} 不存在")
    p = o["props"]
    return {"id": id, "标题": o["title"], "状态": onto.enum_label("piece_status", p.get("status")),
            "版本": p.get("version"), "创作包": p.get("package_text") or "（还没起草）",
            "体检": p.get("check_report"), "审稿意见": p.get("feedback")}


# ── OPC 基地招商（私有模块，发布物不含）────────────
try:
    from autobiz.opc import occupancy, opc_pipeline  # noqa: E402,F401
except ImportError:  # pragma: no cover
    pass


# ── 数据回流（S3，实现见 feedback.py）──────────────
from autobiz.feedback import conversion_stats, format_performance, unattributed_leads  # noqa: E402,F401


# ── 客户跟进（S1）────────────────────────────────────
def followup_today(conn, onto) -> dict:
    tz = _tz()
    eod = conn.execute("SELECT (date_trunc('day', now() AT TIME ZONE %s) + interval '1 day') AT TIME ZONE %s e",
                       (tz, tz)).fetchone()["e"]
    rows = conn.execute("""
        SELECT id, type, title, props, created_at, (props->>'next_followup_at')::timestamptz nxt FROM objects
        WHERE (type='lead' AND props->>'status' IN ('new','following'))
           OR (type='organization' AND props->>'next_followup_at' IS NOT NULL)
        ORDER BY (props->>'next_followup_at')::timestamptz NULLS LAST""").fetchall()
    now = conn.execute("SELECT now() n").fetchone()["n"]
    due, stale_new = [], []
    for r in rows:
        p = r["props"]
        brief = {"id": r["id"], "类型": _label(onto, r["type"]), "名称": r["title"],
                 "业务线": onto.enum_label("business_line", p["business_line"]) if p.get("business_line") else "未标注",
                 "平台": onto.enum_label("platform", p["platform"]) if p.get("platform") else None,
                 "最近联系": (p.get("last_contact_at") or "")[:10] or None}
        if r["nxt"] and r["nxt"] < eod:
            brief["下次跟进"] = str(r["nxt"])[:10]
            brief["超期天数"] = max((now - r["nxt"]).days, 0)
            due.append(brief)
        elif r["type"] == "lead" and p.get("status") == "new" and not p.get("last_contact_at") \
                and (now - r["created_at"]).total_seconds() > 86400:
            stale_new.append(brief)
    due.sort(key=lambda x: -x["超期天数"])
    return {"今天该跟进": due, "超24小时未跟进的新线索": stale_new,
            "说明": "跟进后用 log_contact 记一笔（会自动定下次跟进日）；不再跟进用 mark_lead_lost"}


# ── 售前域 ──────────────────────────────────────────
def decision_ledger(conn, onto, customer: str) -> dict:
    org = _resolve(conn, onto, customer, "organization")
    rows = conn.execute("""SELECT o.id, o.props, o.created_at FROM links l JOIN objects o ON o.id=l.from_id
        WHERE l.link_type='decision_for' AND l.to_id=%s ORDER BY o.created_at""", (org["id"],)).fetchall()
    act = [{"id": r["id"], "口径": r["props"]["statement"], "适用": r["props"].get("scope"),
            "拍板人": r["props"].get("decided_by"), "日期": (r["props"].get("decided_at") or "")[:10],
            "禁用说法": r["props"].get("forbid") or []} for r in rows if r["props"].get("status") == "active"]
    old = [r["props"]["statement"][:40] for r in rows if r["props"].get("status") != "active"]
    return {"客户": org["title"], "有效口径": act, **({"已替代": old} if old else {})}


def presales_board(conn, onto) -> dict:
    orgs = conn.execute("""
        SELECT DISTINCT org.id, org.title FROM objects org
        WHERE org.type='organization' AND EXISTS (
          SELECT 1 FROM links l WHERE l.link_type='req_of' AND (l.to_id=org.id OR l.to_id IN (
            SELECT from_id FROM links WHERE link_type='lead_org' AND to_id=org.id)))""").fetchall()
    board = []
    for o in orgs:
        q = lambda sql: conn.execute(sql, (o["id"],)).fetchone()
        notes = q("""SELECT count(*) n, max(n.created_at) last FROM objects n JOIN links l ON l.from_id=n.id AND l.link_type='req_of'
                     WHERE l.to_id=%s""")
        ana = q("""SELECT a.id, a.title, a.props->>'status' st, a.created_at FROM links l JOIN objects a ON a.id=l.from_id
                   WHERE l.link_type='analysis_of' AND l.to_id=%s ORDER BY a.created_at DESC LIMIT 1""")
        prop = None
        if ana:
            prop = conn.execute("""SELECT p.id, p.title, p.props->>'status' st, (p.props->>'price')::numeric price, p.props->>'doc_url' url,
                     p.props->>'procurement_stage' stage, p.props->>'entity' entity
                     FROM links l JOIN objects p ON p.id=l.from_id WHERE l.link_type='proposal_for' AND l.to_id=%s
                     AND p.props->>'status' <> 'superseded' ORDER BY p.created_at DESC LIMIT 1""", (ana["id"],)).fetchone()
        proj = q("""SELECT p.title, p.props->>'status' st FROM links l JOIN objects p ON p.id=l.from_id
                    WHERE l.link_type='project_of' AND l.to_id=%s ORDER BY p.created_at DESC LIMIT 1""")
        if not ana:
            nxt = "分析需求（analyze_requirement）"
        elif notes["last"] and notes["last"] > ana["created_at"]:
            nxt = "有新的需求碎片，重新分析需求"
        elif ana["st"] == "draft":
            nxt = "审阅需求分析，确认后执行 confirm_analysis"
        elif not prop:
            nxt = "生成售前方案（draft_proposal）"
        elif prop["st"] == "draft":
            nxt = "审阅方案，发布到飞书（publish_proposal）"
        elif prop["st"] == "sent":
            nxt = "跟进客户反馈：修改方案（revise_proposal）或成交（accept_proposal）"
        elif proj:
            nxt = f"交付中：{proj['title']}"
        else:
            nxt = "—"
        board.append({"客户": o["title"], "id": o["id"], "需求碎片": notes["n"],
                      "需求分析": f"{ana['title']}（{onto.enum_label('analysis_status', ana['st'])}）" if ana else "无",
                      "方案": (f"{prop['title']}（{onto.enum_label('proposal_status', prop['st'])}，{float(prop['price'] or 0):,.0f} 元）"
                               if prop else "无"),
                      "飞书文档": prop["url"] if prop else None, "下一步": nxt,
                      **({"采购阶段": onto.enum_label("procurement_stage", prop["stage"])} if prop and prop["stage"] else {}),
                      **({"签约主体": prop["entity"]} if prop and prop["entity"] else {})})
    return {"售前看板": board}
