"""售前链路：丢需求 → 分析 → 确认 → 方案 → 成交转项目；需求脱敏转选题。模型用假实现。"""
import json
import re

import pytest

from autobiz import functions, llm


def codes(user, prefix):
    return re.findall(rf"\[({prefix}\d+)\]", user)


@pytest.fixture
def fake_llm():
    calls = []

    def fn(task, system, user):
        calls.append((task, user))
        if task == "analyze_requirement":
            return json.dumps({
                "title": "张三女装-门店引流", "business_goal": "提升到店客流", "current_state": "靠老客",
                "pains_summary": "不会拍短视频", "constraints": "预算小", "open_questions": ["每月预算多少？"],
                "fit_signals": {"positive": ["老板亲自拍板"], "risk": ["预算不明"], "to_verify": []},
                "scope_options": [{"name": "探店视频试点", "what": "先做 4 条", "deliverables": "视频脚本",
                                   "products": codes(user, "S")[:1]}],
                "pain_refs": codes(user, "P")[:1], "ref_ids": codes(user, "C")[:2]}, ensure_ascii=False)
        if task == "proposal":
            return json.dumps({
                "title": "张三女装门店引流方案", "summary": "三句话", "background": "背景", "goals": ["客流 +20%"],
                "phases": [{"name": "试点", "weeks": 2, "what": "做 4 条", "deliverables": ["脚本库"], "pct": 40},
                           {"name": "推广", "weeks": 4, "what": "矩阵", "deliverables": ["培训"], "pct": 60}],
                "final_deliverables": ["知识库"], "client_duties": ["指定负责人"], "risks": ["老板没时间"],
                "cited": codes(user, "C"), "payment_terms_suggestion": "首付 50%", "architecture": None}, ensure_ascii=False)
        if task == "desensitize_topic":
            return json.dumps(fn.topic_outputs.pop(0), ensure_ascii=False)
        raise AssertionError(task)
    fn.topic_outputs = []
    llm.set_fake(fn)
    yield fn, calls
    llm.set_fake(None)


def seed(act):
    new = lambda t, **p: act("create_object", type=t, props=p)["result"]["id"]
    ind = new("industry", name="实体门店", stance="target")
    pain = new("pain_point", title="门店不会拍短视频引流", audience="customer")
    act("link_objects", link_type="pain_in", **{"from": pain, "to": ind})
    new("service_product", name="AI 探店视频生成 skill", product_type="skill", price=1000, funnel_step=3)
    new("case", title="自有：某火锅店探店视频", case_origin="own")                     # 人工录入 → 已确认
    return ind


def test_full_presales_chain(act, conn, onto, fake_llm):
    from autobiz import store
    from autobiz.store import Ctx
    seed(act)
    ai_case = store.create(Ctx(conn, onto), "case", {"title": "AI抽取：某奶茶店探店"}, origin="ai")   # 未确认
    conn.commit()

    r = act("capture_requirement", customer="张三女装", text="想用AI拍探店视频，店里没人会剪", channel="wechat")
    assert r["ok"] and r["result"]["new_customer"]
    org = r["result"]["customer_id"]
    act("capture_requirement", customer="张三女装", text="预算不多，先试试", channel="offline")

    a = act("analyze_requirement", target=org)
    assert a["ok"], a
    assert a["result"]["notes_used"] == 2
    aid = a["result"]["id"]
    assert not act("draft_proposal", target=aid, products=["AI 探店视频生成 skill"])["ok"]   # 未确认分析不能出方案
    assert act("confirm_analysis", target=aid)["ok"]
    act("link_objects", link_type="analysis_refs", **{"from": aid, "to": ai_case})    # 分析引用了一条未确认案例

    p = act("draft_proposal", target=aid, products=["AI 探店视频生成 skill"])
    assert p["ok"], p
    res = p["result"]
    assert res["price"] == 1000
    assert "AI抽取：某奶茶店探店" not in res["cited"]                    # 未确认知识没有交给模型
    assert ai_case in [x["id"] for x in res["相关但未确认的知识"]]       # 但会列出来，方便一键确认（用到时再确认）
    assert "咨询提醒" in res                                             # S1：没做顾问咨询 → 软提醒，但不拦截
    body = conn.execute("SELECT props FROM objects WHERE id=%s", (res["id"],)).fetchone()["props"]["body"]
    assert "400 元" in body and "600 元" in body                          # 阶段金额由代码按比例算
    assert "确认付款节点。成交时里程碑金额按上面各阶段比例生成" in conn.execute(
        "SELECT props->>'payment_terms' t FROM objects WHERE id=%s", (res["id"],)).fetchone()["t"]   # 称呼由 profile 注入（D-1）

    acc = act("accept_proposal", target=res["id"])
    assert acc["ok"], acc
    proj = acc["result"]["project"]
    assert conn.execute("SELECT props->>'business_line' l FROM objects WHERE id=%s", (proj,)).fetchone()["l"] == "online"  # S1：继承客户（微信渠道 → 线上）
    amounts = sorted(float(r["props"]["amount"]) for r in conn.execute(
        "SELECT m.props FROM links l JOIN objects m ON m.id=l.from_id WHERE l.link_type='milestone_of' AND l.to_id=%s", (proj,)))
    assert amounts == [400, 600]                                          # 付款节点 = 成交价 × 阶段比例
    board = functions.presales_board(conn, onto)["售前看板"][0]
    assert board["客户"] == "张三女装" and "已接受" in board["方案"]


