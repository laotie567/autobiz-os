"""S1（ADR-0015）：业务线 / 订单 / 线索跟进 / 咨询软提醒 / 驾驶舱 / 飞书简报。"""
import json

import pytest

from autobiz import functions


@pytest.fixture
def goals(monkeypatch):
    from autobiz import profile
    g = {"monthly_revenue": 50000, "revenue_lines": ["online"], "monthly_leads": 100, "lead_lines": ["online"],
         "weekly_posts": 50, "daily_live_talk": 2,
         "format_mix": {"live_talk": 14, "image_video": 8, "image_post": 8, "explainer": 6, "article": 6,
                        "motion": 4, "science": 4, "avatar_talk": 0}}
    monkeypatch.setattr(profile, "goals", lambda: g)
    return g


def _new(act, t, **p):
    r = act("create_object", type=t, props=p)
    assert r["ok"], r
    return r["result"]["id"]


# ── 线索 ──────────────────────────────────────────────
def test_quick_lead_defaults_online_and_followup(act, conn, onto):
    r = act("quick_lead", role="agent", who="开店的老周", platform="douyin", keyword="选址", text="想开奶茶店，选址怎么看")
    assert r["ok"], r                                  # direct：agent 不需预览
    o = conn.execute("SELECT props FROM objects WHERE id=%s", (r["result"]["id"],)).fetchone()["props"]
    assert o["business_line"] == "online" and o["keyword"] == "选址" and o["next_followup_at"]
    assert "【选址】" in conn.execute("SELECT title FROM objects WHERE id=%s", (r["result"]["id"],)).fetchone()["title"]


def test_lead_without_platform_is_unlabeled_with_hint(act):
    r = act("quick_lead", role="agent", who="线下朋友介绍的王总")
    assert r["ok"] and r["result"]["业务线"] == "未标注" and "提醒" in r["result"]


def test_log_contact_moves_new_to_following_and_appends(act, conn):
    lid = act("quick_lead", role="agent", who="A", platform="xiaohongshu")["result"]["id"]
    r = act("log_contact", role="agent", target=lid, note="回复了选址清单", next_in_days=2)
    assert r["ok"], r
    p = conn.execute("SELECT props FROM objects WHERE id=%s", (lid,)).fetchone()["props"]
    assert p["status"] == "following" and len(p["contact_log"]) == 1 and p["last_contact_at"]
    act("log_contact", role="agent", target=lid, note="约了周五电话")
    p = conn.execute("SELECT props FROM objects WHERE id=%s", (lid,)).fetchone()["props"]
    assert len(p["contact_log"]) == 2


def test_contact_log_is_managed(act):
    lid = act("quick_lead", role="agent", who="B", platform="douyin")["result"]["id"]
    r = act("update_object", target=lid, props={"contact_log": []})
    assert not r["ok"] and "流程行动维护" in r["error"]


def test_mark_lead_lost(act, conn):
    lid = act("quick_lead", role="agent", who="C", platform="douyin")["result"]["id"]
    assert act("mark_lead_lost", role="agent", target=lid, reason="预算太低")["ok"]
    p = conn.execute("SELECT props FROM objects WHERE id=%s", (lid,)).fetchone()["props"]
    assert p["status"] == "lost" and p["lost_reason"] == "预算太低"


def test_followup_today_lists_due_and_stale(act, conn, onto):
    due = act("quick_lead", role="agent", who="到期的", platform="douyin", next_in_days=0)["result"]["id"]
    later = act("quick_lead", role="agent", who="下周的", platform="douyin", next_in_days=7)["result"]["id"]
    stale = act("quick_lead", role="agent", who="老线索", platform="douyin", next_in_days=9)["result"]["id"]
    conn.execute("UPDATE objects SET created_at = now() - interval '2 days' WHERE id=%s", (stale,))
    conn.commit()
    f = functions.followup_today(conn, onto)
    ids = [x["id"] for x in f["今天该跟进"]]
    assert due in ids and later not in ids
    assert stale in [x["id"] for x in f["超24小时未跟进的新线索"]]


def test_convert_lead_passes_business_line_to_org(act, conn):
    lid = act("quick_lead", role="agent", who="D", platform="douyin")["result"]["id"]
    r = act("convert_lead", target=lid, org_name="甜品工作室", confirm_new=True)
    assert r["ok"], r
    org = conn.execute("SELECT props FROM objects WHERE id=%s", (r["result"]["organization"],)).fetchone()["props"]
    assert org["business_line"] == "online"


