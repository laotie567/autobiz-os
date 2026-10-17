"""S4（ADR-0018）线下项目：客户拍板口径（硬约束 + 禁用说法）、人天 / 总价计价、签约主体、采购阶段。"""
import json
import re

import pytest

from autobiz import functions, llm


@pytest.fixture
def fake_proposal():
    calls = []
    state = {"say": "本平台满足信创要求"}

    def fn(task, system, user):
        calls.append((task, user))
        if task == "analyze_requirement":
            return json.dumps({"title": "智算平台", "business_goal": "g", "pains_summary": "p", "open_questions": ["q"],
                               "fit_signals": {"positive": [], "risk": [], "to_verify": []},
                               "scope_options": [{"name": "s", "products": []}], "pain_refs": [], "ref_ids": []})
        if task == "proposal":
            summary = state["say"] if len([c for c in calls if c[0] == "proposal"]) == 1 else "按口径改写后的摘要"
            return json.dumps({"title": "私有化平台建设方案", "summary": summary, "background": "b", "goals": ["g"],
                               "phases": [{"name": "建设期", "weeks": 12, "what": "w", "deliverables": ["d"], "pct": 40},
                                          {"name": "迭代期", "weeks": 16, "what": "w", "deliverables": ["d"], "pct": 60}],
                               "final_deliverables": [], "client_duties": [], "risks": [], "cited": [],
                               "payment_terms_suggestion": "按阶段", "architecture": None}, ensure_ascii=False)
        raise AssertionError(task)
    llm.set_fake(fn)
    yield calls, state
    llm.set_fake(None)


def _confirmed_analysis(act, conn, name="某区国有平台公司"):
    r = act("capture_requirement", customer=name, text="要做私有化大模型平台", channel="meeting")
    org = r["result"]["customer_id"]
    a = act("analyze_requirement", target=org)
    assert a["ok"], a
    assert act("confirm_analysis", target=a["result"]["id"])["ok"]
    return org, a["result"]["id"]


def test_new_gov_customer_from_meeting_is_offline_line(act, conn, fake_proposal):
    org, _ = _confirmed_analysis(act, conn)
    assert conn.execute("SELECT props->>'business_line' l FROM objects WHERE id=%s", (org,)).fetchone()["l"] == "offline"


def test_decision_is_injected_and_forbid_triggers_rewrite(act, conn, onto, fake_proposal):
    calls, _ = fake_proposal
    org, aid = _confirmed_analysis(act, conn)
    r = act("record_decision", role="agent", customer=org, statement="49.2 万是一期建设成本预算，不是营收",
            scope="财务", decided_by="客户分管领导", forbid=["满足信创"])
    assert r["ok"], r
    p = act("draft_proposal", target=aid, pricing="man_day",
            man_days=[{"role": "开发", "days": 200, "rate": 1500}, {"role": "实施", "days": 128, "rate": 1500}],
            entity="某某科技有限公司", procurement_stage="initiation")
    assert p["ok"], p
    prop_calls = [u for t, u in calls if t == "proposal"]
    assert "49.2 万是一期建设成本预算" in prop_calls[0] and "禁止出现的说法：满足信创" in prop_calls[0]
    assert len(prop_calls) == 2                                             # 第一稿含禁用说法 → 校验不合格重写
    props = conn.execute("SELECT props FROM objects WHERE id=%s", (p["result"]["id"],)).fetchone()["props"]
    assert props["price"] == 492000 and props["pricing"] == "man_day"
    assert "开发：200 人天 × 1,500 元 = 300,000 元" in props["body"] and "—— 某某科技有限公司" in props["body"]
    assert "满足信创" not in props["body"]
    led = functions.decision_ledger(conn, onto, customer=org)
    assert led["有效口径"][0]["禁用说法"] == ["满足信创"]


def test_decision_replace_supersedes_old(act, conn, onto, fake_proposal):
    org, _ = _confirmed_analysis(act, conn)
    d1 = act("record_decision", role="agent", customer=org, statement="技术分三期")["result"]["id"]
    act("record_decision", role="agent", customer=org, statement="技术三期 ≠ 财务三年，两条时间线分开", replaces=d1)
    led = functions.decision_ledger(conn, onto, customer=org)
    assert [x["口径"] for x in led["有效口径"]] == ["技术三期 ≠ 财务三年，两条时间线分开"] and led["已替代"]


def test_man_day_pricing_validated_before_model_call(act, conn, fake_proposal):
    calls, _ = fake_proposal
    _, aid = _confirmed_analysis(act, conn)
    n = len(calls)
    r = act("draft_proposal", target=aid, pricing="man_day", man_days=[{"role": "开发", "days": 0, "rate": 1500}])
    assert not r["ok"] and "大于 0" in r["error"] and len(calls) == n          # 没白调模型


def test_custom_total_and_stage_and_board(act, conn, onto, fake_proposal):
    calls, state = fake_proposal
    state["say"] = "正常摘要"
    org, aid = _confirmed_analysis(act, conn)
    p = act("draft_proposal", target=aid, pricing="custom", total=880000)
    assert p["ok"] and p["result"]["price"] == 880000
    assert act("set_procurement_stage", role="agent", target=p["result"]["id"], stage="feasibility")["ok"]
    board = functions.presales_board(conn, onto)["售前看板"][0]
    assert board["采购阶段"] == "可研"
    rv = act("revise_proposal", target=p["result"]["id"], feedback="阶段名改一下")
    assert rv["ok"], rv
    new = conn.execute("SELECT props FROM objects WHERE id=%s", (rv["result"]["id"],)).fetchone()["props"]
    assert new["price"] == 880000 and new["pricing"] == "custom" and new["procurement_stage"] == "feasibility"


def test_product_sum_still_requires_products(act, conn, fake_proposal):
    _, aid = _confirmed_analysis(act, conn)
    r = act("draft_proposal", target=aid)
    assert not r["ok"] and "pricing=man_day" in r["error"]


def test_org_type_has_soe_and_gov(onto):
    assert {"soe", "gov"} <= set(onto.enums["org_type"])
