"""数据回流（S3 / ADR-0017）：批量回填发布数据、线索按关键词归因到内容、形态线索率与实测转化。

原则：匹配不唯一就不写（列候选让人认领），宁可少归因也不错归因；转化率只用实测数据，满 30 天才给结论。
"""
from __future__ import annotations

import re

from autobiz import store
from autobiz.store import ActionError, Ctx

ATTRIBUTION_WINDOW_DAYS = 14
MIN_DAYS_FOR_ADVICE = 28


def _norm(s: str) -> str:
    return re.sub(r"[\s\W_]+", "", str(s or "")).lower()


# ── 线索归因 ─────────────────────────────────────────
def auto_attribute(ctx: Ctx, lead_id: str, keyword: str | None, platform: str | None) -> dict | None:
    """线索带私信关键词时，归到近 14 天内带同一关键词的发布：同平台优先，再取最近发布的一条。"""
    kw = _norm(keyword)
    if not kw:
        return None
    rows = ctx.conn.execute("""
        SELECT id, title, props FROM objects WHERE type='publish_record' AND props->>'keyword' IS NOT NULL
          AND COALESCE((props->>'published_at')::timestamptz, created_at) > now() - %s * interval '1 day'
          AND COALESCE((props->>'published_at')::timestamptz, created_at) <= now()
        ORDER BY COALESCE((props->>'published_at')::timestamptz, created_at) DESC""", (ATTRIBUTION_WINDOW_DAYS,)).fetchall()
    hits = [r for r in rows if _norm(r["props"].get("keyword")) == kw]
    if not hits:
        return None
    best = sorted(hits, key=lambda r: r["props"].get("platform") != platform)[0]   # 稳定排序：同平台优先，其次最近
    store.link(ctx, "lead_from", lead_id, best["id"])
    store.update(ctx, lead_id, {"attribution": "keyword_auto"})
    return {"发布": best["title"], "id": best["id"], "候选数": len(hits)}


def attribute_lead(ctx: Ctx, lead, p):
    rec = store.require(ctx.conn, p["publish"], ["publish_record"])
    for r in ctx.conn.execute("SELECT to_id FROM links WHERE link_type='lead_from' AND from_id=%s", (lead["id"],)).fetchall():
        store.unlink(ctx, "lead_from", lead["id"], r["to_id"])
    store.link(ctx, "lead_from", lead["id"], rec["id"])
    store.update(ctx, lead["id"], {"attribution": "manual"})
    return {"线索": lead["title"], "来源内容": rec["title"]}


# ── 批量回填 ─────────────────────────────────────────
_METRIC_KEYS = ("views", "likes", "comments", "favorites", "new_followers", "completion_rate", "leads_dm", "leads_contact")


def _match_record(conn, m: str, platform: str | None) -> tuple[dict | None, list[str]]:
    m = str(m or "").strip()
    if not m:
        return None, []
    if m.startswith("publish_record_"):
        r = store.get(conn, m)
        return (r, []) if r and r["type"] == "publish_record" else (None, [])
    if m.startswith("http"):
        rows = conn.execute("SELECT * FROM objects WHERE type='publish_record' AND props->>'url'=%s", (m,)).fetchall()
        return (rows[0], []) if len(rows) == 1 else (None, [r["title"] for r in rows])
    rows = conn.execute("""SELECT o.*, similarity(o.title, %s) sim FROM objects o WHERE type='publish_record'
                           AND COALESCE((props->>'published_at')::timestamptz, created_at) > now() - interval '60 days'
                           AND (%s::text IS NULL OR props->>'platform' = %s)""", (m, platform, platform)).fetchall()
    key = _norm(m)
    exact = [r for r in rows if _norm(r["title"]) == key]
    if len(exact) == 1:
        return exact[0], []
    contain = [r for r in rows if key and (key in _norm(r["title"]) or _norm(r["title"]) in key)]
    if len(contain) == 1:
        return contain[0], []
    pool = exact or contain or sorted((r for r in rows if r["sim"] >= 0.3), key=lambda r: -r["sim"])
    if len(pool) == 1 or (len(pool) > 1 and not exact and not contain and pool[0]["sim"] - pool[1]["sim"] >= 0.15):
        return pool[0], []
    return None, [f"{r['title']}（{r['id']}）" for r in pool[:4]]