# ── 订单与收入 ──────────────────────────────────────────
def test_record_order_infers_online_for_products(act, conn):
    r = act("record_order", role="agent", title="选址 skill", amount=899, kind="product", buyer="老周")
    assert r["ok"] and r["result"]["业务线"] == "线上获客业务"


def test_record_order_rejects_nonpositive(act):
    r = act("record_order", role="agent", title="x", amount=0, kind="product")
    assert not r["ok"] and "大于 0" in r["error"]


def test_record_order_inherits_customer_line(act, conn):
    org = _new(act, "organization", name="线下老客户", business_line="offline")
    r = act("record_order", role="agent", title="定制开发首款", amount=20000, kind="custom", customer=org)
    assert r["ok"] and r["result"]["业务线"] == "线下项目"
    assert conn.execute("SELECT 1 FROM links WHERE link_type='order_of' AND to_id=%s", (org,)).fetchone()


def test_refund_order_is_gate(act, conn):
    oid = act("record_order", role="agent", title="退款单", amount=500, kind="product")["result"]["id"]
    r = act("refund_order", role="agent", target=oid, reason="不想要了")
    assert not r["ok"]                                 # agent 不能自行确认闸门


def test_revenue_splits_lines_and_counts_payments_from_log(act, conn, onto):
    act("record_order", role="agent", title="skill", amount=899, kind="product")
    act("record_order", role="agent", title="代运营服务费", amount=10000, kind="other", business_line="partner")
    org = _new(act, "organization", name="线上来的客户", business_line="online")
    proj = _new(act, "project", title="工作台定制", business_line="online")
    act("link_objects", link_type="project_of", **{"from": proj, "to": org})
    ms = _new(act, "milestone", title="阶段1", amount=8000)
    act("link_objects", link_type="milestone_of", **{"from": ms, "to": proj})
    assert act("record_payment", target=ms, amount=3000)["ok"]
    assert act("record_payment", target=ms, amount=2000)["ok"]
    r = functions.revenue(conn, onto)
    assert r["按业务线"]["线上获客业务"] == 899 + 5000
    assert r["按业务线"]["合伙业务"] == 10000
    assert r["按类型"]["项目回款"] == 5000


def test_consultation_records_verdict_and_fee_order(act, conn, onto):
    org = _new(act, "organization", name="烘焙店", business_line="online")
    r = act("record_consultation", role="agent", target=org, verdict="go", reason="老板亲自拍板、预算明确", hours=1, fee=900)
    assert r["ok"], r
    p = conn.execute("SELECT props FROM objects WHERE id=%s", (org,)).fetchone()["props"]
    assert p["consult_verdict"] == "go" and p["consulted_at"]
    assert functions.revenue(conn, onto)["按类型"]["顾问咨询"] == 900


# ── 驾驶舱 ────────────────────────────────────────────
def test_goal_progress_counts_only_target_lines(act, conn, onto, goals):
    act("record_order", role="agent", title="skill", amount=1000, kind="product")
    act("record_order", role="agent", title="线下项目款", amount=30000, kind="custom", business_line="offline")
    act("quick_lead", role="agent", who="E", platform="douyin")
    act("quick_lead", role="agent", who="F", business_line="partner")
    g = functions.goal_progress(conn, onto)
    assert g["本月收入"]["实际"] == 1000 and g["本月收入"]["目标"] == 50000
    assert g["其他业务线收入"]["线下项目"] == 30000
    assert g["本月线索"]["实际"] == 1


def test_goal_progress_posts_and_live_talk(act, conn, onto, goals):
    for i in range(3):
        tid = act("propose_topic", title=f"选题{i}")["result"]["id"]
        conn.execute("UPDATE objects SET props = props || '{\"status\":\"scheduled\"}' WHERE id=%s", (tid,))
        conn.commit()
        fmt = "live_talk" if i < 2 else "image_post"
        assert act("mark_published", target=tid, platform="douyin", format=fmt)["ok"]
    g = functions.goal_progress(conn, onto)
    assert g["本周发布"]["实际"] == 3
    assert g["真人口播"]["今日"] == 2 and g["真人口播"]["本周目标"] == 14
    assert g["本周发布"]["按形态"]["真人口播"] == {"实际": 2, "计划": 14}