def test_requirement_to_topic_blocks_leaks_and_preview_equals_confirm(act, conn, fake_llm):
    fn, calls = fake_llm
    seed(act)
    org = act("capture_requirement", customer="张三女装", text="想拍探店视频")["result"]["customer_id"]
    aid = act("analyze_requirement", target=org)["result"]["id"]
    fn.topic_outputs = [{"title": "张三女装靠AI翻身", "angle": "x"},                    # 泄露客户名 → 重试
                        {"title": "女装店老板不会拍视频怎么办", "angle": "三步"}]
    prev = act("requirement_to_topic", target=aid, confirm=False)
    assert prev["ok"] and prev["status"] == "preview"
    assert prev["result"]["title"] == "女装店老板不会拍视频怎么办"
    n_calls = len(calls)
    done = act("requirement_to_topic", target=aid)
    assert done["result"]["title"] == prev["result"]["title"]          # 预览即所得
    assert len(calls) == n_calls                                        # 确认时没有重新调用模型


def test_publish_preview_has_no_side_effects(act, conn, fake_llm, monkeypatch):
    import subprocess
    seed(act)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("预览不应调用 lark-cli")))
    org = act("capture_requirement", customer="张三女装", text="想拍探店视频")["result"]["customer_id"]
    aid = act("analyze_requirement", target=org)["result"]["id"]
    act("confirm_analysis", target=aid)
    pid = act("draft_proposal", target=aid, products=["AI 探店视频生成 skill"])["result"]["id"]
    r = act("publish_proposal", target=pid, confirm=False)
    assert r["ok"] and "将创建" in r["result"]


def test_internal_codes_are_replaced_with_names():
    from autobiz.presales import Refs
    r = Refs()
    r.add({"id": "p1", "type": "pain_point", "title": "客服重复咨询"}, "x")
    assert r.humanize("占用人力（P1），见 [P1]，P10 不动") == "占用人力「客服重复咨询」，见 「客服重复咨询」，P10 不动"


def test_negotiable_products_show_reference_price_not_quote(act, conn, fake_llm):
    seed(act)
    act("create_object", type="service_product", props={"name": "FDE 驻场完整交付", "product_type": "fde", "price": 50000})
    org = act("capture_requirement", customer="张三女装", text="全面 AI 化")["result"]["customer_id"]
    aid = act("analyze_requirement", target=org)["result"]["id"]
    act("confirm_analysis", target=aid)
    pid = act("draft_proposal", target=aid, products=["FDE 驻场完整交付"])["result"]["id"]
    body = conn.execute("SELECT props->>'body' b FROM objects WHERE id=%s", (pid,)).fetchone()["b"]
    assert "参考起步价：50,000 元起" in body and "一事一议" in body and "方案总价" not in body