def backfill_batch(ctx: Ctx, _t, p):
    from autobiz.handlers import backfill_metrics
    items = p["items"]
    if not isinstance(items, list) or not items:
        raise ActionError("items 应为非空数组：[{match: 标题/链接/发布记录id, views: …}]")
    done, unmatched = [], []
    for it in items:
        if not isinstance(it, dict):
            raise ActionError("items 每一项都应是对象")
        rec, cands = _match_record(ctx.conn, it.get("match") or it.get("title") or it.get("url"), it.get("platform"))
        label = it.get("match") or it.get("title") or it.get("url")
        if not rec:
            unmatched.append({"条目": label, "候选": cands or "无"})
            continue
        rec = store.require(ctx.conn, rec["id"], lock=True)
        params = {k: it[k] for k in _METRIC_KEYS if it.get(k) is not None}
        params["stage"] = str(it.get("stage") or p.get("stage") or "7d")
        try:
            backfill_metrics(ctx, rec, params)
        except ActionError as e:
            unmatched.append({"条目": label, "问题": str(e)})
            continue
        done.append(f"{rec['title']}（{params['stage']}）")
    out = {"已回填": done, "未写入": unmatched}
    if unmatched:
        out["下一步"] = "未写入的条目：确认是哪条后用发布记录 id 作为 match 重新回填"
    return out


# ── 查询 ─────────────────────────────────────────────
def _leads_of_record_sql(alias: str = "pr") -> str:
    return (f"GREATEST(COALESCE(({alias}.props->>'leads_dm')::int,0) + COALESCE(({alias}.props->>'leads_contact')::int,0),"
            f" (SELECT count(*) FROM links WHERE link_type='lead_from' AND to_id={alias}.id))")


def format_performance(conn, onto, days: int | None = None) -> dict:
    from autobiz.profile import goals
    days = int(days or 30)
    pub = "COALESCE((pr.props->>'published_at')::timestamptz, pr.created_at)"
    rows = conn.execute(f"""
        SELECT COALESCE(pr.props->>'format','unset') f, pr.props->>'keyword' kw,
               COALESCE((pr.props->>'views_7d')::int, (pr.props->>'views_72h')::int, (pr.props->>'views_24h')::int) v,
               {_leads_of_record_sql()} ld, {pub} at
        FROM objects pr WHERE pr.type='publish_record' AND {pub} > now() - %s * interval '1 day'""", (days,)).fetchall()
    by_f: dict[str, dict] = {}
    by_kw: dict[str, dict] = {}
    for r in rows:
        g = by_f.setdefault(r["f"], {"发布": 0, "线索": 0, "_v": [], })
        g["发布"] += 1
        g["线索"] += r["ld"]
        if r["v"] is not None:
            g["_v"].append(r["v"])
        if r["kw"]:
            k = by_kw.setdefault(r["kw"], {"发布": 0, "线索": 0})
            k["发布"] += 1
            k["线索"] += r["ld"]
    table = {}
    for f, g in sorted(by_f.items(), key=lambda x: -(x[1]["线索"] / x[1]["发布"])):
        table[onto.enum_label("content_format", f) if f != "unset" else "未标形态"] = {
            "发布": g["发布"], "线索": g["线索"], "每条线索": round(g["线索"] / g["发布"], 2),
            "平均播放": round(sum(g["_v"]) / len(g["_v"])) if g["_v"] else None}
    first = conn.execute("SELECT min(COALESCE((props->>'published_at')::timestamptz, created_at)) m FROM objects "
                         "WHERE type='publish_record'").fetchone()["m"]
    now = conn.execute("SELECT now() n").fetchone()["n"]
    span = (now - first).days if first else 0
    out = {"窗口天数": days, "按形态": table,
           "按关键词": {k: {**v, "每条线索": round(v["线索"] / v["发布"], 2)} for k, v in
                     sorted(by_kw.items(), key=lambda x: -x[1]["线索"])}}
    mix = (goals().get("format_mix") or {})
    rates = {f: g["线索"] / g["发布"] for f, g in by_f.items() if f in mix and g["发布"] >= 3 and f != "live_talk"}
    if span < MIN_DAYS_FOR_ADVICE or len(rates) < 2:
        out["配比建议"] = f"数据不足（已有 {span} 天、{len(rates)} 种形态各满 3 条）——满 {MIN_DAYS_FOR_ADVICE} 天且至少 2 种形态各发满 3 条后给建议"
        return out
    hi, lo = max(rates, key=rates.get), min(rates, key=rates.get)
    if hi == lo or rates[hi] - rates[lo] < 0.2 or mix.get(lo, 0) < 3:
        out["配比建议"] = "各形态线索率差距不大，维持当前配比"
    else:
        lab = lambda f: onto.enum_label("content_format", f)
        out["配比建议"] = (f"建议下周把「{lab(lo)}」减 2 条（{mix[lo]}→{mix[lo] - 2}）、「{lab(hi)}」加 2 条"
                       f"（{mix.get(hi, 0)}→{mix.get(hi, 0) + 2}）：近 {days} 天每条线索 {rates[lo]:.2f} vs {rates[hi]:.2f}。"
                       "真人口播保持每日硬指标不动。确认后改 profile.yaml 的 goals.format_mix")
    return out