def test_receivables_due_and_overdue(act, conn, onto):
    past = _new(act, "milestone", title="逾期阶段", amount=5000, due="2020-01-01T00:00:00+08:00")
    _new(act, "milestone", title="远期阶段", amount=5000, due="2099-01-01T00:00:00+08:00")
    r = functions.receivables(conn, onto)
    assert [x["id"] for x in r["已逾期"]] == [past]
    assert r["7天内到期"] == []


# ── 售前软提醒 ────────────────────────────────────────
def test_new_customer_from_douyin_requirement_is_online(act, conn):
    r = act("capture_requirement", role="agent", customer="抖音来的张老板", text="想做探店视频", channel="douyin")
    p = conn.execute("SELECT props FROM objects WHERE id=%s", (r["result"]["customer_id"],)).fetchone()["props"]
    assert p["business_line"] == "online"


def test_onto_has_s1_functions(onto):
    for f in ("goal_progress", "followup_today", "revenue"):
        assert f in onto.functions


def test_views_recompile_after_adding_property(conn, onto):
    """回归：给已有类型加属性后 init 必须能重编译视图（曾因 CREATE OR REPLACE VIEW 报错）。"""
    import copy
    from autobiz import db
    from autobiz.ontology import PropType
    o2 = copy.deepcopy(onto)
    t = o2.object_types["lead"]
    t.properties = {"aaa_new": PropType("text"), **t.properties}      # 插在中间，最容易触发列名冲突
    for ddl in db.view_ddl(o2):
        conn.execute(ddl)
    for ddl in db.view_ddl(onto):                                       # 再改回去
        conn.execute(ddl)
    conn.commit()


# ── 简报与飞书 ────────────────────────────────────────
def test_daily_brief_sections(act, conn, onto, goals):
    from autobiz import brief
    act("record_order", role="agent", title="skill", amount=899, kind="product")
    act("quick_lead", role="agent", who="老周", platform="douyin", next_in_days=0)
    text = brief.daily(conn, onto)
    assert "AutoBiz 日报" in text and "【目标】收入 899 / 50,000" in text
    assert "【真人口播】今天 0 / 2 ⚠" in text
    assert "跟进 1 条" in text and "老周" in text


def test_daily_brief_soft_reminder_for_no_consultation(act, conn, onto, goals):
    from autobiz import brief
    org = _new(act, "organization", name="没咨询的客户", business_line="online")
    aid = _new(act, "requirement_analysis", title="分析", status="draft")
    act("link_objects", link_type="analysis_of", **{"from": aid, "to": org})
    pid = _new(act, "proposal", title="方案 v1", status="draft")
    act("link_objects", link_type="proposal_for", **{"from": pid, "to": aid})
    assert "没咨询的客户" in brief.daily(conn, onto)


def test_weekly_brief_runs(act, conn, onto, goals):
    from autobiz import brief
    text = brief.weekly(conn, onto)
    assert "AutoBiz 周报" in text and "【发布】0 / 50 条" in text


def test_feishu_send_signs_payload(monkeypatch):
    import base64, hashlib, hmac
    from autobiz import brief
    sent = {}
    monkeypatch.setenv("FEISHU_BOT_WEBHOOK", "https://open.feishu.cn/open-apis/bot/v2/hook/test")
    monkeypatch.setenv("FEISHU_BOT_SECRET", "s3cret")
    monkeypatch.setattr(brief, "_post", lambda url, payload: sent.update(url=url, payload=payload) or {"code": 0})
    brief.send_feishu("你好")
    p = sent["payload"]
    assert p["msg_type"] == "text" and p["content"]["text"] == "你好"
    expect = base64.b64encode(hmac.new(f"{p['timestamp']}\ns3cret".encode(), digestmod=hashlib.sha256).digest()).decode()
    assert p["sign"] == expect


def test_feishu_send_requires_webhook(monkeypatch):
    from autobiz import brief
    monkeypatch.delenv("FEISHU_BOT_WEBHOOK", raising=False)
    with pytest.raises(SystemExit):
        brief.send_feishu("x")


def test_feishu_error_code_raises(monkeypatch):
    from autobiz import brief
    monkeypatch.setenv("FEISHU_BOT_WEBHOOK", "https://example/hook")
    monkeypatch.delenv("FEISHU_BOT_SECRET", raising=False)
    monkeypatch.setattr(brief, "_post", lambda url, payload: {"code": 19021, "msg": "sign match fail"})
    with pytest.raises(SystemExit):
        brief.send_feishu("x")
