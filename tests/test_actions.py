"""Action 引擎与业务规则。"""


def test_every_action_logs_even_when_rejected(act, conn):
    r = act("create_object", type="tool", props={"vendor": "字节"})          # 缺必填 name
    assert not r["ok"] and "必填" in r["error"]
    log = conn.execute("SELECT status, error FROM action_log ORDER BY id DESC LIMIT 1").fetchone()
    assert log["status"] == "rejected"


def test_preview_does_not_write(act, conn):
    r = act("create_object", confirm=False, type="tool", props={"name": "扣子"})
    assert r["ok"] and r["status"] == "preview" and r["changes"][0]["op"] == "create"
    assert conn.execute("SELECT count(*) n FROM objects").fetchone()["n"] == 0


def test_role_permission(act):
    r = act("verify_object", role="assistant", targets=["x"])
    assert not r["ok"] and "无权" in r["error"]


def test_enum_accepts_chinese_label_and_rejects_garbage(act):
    assert act("create_object", type="tool", props={"name": "扣子", "mastery": "学过"})["ok"]
    r = act("create_object", type="tool", props={"name": "豆包", "mastery": "精通"})
    assert not r["ok"] and "精通" in r["error"]


def test_link_type_is_enforced(act):
    t = act("create_object", type="tool", props={"name": "扣子"})["result"]["id"]
    i = act("create_object", type="industry", props={"name": "教培"})["result"]["id"]
    r = act("link_objects", link_type="pain_in", **{"from": t, "to": i})
    assert not r["ok"] and "只能从" in r["error"]


def test_precondition_blocks_double_finalize(act):
    t = act("propose_topic", title="选址三步排雷")["result"]["id"]
    assert act("finalize_topic", target=t)["ok"]
    r = act("finalize_topic", target=t)
    assert not r["ok"] and "已定档" in r["error"]


def test_unverified_knowledge_cannot_be_cited_externally(act, conn, onto):
    from autobiz import store
    from autobiz.store import Ctx
    ctx = Ctx(conn, onto)
    ai_case = store.create(ctx, "case", {"title": "某女装店用 AI 探店"}, origin="ai", confidence=0.7)
    conn.commit()
    pr = act("create_object", type="proposal", props={"title": "方案"})["result"]["id"]
    r = act("link_objects", link_type="proposal_cites", **{"from": pr, "to": ai_case})
    assert not r["ok"] and "尚未确认" in r["error"]
    assert act("verify_object", targets=[ai_case])["ok"]
    assert act("link_objects", link_type="proposal_cites", **{"from": pr, "to": ai_case})["ok"]


def test_merge_moves_links_and_keeps_alias(act, conn):
    a = act("create_object", type="tool", props={"name": "扣子"})["result"]["id"]
    b = act("create_object", type="tool", props={"name": "Coze"})["result"]["id"]
    c = act("create_object", type="concept", props={"name": "智能体"})["result"]["id"]
    act("link_objects", link_type="tool_implements", **{"from": b, "to": c})
    assert act("merge_objects", keep=a, absorb=b)["ok"]
    assert conn.execute("SELECT aliases FROM objects WHERE id=%s", (a,)).fetchone()["aliases"] == ["Coze"]
    assert conn.execute("SELECT count(*) n FROM links WHERE from_id=%s", (a,)).fetchone()["n"] == 1
    from autobiz import store
    assert store.find_by_name(conn, "tool", "coze")["id"] == a     # 别名可查


def test_ingest_dedupes_by_url(act):
    p = dict(title="文章", source_channel="x_clip", content="正文", source_url="https://x.com/1")
    assert act("ingest_source", **p)["result"]["duplicate"] is False
    assert act("ingest_source", **p)["result"]["duplicate"] is True


def test_practice_raises_mastery_but_never_lowers(act, conn):
    t = act("create_object", type="tool", props={"name": "Claude Code", "mastery": "delivered"})["result"]["id"]
    c = act("create_object", type="concept", props={"name": "MCP"})["result"]["id"]
    act("log_practice", title="接豆包连接器", what_i_did="配置自定义连接器", targets=[t, c])
    m = {r["id"]: r["props"].get("mastery") for r in conn.execute("SELECT id, props FROM objects WHERE id IN (%s,%s)", (t, c))}
    assert m == {t: "delivered", c: "practiced"}


def test_merge_moves_links_with_audit_and_dedupes(act, conn):
    """merge 迁移关系必须经 store.link（关系校验 + 进审计）（收据：2026-10-17 评审发现裸写 links 绕过 store 层）。"""
    a = act("create_object", type="tool", props={"name": "扣子"})["result"]["id"]
    b = act("create_object", type="tool", props={"name": "Coze"})["result"]["id"]
    c = act("create_object", type="concept", props={"name": "智能体"})["result"]["id"]
    d = act("create_object", type="concept", props={"name": "工作流"})["result"]["id"]
    act("link_objects", link_type="tool_implements", **{"from": a, "to": c})
    act("link_objects", link_type="tool_implements", **{"from": b, "to": c})   # 与 keep 重复：迁移时去重
    act("link_objects", link_type="tool_implements", **{"from": b, "to": d})
    r = act("merge_objects", keep=a, absorb=b)
    assert r["ok"] and r["result"]["moved_links"] == 1          # 只有 b→d 是新关系
    assert [ch["op"] for ch in r["changes"]].count("link") == 1
    assert conn.execute("SELECT count(*) n FROM links WHERE from_id=%s", (a,)).fetchone()["n"] == 2
