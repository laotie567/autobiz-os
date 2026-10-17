"""抽取管线的质量兜底：引文校验、相关度闸门、模型输出不合格重试。"""
import json

import pytest

from autobiz import llm

DOC = "电竞酒店老板普遍头疼前台人效低。成都某电竞酒店用扣子搭了智能客服，夜班前台从 3 人减到 1 人。"


@pytest.fixture
def fake():
    calls = []

    def install(relevance, extraction):
        def fn(task, system, user):
            calls.append(task)
            return json.dumps(relevance if task == "relevance" else extraction, ensure_ascii=False)
        llm.set_fake(fn)
        return calls
    yield install
    llm.set_fake(None)


def _doc(act):
    act("create_object", type="industry", props={"name": "电竞酒店", "stance": "watching"})
    return act("ingest_source", title="电竞酒店智能客服", source_channel="x_clip", content=DOC)["result"]["id"]


def test_hallucinated_object_is_dropped(act, conn, fake):
    doc = _doc(act)
    fake({"relevance": 9, "summary": "电竞酒店智能客服案例"}, {
        "tool": [{"name": "扣子", "quote": "用扣子搭了智能客服", "confidence": 0.9}],
        "case": [{"title": "电竞酒店智能客服", "subject": "成都某电竞酒店", "outcome": "夜班前台从 3 人减到 1 人",
                  "industry": "电竞酒店", "uses": ["扣子"], "quote": "夜班前台从 3 人减到 1 人", "confidence": 0.9},
                 {"title": "编造的案例", "quote": "原文里根本没有这句话", "confidence": 0.9}],
    })
    r = act("extract_knowledge", role="system", target=doc)
    assert r["ok"], r
    assert r["result"]["dropped_no_quote"] == ["case:编造的案例"]
    case = conn.execute("SELECT * FROM objects WHERE type='case'").fetchone()
    assert case["verified"] is False and case["origin"] == "ai" and "@extract.v" in case["extracted_by"]
    links = {r["link_type"] for r in conn.execute("SELECT link_type FROM links WHERE from_id=%s", (case["id"],))}
    assert links == {"extracted_from", "case_in", "case_uses"}


def test_low_relevance_skips_extraction(act, conn, fake):
    doc = _doc(act)
    calls = fake({"relevance": 2, "summary": "无关"}, {})
    act("extract_knowledge", role="system", target=doc)
    assert calls == ["relevance"]
    assert conn.execute("SELECT props->>'status' s FROM objects WHERE id=%s", (doc,)).fetchone()["s"] == "irrelevant"


def test_existing_name_is_merged_not_duplicated(act, conn, fake):
    doc = _doc(act)
    act("create_object", type="tool", props={"name": "扣子"})
    fake({"relevance": 8, "summary": "x"}, {"tool": [{"name": "扣子", "quote": "用扣子搭了智能客服", "confidence": 0.9}]})
    r = act("extract_knowledge", role="system", target=doc)
    assert r["result"]["merged"] == 1
    assert conn.execute("SELECT count(*) n FROM objects WHERE type='tool'").fetchone()["n"] == 1


def test_bad_json_retries_once_then_fails_loudly():
    outputs = iter(["不是 JSON", '{"relevance": 99, "summary": ""}'])
    llm.set_fake(lambda *a: next(outputs))
    try:
        from autobiz.extract import _validate_relevance
        with pytest.raises(ValueError, match="两次输出均不合格"):
            llm.chat_json("relevance", "s", "u", _validate_relevance)
    finally:
        llm.set_fake(None)


def test_invalid_audience_is_dropped_not_fatal(act, conn, fake):
    doc = _doc(act)
    fake({"relevance": 8, "summary": "x"}, {"pain_point": [
        {"title": "前台人效低", "audience": "boss", "quote": "电竞酒店老板普遍头疼前台人效低", "confidence": 0.8}]})
    assert act("extract_knowledge", role="system", target=doc)["ok"]
    p = conn.execute("SELECT props FROM objects WHERE type='pain_point'").fetchone()["props"]
    assert "audience" not in p


def test_eval_rules():
    from autobiz.evaluate import check
    a = {"relevance": 8, "data": {"case": [{"title": "虚构案例"}],
                                  "pain_point": [{"title": "普通人不会用AI", "industry": "教培", "audience": "creator"}]}}
    fails = check({"no_case": True, "pain_industry_forbid": [{"pain": "普通人", "industry": "教培"}]}, a)
    assert len(fails) == 2


def test_creator_pains_stay_out_of_industry_map(act, conn, onto):
    from autobiz import functions
    ind = act("create_object", type="industry", props={"name": "教培"})["result"]["id"]
    for title, aud in [("课件制作难", "customer"), ("AI评分不稳定", "creator")]:
        act("create_object", type="pain_point", props={"title": title, "audience": aud}, links=[{"link_type": "pain_in", "to": ind}])
    m = functions.industry_map(conn, onto, industry="教培")
    assert [p["痛点"] for p in m["痛点全景"]] == ["课件制作难"]


def test_fiction_document_never_yields_cases(act, conn, fake):
    doc = _doc(act)
    fake({"relevance": 8, "summary": "（虚构）一个电竞酒店的故事"},
         {"case": [{"title": "电竞酒店智能客服", "quote": "夜班前台从 3 人减到 1 人", "confidence": 0.9}]})
    r = act("extract_knowledge", role="system", target=doc)
    assert r["result"]["dropped_fiction"] == ["case:电竞酒店智能客服"]
    assert conn.execute("SELECT count(*) n FROM objects WHERE type='case'").fetchone()["n"] == 0


def test_analysis_failure_marks_doc_failed(act, conn):
    """批量抽取失败要落 status=failed（收据：2026-10-17 评审发现失败路径写 status
    被 B-1 托管字段守卫拒绝，失败状态从未真正落库，失败文档会永远留在待抽取队列）。"""
    from autobiz import extract
    doc = _doc(act)
    extract.precomputed[doc] = {"__failed__": "模型调用超时"}
    r = act("extract_knowledge", role="system", target=doc)
    assert r["ok"] and r["result"]["status"] == "failed"
    assert conn.execute("SELECT props->>'status' s FROM objects WHERE id=%s", (doc,)).fetchone()["s"] == "failed"
