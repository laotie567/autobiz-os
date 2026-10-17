"""端到端：知识 → 选题 → 发布 → 线索 → 客户 → 需求 → 方案 → 成交 → 收款 → 结项 → 案例回到行业地图。"""
import json
import re

from autobiz import functions, llm


def _fake_presales(task, system, user):
    codes = lambda p: re.findall(rf"\[({p}\d+)\]", user)
    if task == "analyze_requirement":
        return json.dumps({"title": "张三女装-选址", "business_goal": "开第二家店", "current_state": "凭感觉选址",
                           "pains_summary": "选址没依据", "constraints": "预算小", "open_questions": ["预算多少？"],
                           "fit_signals": {"positive": ["老板拍板"], "risk": [], "to_verify": []},
                           "scope_options": [{"name": "选址诊断", "what": "三轮筛选", "deliverables": "报告",
                                              "products": codes("S")[:1]}],
                           "pain_refs": codes("P")[:1], "ref_ids": []}, ensure_ascii=False)
    return json.dumps({"title": "张三女装选址诊断", "summary": "s", "background": "b", "goals": ["g"],
                       "phases": [{"name": "数据调研", "weeks": 1, "what": "w", "deliverables": ["数据表"], "pct": 50},
                                  {"name": "诊断报告", "weeks": 1, "what": "w", "deliverables": ["报告"], "pct": 50}],
                       "final_deliverables": [], "client_duties": [], "risks": [], "cited": [],
                       "payment_terms_suggestion": "各 50%", "architecture": None}, ensure_ascii=False)


def test_full_flywheel(act, conn, onto):
    new = lambda type_, **props: act("create_object", type=type_, props=props)["result"]["id"]
    ind = new("industry", name="实体门店", stance="target")
    pain = new("pain_point", title="开店选址", willingness_to_pay="high", audience="customer")
    act("link_objects", link_type="pain_in", **{"from": pain, "to": ind})
    tool = new("tool", name="高德开放平台", mastery="studied")
    new("service_product", name="选址诊断", product_type="consulting", price=3800)

    # 知识 → 内容
    topic = act("propose_topic", title="开店选址三步排雷", sources=[pain])["result"]["id"]
    assert act("finalize_topic", target=topic)["ok"]
    pub = act("mark_published", target=topic, platform="抖音", account="小武总企业AI陪跑")["result"]["created"][0]
    assert act("backfill_metrics", target=pub, stage="7天", views=12000, likes=300, leads_dm=5)["ok"]

    # 内容 → 客户 → 需求 → 方案 → 成交
    lead = act("create_lead", title="抖音-女装店张三", platform="douyin", source_publish=pub)["result"]["id"]
    org = act("convert_lead", target=lead, org_name="张三女装", industry="实体门店")["result"]["organization"]
    act("link_objects", link_type="org_has_pain", **{"from": org, "to": pain})
    act("capture_requirement", customer="张三女装", text="想开第二家店，不知道选哪")
    llm.set_fake(_fake_presales)
    try:
        aid = act("analyze_requirement", target=org)["result"]["id"]
        act("confirm_analysis", target=aid)
        prop = act("draft_proposal", target=aid, products=["选址诊断"])["result"]["id"]
    finally:
        llm.set_fake(None)
    acc = act("accept_proposal", target=prop)
    assert acc["ok"], acc
    proj = acc["result"]["project"]
    act("link_objects", link_type="project_uses", **{"from": proj, "to": tool})

    # 收款：完成一期未收款 → 应收提醒；收款后清零
    ms = conn.execute("SELECT id FROM links l JOIN objects m ON m.id=l.from_id WHERE l.link_type='milestone_of' "
                      "AND l.to_id=%s ORDER BY m.title", (proj,)).fetchall()
    assert len(ms) == 2
    done = act("complete_milestone", target=ms[0]["id"])
    assert "1,900" in done["result"]["提醒"]
    assert functions.receivables(conn, onto)["项目"][0]["该催款"]
    act("record_payment", target=ms[0]["id"], amount=1900)
    rec = functions.receivables(conn, onto)
    assert rec["合计"] == {"合同额": 3800.0, "已收": 1900.0, "未收": 1900.0} and not rec["项目"][0]["该催款"]

    # 交付 → 知识：结项沉淀自有案例
    case = act("complete_project", target=proj, retrospective="POI + 客流热力三轮筛选", outcome="避开 2 个高租低流铺位")["result"]["case"]
    m = functions.industry_map(conn, onto, industry="实体门店")
    entry = m["痛点全景"][0]
    assert entry["案例"][0]["来源"] == "自己交付"
    assert entry["涉及工具与概念"][0]["掌握度"] == "交付过"

    # 溯源：从案例一路追到当初那条抖音
    chain = "\n".join(functions.trace(conn, onto, id=case, depth=10)["关系链"])
    assert "开店选址三步排雷" in chain and "抖音-女装店张三" in chain

    perf = functions.content_performance(conn, onto)["内容表现"][0]
    assert perf["获客"] == 5 and perf["播放24h/72h/7d"] == [None, None, 12000] and perf["选题"] == "开店选址三步排雷"
    assert functions.pipeline(conn, onto)["方案"]["已接受"]["金额"] == 3800


def test_overview_and_search(act, conn, onto):
    act("create_object", type="tool", props={"name": "飞书多维表格", "description": "在线数据库"})
    o = functions.overview(conn, onto)
    assert o["对象数量"]["工具"] == 1
    r = functions.search(conn, onto, query="飞书多维表格")
    assert r["结果"][0]["名称"] == "飞书多维表格"


def test_learning_gaps(act, conn, onto):
    new = lambda type_, **props: act("create_object", type=type_, props=props)["result"]["id"]
    ind = new("industry", name="教培", stance="target")
    pain = new("pain_point", title="3D 互动课件")
    act("link_objects", link_type="pain_in", **{"from": pain, "to": ind})
    case = new("case", title="物理老师用 AI 做 3D 课件")
    act("link_objects", link_type="case_solves", **{"from": case, "to": pain})
    t1 = new("tool", name="Three.js", mastery="heard")
    t2 = new("tool", name="Claude Code", mastery="delivered")
    for t in (t1, t2):
        act("link_objects", link_type="case_uses", **{"from": case, "to": t})
    gaps = functions.learning_gaps(conn, onto)["建议优先学习"]
    assert [g["名称"] for g in gaps] == ["Three.js"]


def test_industry_coverage_flags_weak_industries(act, conn, onto):
    act("create_object", type="industry", props={"name": "教培", "stance": "target"})
    cov = functions.industry_coverage(conn, onto)["行业覆盖"]
    assert cov[0]["行业"] == "教培" and "没有任何案例" in cov[0]["短板"]
