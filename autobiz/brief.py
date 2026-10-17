"""每日简报 / 每周复盘 + 飞书群机器人推送（S1 / ADR-0015）。

只读：所有数字来自本体函数（goal_progress / followup_today / receivables / revenue / overview），
不调模型、不写库。推送用飞书「自定义机器人」Webhook（建议建一个只有自己的群），开启签名校验：
    .env  FEISHU_BOT_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/xxxx
          FEISHU_BOT_SECRET=<机器人安全设置里的签名密钥>
定时：scripts/brief.sh + scripts/schedule/ 模板（每天 08:00 日报，每周一 08:30 周报）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.request

from autobiz import functions

WEEKDAY = "一二三四五六日"


def _yuan(x) -> str:
    return f"{float(x or 0):,.0f}"


def _pct(d: dict) -> str:
    if "目标" not in d:
        return f"{d['实际']}"
    warn = " ⚠" if d.get("落后") else ""
    return f"{d['实际'] if not isinstance(d['实际'], float) else _yuan(d['实际'])} / {d['目标']:,}（{d['完成率']:.0%}｜时间 {d['时间进度']:.0%}{warn}）"


def _local_now(conn) -> tuple:
    from autobiz.profile import timezone_name
    r = conn.execute("SELECT (now() AT TIME ZONE %s) t", (timezone_name(),)).fetchone()["t"]
    return r


def daily(conn, onto) -> str:
    from autobiz.profile import timezone_name
    tz_name = timezone_name()
    now = _local_now(conn)
    g = functions.goal_progress(conn, onto)
    f = functions.followup_today(conn, onto)
    rv = functions.receivables(conn, onto)
    ov = functions.overview(conn, onto)["待办"]
    lines = [f"🗓 {now.month}月{now.day}日（周{WEEKDAY[now.weekday()]}）· AutoBiz 日报"]

    inc, ld, posts = g["本月收入"], g["本月线索"], g["本周发布"]
    lines.append(f"【目标】收入 {_pct(inc)} · 线索 {_pct(ld)} · 本周发布 {_pct(posts)}")
    if "真人口播" in g:
        lt = g["真人口播"]
        flag = " ⚠" if lt["今日"] < lt["今日目标"] else " ✓"
        lines.append(f"【真人口播】今天 {lt['今日']} / {lt['今日目标']}{flag} · 本周 {lt['本周']} / {lt['本周目标']}")
    if g["其他业务线收入"]:
        lines.append("【其他业务线·本月】" + " · ".join(f"{k} ¥{_yuan(v)}" for k, v in g["其他业务线收入"].items()))

    if hasattr(functions, "occupancy") and "space" in onto.object_types:      # OPC 私有模块
        oc = functions.occupancy(conn, onto)
        if "合计" in oc:
            t = oc["合计"]
            pipe = functions.opc_pipeline(conn, onto, days=30)
            lines.append(f"【OPC 基地】出租率 {t['出租率（按个数）']:.0%}（入驻 {t['已入驻']} / 共 {t['总数']}，预留 {t['预留']}）"
                         f" · 在驻企业 {oc['在驻企业']} · 近30天招商线索 {pipe['招商线索']}"
                         + (f" · 30天内到期 {len(oc['30天内到期入驻'])}" if oc["30天内到期入驻"] else ""))
    todo = []
    due, stale = f["今天该跟进"], f["超24小时未跟进的新线索"]
    if due or stale:
        over = [x for x in due if x.get("超期天数")]
        names = "、".join(x["名称"] for x in (over + due)[:3])
        todo.append(f"跟进 {len(due)} 条" + (f"（{len(over)} 条超期）" if over else "") + (f"：{names}" if names else "")
                    + (f"；另有 {len(stale)} 条新线索超 24 小时没回" if stale else ""))
    if rv["已逾期"]:
        todo.append("催款（已逾期）：" + "；".join(f"{x['项目'] or ''}{x['里程碑']} ¥{_yuan(x['未收'])}" for x in rv["已逾期"][:3]))
    if rv["7天内到期"]:
        todo.append("7 天内到期回款：" + "；".join(f"{x['里程碑']} {x['到期']} ¥{_yuan(x['未收'])}" for x in rv["7天内到期"][:3]))
    pc = {r["s"]: r["n"] for r in conn.execute(
        "SELECT props->>'status' s, count(*) n FROM objects WHERE type='content_piece' GROUP BY 1").fetchall()}
    if pc.get("in_review"):
        todo.append(f"待审稿件 {pc['in_review']} 条 → 对豆包说「审稿」")
    if pc.get("changes_requested"):
        todo.append(f"待改稿 {pc['changes_requested']} 条（revise_piece）")
    if pc.get("check_failed"):
        todo.append(f"体检两次未过 {pc['check_failed']} 条（看问题后重写或放弃）")
    today = conn.execute("""SELECT props->>'status' s, props->>'format' f FROM objects WHERE type='content_piece'
        AND ((props->>'planned_date')::timestamptz AT TIME ZONE %s)::date = (now() AT TIME ZONE %s)::date
        AND props->>'status' <> 'dropped'""", (tz_name, tz_name)).fetchall()
    if today:
        ready = sum(1 for r in today if r["s"] in ("approved", "produced"))
        done = sum(1 for r in today if r["s"] == "published")
        live = sum(1 for r in today if r["f"] == "live_talk")
        todo.append(f"今天排期 {len(today)} 条（其中真人口播 {live}）：已发布 {done}，待出品/待发 {ready}，"
                    f"还没过审 {len(today) - ready - done}")
    for k in ("候选选题（待定档）", "已定档待发布", "缺7天数据的发布", "待确认的需求分析", "已发送待成交的方案"):
        if ov.get(k):
            todo.append(f"{k} {ov[k]}" + ("（把创作者后台数据截图发给豆包，批量回填）" if k == "缺7天数据的发布" else ""))
    pend = conn.execute("SELECT count(*) n FROM approvals WHERE status='pending' AND expires_at > now()").fetchone()["n"]
    if pend:
        todo.append(f"待你审批的审批码 {pend} 个（终端 autobiz approve <码>）")
    lines.append("【今天必做】" + ("\n" + "\n".join(f" {i}. {t}" for i, t in enumerate(todo, 1)) if todo else "无"))

    soft = conn.execute("""SELECT DISTINCT org.title FROM objects org
        JOIN links a ON a.to_id=org.id AND a.link_type='analysis_of'
        JOIN links pf ON pf.to_id=a.from_id AND pf.link_type='proposal_for'
        JOIN objects p ON p.id=pf.from_id AND p.props->>'status' IN ('draft','sent')
        WHERE org.type='organization' AND org.props->>'consult_verdict' IS NULL LIMIT 5""").fetchall()
    tips = []
    if soft:
        tips.append("尚无咨询结论却已出方案：" + "、".join(r["title"] for r in soft) + "（软提醒）")
    if "未标注" in g["其他业务线收入"]:
        tips.append(f"本月有 ¥{_yuan(g['其他业务线收入']['未标注'])} 收入未标业务线，不计入目标（q revenue 查明细后补标）")
    if ld.get("未标业务线"):
        tips.append(f"本月 {ld['未标业务线']} 条线索未标业务线，不计入目标")
    if tips:
        lines.append("【提醒】" + "；".join(tips))

    y = conn.execute("""SELECT
        (SELECT count(*) FROM objects WHERE type='lead' AND created_at >= now() - interval '1 day') leads,
        (SELECT count(*) FROM objects WHERE type='order' AND created_at >= now() - interval '1 day') orders,
        (SELECT COALESCE(sum((props->>'amount')::numeric),0) FROM objects WHERE type='order' AND created_at >= now() - interval '1 day') order_amt,
        (SELECT count(*) FROM objects WHERE type='publish_record' AND created_at >= now() - interval '1 day') posts""").fetchone()
    try:
        cost = conn.execute("SELECT COALESCE(sum(cost_cny),0) s FROM llm_usage WHERE at >= now() - interval '1 day'").fetchone()["s"]
    except Exception:
        conn.rollback()
        cost = 0
    lines.append(f"【近24小时】新线索 {y['leads']} · 订单 {y['orders']} 笔 ¥{_yuan(y['order_amt'])} · 发布 {y['posts']} · 模型 ¥{float(cost):.2f}")
    conn.rollback()
    return "\n".join(lines)


def weekly(conn, onto) -> str:
    from autobiz.profile import goals, timezone_name
    tz, g = timezone_name(), goals()
    wk = conn.execute("""SELECT (date_trunc('week', now() AT TIME ZONE %(tz)s) - interval '7 days') AT TIME ZONE %(tz)s s,
                                 date_trunc('week', now() AT TIME ZONE %(tz)s) AT TIME ZONE %(tz)s e""", {"tz": tz}).fetchone()
    s, e = wk["s"], wk["e"]
    pub_at = "COALESCE((props->>'published_at')::timestamptz, created_at)"
    fmts = {r["f"]: r["n"] for r in conn.execute(
        f"SELECT COALESCE(props->>'format','unset') f, count(*) n FROM objects WHERE type='publish_record' "
        f"AND {pub_at} >= %s AND {pub_at} < %s GROUP BY 1", (s, e)).fetchall()}
    mix = g.get("format_mix") or {}
    total = sum(fmts.values())
    lab = conn.execute("SELECT to_char(%(s)s::timestamptz AT TIME ZONE %(tz)s, 'MM-DD') a, "
                       "to_char((%(e)s::timestamptz - interval '1 day') AT TIME ZONE %(tz)s, 'MM-DD') b",
                       {"s": s, "e": e, "tz": tz}).fetchone()
    lines = [f"📊 AutoBiz 周报（{lab['a']} ~ {lab['b']}）"]
    target = g.get("weekly_posts")
    lines.append(f"【发布】{total}" + (f" / {target}" if target else "") + " 条")
    keys = list(dict.fromkeys([*mix.keys(), *fmts.keys()]))
    if keys:
        lines.append("  " + " · ".join(
            f"{onto.enum_label('content_format', k) if k != 'unset' else '未标形态'} {fmts.get(k, 0)}"
            + (f"/{mix[k]}" if mix.get(k) else "") for k in keys if fmts.get(k) or mix.get(k)))

    leads = conn.execute("""SELECT COALESCE(props->>'platform','—') p, count(*) n FROM objects
                            WHERE type='lead' AND created_at >= %s AND created_at < %s GROUP BY 1""", (s, e)).fetchall()
    lines.append(f"【线索】{sum(r['n'] for r in leads)} 条" +
                 ("：" + " · ".join(f"{onto.enum_label('platform', r['p']) if r['p'] != '—' else '无平台'} {r['n']}" for r in leads) if leads else ""))
    by_fmt = conn.execute(f"""SELECT COALESCE(pr.props->>'format','unset') f, count(DISTINCT l.from_id) n
        FROM links l JOIN objects pr ON pr.id=l.to_id JOIN objects ld ON ld.id=l.from_id
        WHERE l.link_type='lead_from' AND ld.created_at >= %s AND ld.created_at < %s GROUP BY 1""", (s, e)).fetchall()
    if by_fmt:
        lines.append("  按带来线索的内容形态：" + " · ".join(
            f"{onto.enum_label('content_format', r['f']) if r['f'] != 'unset' else '未标形态'} {r['n']}" for r in by_fmt))

    rows = functions._revenue_rows(conn, s, e)
    by_line: dict[str, float] = {}
    for x in rows:
        k = onto.enum_label("business_line", x["业务线"]) if x["业务线"] else "未标注"
        by_line[k] = by_line.get(k, 0) + x["金额"]
    lines.append("【收入】" + (" · ".join(f"{k} ¥{_yuan(v)}" for k, v in by_line.items()) if by_line else "0"))

    top = conn.execute(f"""SELECT title, props->>'platform' p, COALESCE((props->>'views_7d')::int, (props->>'views_72h')::int,
                              (props->>'views_24h')::int, 0) v,
                              COALESCE((props->>'leads_dm')::int,0)+COALESCE((props->>'leads_contact')::int,0) ld
                           FROM objects WHERE type='publish_record' AND {pub_at} >= %s AND {pub_at} < %s
                           ORDER BY ld DESC, v DESC LIMIT 3""", (s, e)).fetchall()
    if top:
        lines.append("【表现最好】" + "；".join(f"{r['title'][:18]}（播放 {r['v']:,}·线索 {r['ld']}）" for r in top))
    try:
        cost = conn.execute("SELECT COALESCE(sum(cost_cny),0) c FROM llm_usage WHERE at >= %s AND at < %s", (s, e)).fetchone()["c"]
        lines.append(f"【模型花费】¥{float(cost):.2f}")
    except Exception:
        conn.rollback()
    fp = functions.format_performance(conn, onto, days=30)
    if fp["按形态"]:
        lines.append("【近30天形态线索率】" + " · ".join(
            f"{k} {v['线索']}/{v['发布']}" for k, v in list(fp["按形态"].items())[:6]))
    if fp["按关键词"]:
        lines.append("【关键词带客】" + " · ".join(f"【{k}】{v['线索']}" for k, v in list(fp["按关键词"].items())[:5]))
    lines.append("【配比建议】" + fp["配比建议"])
    cs = functions.conversion_stats(conn, onto, days=30)
    if "实测转化率" in cs:
        r = cs["实测转化率"]
        lines.append(f"【实测转化·近30天】线索 {cs['线索']} → 咨询 {r['线索→咨询']:.0%} → 成交 {r['线索→成交']:.0%}"
                     f" · 每条线索平均成交额 ¥{r['每条线索平均成交额']:,.0f}")
    un = functions.unattributed_leads(conn, onto, days=7)["待认领"]
    if un:
        lines.append(f"【待认领线索】{len(un)} 条不知道是哪条内容带来的 → 对豆包说「认领线索」")
    gp = functions.goal_progress(conn, onto)
    lines.append(f"【本月进度】收入 {_pct(gp['本月收入'])} · 线索 {_pct(gp['本月线索'])}")
    conn.rollback()
    return "\n".join(lines)


# ── 飞书推送 ─────────────────────────────────────────
def feishu_sign(timestamp: str, secret: str) -> str:
    """飞书自定义机器人签名：以「timestamp + \\n + 密钥」为 HMAC-SHA256 的 key，对空串签名后 base64。"""
    key = f"{timestamp}\n{secret}".encode("utf-8")
    return base64.b64encode(hmac.new(key, digestmod=hashlib.sha256).digest()).decode()


def _post(url: str, payload: dict) -> dict:
    req = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


def send_feishu(text: str) -> dict:
    url = os.environ.get("FEISHU_BOT_WEBHOOK", "").strip()
    if not url:
        raise SystemExit("未配置 FEISHU_BOT_WEBHOOK：在飞书单人群添加「自定义机器人」，把 Webhook 地址和签名密钥写进 .env")
    payload: dict = {"msg_type": "text", "content": {"text": text}}
    secret = os.environ.get("FEISHU_BOT_SECRET", "").strip()
    if secret:
        ts = str(int(time.time()))
        payload.update({"timestamp": ts, "sign": feishu_sign(ts, secret)})
    resp = _post(url, payload)
    code = resp.get("code", resp.get("StatusCode", 0))
    if code not in (0, None):
        raise SystemExit(f"飞书推送失败：{resp}")
    return resp