def unattributed_leads(conn, onto, days: int | None = None) -> dict:
    days = int(days or 14)
    rows = conn.execute("""SELECT id, title, props, created_at FROM objects l WHERE type='lead'
        AND created_at > now() - %s * interval '1 day'
        AND NOT EXISTS (SELECT 1 FROM links WHERE link_type='lead_from' AND from_id=l.id)
        ORDER BY created_at DESC""", (days,)).fetchall()
    out = []
    for r in rows:
        p = r["props"]
        cands = conn.execute("""SELECT id, title FROM objects WHERE type='publish_record'
            AND (%s::text IS NULL OR props->>'platform' = %s)
            AND COALESCE((props->>'published_at')::timestamptz, created_at) BETWEEN %s - interval '14 days' AND %s
            ORDER BY COALESCE((props->>'published_at')::timestamptz, created_at) DESC LIMIT 3""",
                             (p.get("platform"), p.get("platform"), r["created_at"], r["created_at"])).fetchall()
        out.append({"id": r["id"], "线索": r["title"], "关键词": p.get("keyword"),
                    "平台": onto.enum_label("platform", p["platform"]) if p.get("platform") else None,
                    "可能来自": [{"id": c["id"], "发布": c["title"]} for c in cands]})
    return {"待认领": out, "说明": "问用户这条线索是看了哪条内容来的，用 attribute_lead 认领（publish 填发布记录 id）"}


def conversion_stats(conn, onto, days: int | None = None) -> dict:
    from autobiz.profile import goals
    days = int(days or 30)
    lines = list(goals().get("lead_lines") or ["online"])
    leads = conn.execute("""SELECT l.id, l.props->>'status' st, l.created_at,
          (SELECT o.id FROM links k JOIN objects o ON o.id=k.to_id WHERE k.link_type='lead_org' AND k.from_id=l.id LIMIT 1) org
        FROM objects l WHERE l.type='lead' AND l.created_at > now() - %s * interval '1 day'
          AND (l.props->>'business_line' = ANY(%s) OR l.props->>'business_line' IS NULL)""", (days, lines)).fetchall()
    n = len(leads)
    st = {}
    consulted = won = 0
    amount = 0.0
    for r in leads:
        st[r["st"]] = st.get(r["st"], 0) + 1
        ids = [r["id"]] + ([r["org"]] if r["org"] else [])
        if r["org"] and conn.execute("SELECT 1 FROM objects WHERE id=%s AND props->>'consult_verdict' IS NOT NULL",
                                     (r["org"],)).fetchone():
            consulted += 1
        amt = conn.execute("""SELECT COALESCE(sum((o.props->>'amount')::numeric),0) s FROM links k JOIN objects o ON o.id=k.from_id
                              WHERE k.link_type='order_of' AND k.to_id = ANY(%s) AND o.props->>'status'='paid'""", (ids,)).fetchone()["s"]
        proj = conn.execute("SELECT 1 FROM links WHERE link_type='project_of' AND to_id=%s", (r["org"],)).fetchone() if r["org"] else None
        if float(amt) > 0 or proj:
            won += 1
            amount += float(amt)
    first = conn.execute("SELECT min(created_at) m FROM objects WHERE type='lead'").fetchone()["m"]
    now = conn.execute("SELECT now() n").fetchone()["n"]
    span = (now - first).days if first else 0
    out = {"窗口天数": days, "线索": n, "状态": {onto.enum_label("lead_status", k): v for k, v in st.items() if k},
           "做过咨询": consulted, "成交": won, "订单金额": amount}
    if span >= MIN_DAYS_FOR_ADVICE and n:
        out["实测转化率"] = {"线索→咨询": round(consulted / n, 3), "线索→成交": round(won / n, 3),
                         "每条线索平均成交额": round(amount / n, 1)}
    else:
        out["说明"] = f"线索数据才 {span} 天，满 {MIN_DAYS_FOR_ADVICE} 天后给出实测转化率"
    return out
