"""生产化整改回归（评估报告附录 A 的 R1–R9 + 闸门 L1/L2）。

每条对应一个曾经真实存在的缺陷；修复后断言「被拒绝 / 结果正确」。改动 B/C 组相关代码前必跑。
"""
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from pgvector.psycopg import register_vector

from autobiz import functions, llm

TEST = os.environ.get("AUTOBIZ_TEST_DSN", "postgresql://autobiz:autobiz@127.0.0.1:5433/autobiz_test")

try:
    from autobiz import video as _video  # noqa: F401
    HAS_VIDEO = True
except ImportError:                      # 发布物不含视频板块（D-7）
    HAS_VIDEO = False
needs_video = pytest.mark.skipif(not HAS_VIDEO, reason="视频板块未随发布物")


# ── R1 / R4（B-1）：生命周期字段不能被通用写操作篡改 ──────
@pytest.fixture
def analysis_fake():
    """r1/j3 用：分析类测试一律假模型——单测不许真调云端 API（也省 token）。"""
    def fn(task, system, user):
        if task == "analyze_requirement":
            return json.dumps({
                "title": "测试分析", "business_goal": "g", "pains_summary": "p", "open_questions": ["q"],
                "fit_signals": {"positive": [], "risk": [], "to_verify": []},
                "scope_options": [{"name": "s", "products": []}], "pain_refs": [], "ref_ids": []})
        if task == "desensitize_topic":
            return json.dumps({"title": "脱敏选题", "angle": "a"})
        raise AssertionError(task)
    llm.set_fake(fn)
    yield fn
    llm.set_fake(None)


def test_r1_update_object_cannot_confirm_analysis(act, analysis_fake):
    """R1：assistant 无权 confirm_analysis，也不能用 update_object 把 status 改成 confirmed。"""
    org = act("create_object", type="organization", props={"name": "R1客户"})["result"]["id"]
    act("capture_requirement", customer=org, text="想做探店视频引流")
    aid = act("analyze_requirement", target=org)["result"]["id"]
    assert not act("confirm_analysis", role="assistant", target=aid)["ok"]
    # 通用修改在预览阶段就会被生命周期守卫拦下（confirm 则先被 L1 拦）
    r = act("update_object", role="assistant", target=aid, props={"status": "confirmed"})
    assert not r["ok"] and ("由流程行动维护" in r["error"] or "必须先预览" in r["error"]), r
    p = act("update_object", role="assistant", confirm=False, target=aid, props={"status": "confirmed"})
    assert not p["ok"] and "由流程行动维护" in p["error"], p


@needs_video
def test_r4_update_object_cannot_forge_fact_lock(act, tmp_path):
    """R4：update_object 不能伪造 facts_locked_at / status=fact_locked。"""
    oid = act("start_video_production", title="R4测试片", dir=str(tmp_path / "r4"))["result"]["id"]
    r = act("update_object", target=oid, props={"facts_locked_at": "2026-10-16T00:00:00+00:00", "status": "fact_locked"})
    assert not r["ok"] and "由流程行动维护" in r["error"]


def test_b1_initial_values_allowed_on_create(act):
    """managed 字段在新建时只允许写声明的初始值（topic 初始 candidate）。"""
    r = act("create_object", type="topic", props={"title": "初始值选题", "status": "candidate"})
    assert r["ok"]
    r2 = act("create_object", type="topic", props={"title": "越权选题", "status": "scheduled"})
    assert not r2["ok"] and "由流程行动维护" in r2["error"]


# ── R2（B-2）：可抽取类型一律未确认入库 ─────────────────
def test_r2_created_case_is_unverified_and_seed_is_verified(act, conn):
    """R2：create_object 写的案例是未确认；只有 seed 通道（origin=seed）直接已确认。"""
    oid = act("create_object", type="case", props={"title": "R2 营收翻三倍", "case_origin": "own"})["result"]["id"]
    assert conn.execute("SELECT verified FROM objects WHERE id=%s", (oid,)).fetchone()["verified"] is False
    sid = act("create_object", actor="seed", type="case", props={"title": "R2 种子案例", "case_origin": "external"})["result"]["id"]
    row = conn.execute("SELECT verified, origin FROM objects WHERE id=%s", (sid,)).fetchone()
    assert row["verified"] is True and row["origin"] == "seed"


# ── R3（B-4）：候选选题不能直接登记发布 ─────────────────
def test_r3_mark_published_requires_finalized_topic(act):
    tid = act("propose_topic", title="R3候选选题")["result"]["id"]
    r = act("mark_published", target=tid, platform="douyin")
    assert not r["ok"] and "只允许在" in r["error"]
    assert act("finalize_topic", target=tid)["ok"]
    assert act("mark_published", target=tid, platform="douyin")["ok"]


# ── R5（C-2）：收款金额必须大于 0 ──────────────────────
def test_r5_negative_payment_rejected(act):
    ms = act("create_object", type="milestone",
             props={"title": "R5里程碑", "amount": 5000})["result"]["id"]
    act("record_payment", target=ms, amount=5000)
    r = act("record_payment", target=ms, amount=-5000)
    assert not r["ok"] and "大于 0" in r["error"]


def test_c2_reverse_payment_is_gated_and_bounded(act):
    ms = act("create_object", type="milestone", props={"title": "冲正里程碑", "amount": 5000})["result"]["id"]
    act("record_payment", target=ms, amount=5000)
    assert not act("reverse_payment", target=ms, amount=9999, reason="多收")["ok"]      # 超过已收
    r = act("reverse_payment", target=ms, amount=2000, reason="多收了 2000")
    assert r["ok"] and r["result"]["received"] == 3000


def test_c2_split_amounts_sum_invariant():
    from autobiz.presales import _split_amounts
    for total, pcts in [(10000.0, [40, 60]), (10000.30, [25, 25, 25, 25]), (100.0, [1, 1, 1]),
                        (999.99, [33.33, 33.33, 33.34]), (10000, [10, 20, 30, 40])]:
        out = _split_amounts(total, pcts)
        assert abs(sum(out) - round(total, 2)) < 1e-9, (total, pcts, out)


# ── R6（C-1）：并发收款不丢账 ─────────────────────────
def test_r6_concurrent_payments_keep_total(onto):
    from autobiz import db

    def open_conn():
        c = db.connect(TEST)
        register_vector(c)
        return c

    c = open_conn()
    try:
        from autobiz import actions
        ms = actions.run(c, onto, "create_object", {"type": "milestone", "props": {"title": "R6并发", "amount": 6000}},
                         actor="测试", role="owner")["result"]["id"]
        c.commit()

        def pay(_):
            wc = open_conn()
            try:
                return actions.run(wc, onto, "record_payment", {"target": ms, "amount": 3000}, actor="并发", role="owner")
            finally:
                wc.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(pay, range(2)))
        assert all(r["ok"] for r in results), results
        got = c.execute("SELECT (props->>'received')::numeric r FROM objects WHERE id=%s", (ms,)).fetchone()["r"]
        assert float(got) == 6000.0, f"并发收款丢账：实收 {got}"
    finally:
        c.close()


# ── R7（C-3）：缓存不影响预览回滚；预览结果可复用 ─────────
@pytest.fixture
def topic_fake():
    calls = []

    def fn(task, system, user):
        calls.append(task)
        if task == "analyze_requirement":
            return json.dumps({
                "title": "R7-会员复购", "business_goal": "提升复购", "current_state": "无会员体系",
                "pains_summary": "不会运营私域", "constraints": "", "open_questions": ["预算？"],
                "fit_signals": {"positive": [], "risk": [], "to_verify": []},
                "scope_options": [{"name": "试点", "what": "先做", "deliverables": "方案",
                                   "products": re.findall(r"\[(S\d+)\]", user)[:1] or []}],
                "pain_refs": [], "ref_ids": []}, ensure_ascii=False)
        if task == "desensitize_topic":
            return json.dumps({"title": "实体店老板想做会员复购怎么办", "angle": "三条低成本玩法"}, ensure_ascii=False)
        raise AssertionError(task)

    llm.set_fake(fn)
    yield calls
    llm.set_fake(None)


def test_r7_preview_rolls_back_and_cache_reuse(act, conn, topic_fake):
    org = act("create_object", type="organization", props={"name": "R7客户"})["result"]["id"]
    act("capture_requirement", customer=org, text="想做会员复购")
    aid = act("analyze_requirement", target=org)["result"]["id"]
    act("confirm_analysis", target=aid)

    p1 = act("requirement_to_topic", target=aid, confirm=False)
    assert p1["status"] == "preview"
    assert conn.execute("SELECT count(*) n FROM objects WHERE type='topic'").fetchone()["n"] == 0, "预览不得写库"
    topic_calls = lambda: [t for t in topic_fake if t == "desensitize_topic"]
    n_after_first = len(topic_calls())
    p2 = act("requirement_to_topic", target=aid, confirm=False)
    assert len(topic_calls()) == n_after_first, "第二次预览应命中缓存（C-3 预览即所得）"
    r = act("requirement_to_topic", target=aid)          # owner 直通（未设 PIN）
    assert r["ok"] and len(topic_calls()) == n_after_first, "确认应复用预览生成的内容"
    assert conn.execute("SELECT count(*) n FROM objects WHERE type='topic'").fetchone()["n"] == 1


# ── R8（B-6）：视频目录必须在 videos 工作区之内 ──────────
@needs_video
def test_r8_video_dir_confined(act, tmp_path):
    r = act("start_video_production", title="R8", dir="/tmp/r8-outside-should-fail")
    assert not r["ok"] and "必须放在" in r["error"]
    assert act("start_video_production", title="R8内", dir=str(tmp_path / "r8-ok"))["ok"]


# ── R9（C-6）：客户重名先归一/给候选，不默默建重复 ─────────
def test_r9_similar_customer_names_not_duplicated(act):
    l1 = act("create_lead", title="线索1")["result"]["id"]
    org1 = act("convert_lead", target=l1, org_name="张三女装")["result"]["organization"]
    l2 = act("create_lead", title="线索2")["result"]["id"]
    r = act("convert_lead", target=l2, org_name="张三 女装")["result"]     # 归一后命中同一客户
    assert r["organization"] == org1
    l3 = act("create_lead", title="线索3")["result"]["id"]
    near = act("convert_lead", target=l3, org_name="张三服装")             # 相似但不等 → 给候选
    assert not near["ok"] and "名字相近" in near["error"]
    ok = act("convert_lead", target=l3, org_name="张三服装", confirm_new=True)
    assert ok["ok"]


# ── L1（B-3）：agent 确认前必须预览，且预览后对象未被改 ────
def test_l1_agent_must_preview_before_confirm(act):
    r = act("create_object", role="agent", type="insight", props={"title": "L1", "body": "x"})
    assert not r["ok"] and "必须先预览" in r["error"]
    p = act("create_object", role="agent", confirm=False, type="insight", props={"title": "L1", "body": "x"})
    assert p["status"] == "preview"
    ok = act("create_object", role="agent", type="insight", props={"title": "L1", "body": "x"})
    assert ok["ok"]


def test_l1_stale_preview_rejected(act, conn):
    oid = act("create_object", type="organization", props={"name": "L1陈旧"})["result"]["id"]
    act("update_object", role="agent", confirm=False, target=oid, props={"notes": "第一版"})   # 预览
    act("update_object", target=oid, props={"notes": "被别人改了"})                            # 预览后被改
    r = act("update_object", role="agent", target=oid, props={"notes": "第一版"})
    assert not r["ok"] and "重新预览" in r["error"]


# ── L2（B-3）：闸门审批码 ─────────────────────────────
def test_l2_gate_approval_flow(act, conn):
    tid = act("propose_topic", title="L2选题")["result"]["id"]
    # agent 预览 → 拿到审批码（pending）
    p = act("finalize_topic", role="agent", confirm=False, target=tid)
    code = p["审批码"]
    assert code and conn.execute("SELECT status FROM approvals WHERE code=%s", (code,)).fetchone()["status"] == "pending"
    # 没有审批码不能确认
    r = act("finalize_topic", role="agent", target=tid)
    assert not r["ok"] and "审批码" in r["error"]
    # 未批准的审批码不能确认
    r2 = act("finalize_topic", role="agent", target=tid, approval=code)
    assert not r2["ok"] and "尚未批准" in r2["error"]
    # 用户在终端批准（测试里直接改状态模拟 autobiz approve 的效果）
    conn.execute("UPDATE approvals SET status='approved', approved_by='测试用户' WHERE code=%s", (code,))
    conn.commit()
    r3 = act("finalize_topic", role="agent", target=tid, approval=code)
    assert r3["ok"], r3
    # 审批码一次性
    tid2 = act("propose_topic", title="L2选题二")["result"]["id"]
    p2 = act("finalize_topic", role="agent", confirm=False, target=tid2)
    conn.execute("UPDATE approvals SET status='approved' WHERE code=%s", (p2["审批码"],))
    conn.commit()
    act("finalize_topic", role="agent", target=tid2, approval=p2["审批码"])
    r4 = act("finalize_topic", role="agent", target=tid2, approval=p2["审批码"])
    assert not r4["ok"] and ("已核销" in r4["error"] or "已被修改" in r4["error"] or "必须先预览" in r4["error"])


def test_l2_owner_direct_confirm_without_pin(act):
    """未设 PIN 的单人实例：老板（owner 角色）保持直通（计划书偏离决策 2）。"""
    tid = act("propose_topic", title="owner直通")["result"]["id"]
    assert act("finalize_topic", role="owner", target=tid)["ok"]


def test_l2_pin_configured_blocks_owner_without_code(act, monkeypatch, tmp_path):
    """设置 PIN 后，owner 角色确认闸门也必须先 approve（分发形态）。"""
    import hashlib
    import secrets
    pin_dir = tmp_path / "config"
    pin_dir.mkdir()
    salt = secrets.token_hex(16)
    pin_dir.joinpath("approver.json").write_text(json.dumps(
        {"salt": salt, "hash": hashlib.scrypt("1234".encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1).hex()}))
    from autobiz import approve
    monkeypatch.setattr(approve, "pin_path", lambda: str(pin_dir / "approver.json"))
    tid = act("propose_topic", title="PIN启用")["result"]["id"]
    r = act("finalize_topic", role="owner", target=tid)
    assert not r["ok"] and "autobiz approve" in r["error"]


# ── I-1：overview 读 dup_candidates；dedupe 刷新 ─────────
def test_i1_dedupe_populates_candidates(act, conn, onto):
    functions.overview(conn, onto)     # dup_candidates 表为空也不报错
    a = act("create_object", type="concept", props={"name": "重复概念甲"})["result"]["id"]
    b = act("create_object", type="concept", props={"name": "重复概念乙"})["result"]["id"]
    conn.execute("UPDATE objects SET embedding = (SELECT embedding FROM objects WHERE id=%s) WHERE id=%s", (a, b))
    conn.commit()
    from autobiz.cli import cmd_dedupe
    cmd_dedupe(onto)
    n = conn.execute("SELECT count(*) n FROM dup_candidates").fetchone()["n"]
    assert n >= 1, "向量相同的对象应进 dup_candidates"


# ── I-2：枚举标签带特殊字符不再炸视图编译 ────────────────
def test_i2_view_compile_survives_special_labels(conn):
    """评估报告实测：枚举标签写成 Tik'Tok 曾让 init 直接 SyntaxError。"""
    from autobiz import ontology as O
    from autobiz.db import view_ddl
    fake = O.Ontology(version="t", enums={"weird": {"tiktok": "Tik'Tok"}},
                      object_types={"weirdtype": O.ObjectType(
                          "weirdtype", "怪", "knowledge", "name", False, [],
                          {"name": O.PropType.parse("text!"), "kind": O.PropType.parse("enum:weird")}, set(), {})},
                      link_types={}, action_types={}, functions={}, roles={})
    for ddl in view_ddl(fake):
        conn.execute(ddl)
    conn.execute('DROP VIEW IF EXISTS "v_weirdtype"')
    conn.commit()


def test_i2_name_constraints_reject_bad_identifiers():
    from autobiz import ontology as O
    fake = O.Ontology(version="t", enums={},
                      object_types={"BadName": O.ObjectType("BadName", "坏", "knowledge", "title", False, [],
                                                           {"title": O.PropType("text", None, False, True),
                                                            "也坏": O.PropType("text")}, set(), {})},
                      link_types={}, action_types={}, functions={}, roles={})
    errs = O._validate(fake)
    assert any("BadName" in e for e in errs) and any("也坏" in e for e in errs)


# ── D-4：本体覆盖层 extend ─────────────────────────────
def test_d4_overlay_extend(tmp_path):
    """粉丝在自己的工作区加枚举值 / 加字段 / 加新域，不改内核 YAML。"""
    import yaml as _yaml
    ov = tmp_path / "ontology"
    ov.mkdir()
    (ov / "my-store.yaml").write_text(_yaml.safe_dump({
        "enums": {"platform": {"extend": True, "kuaishou": "快手"}},
        "object_types": {"organization": {"extend": True, "properties": {"store_count": "int", "city": "text"}}},
    }, allow_unicode=True), encoding="utf-8")
    import os
    old = os.environ.get("AUTOBIZ_HOME")
    os.environ["AUTOBIZ_HOME"] = str(tmp_path)
    try:
        from autobiz import ontology as O
        onto = O.load()          # lru_cache 不在此（load 无缓存），直接重载
        assert onto.enums["platform"]["kuaishou"] == "快手"
        assert "store_count" in onto.object_types["organization"].properties
    finally:
        os.environ["AUTOBIZ_HOME"] = old or ""
    from autobiz import ontology as O2
    onto2 = O2.load(os.path.join(O2.ROOT, "ontology"))     # 显式只载内核
    assert "kuaishou" not in onto2.enums["platform"], "覆盖层不得污染内核加载"


def test_d4_overlay_rejects_modification(tmp_path):
    import yaml as _yaml
    ov = tmp_path / "ontology"
    ov.mkdir()
    (ov / "bad.yaml").write_text(_yaml.safe_dump({
        "object_types": {"organization": {"extend": True, "properties": {"name": "longtext"}}},
    }, allow_unicode=True), encoding="utf-8")
    import os
    old = os.environ.get("AUTOBIZ_HOME")
    os.environ["AUTOBIZ_HOME"] = str(tmp_path)
    try:
        from autobiz import ontology as O
        with pytest.raises(O.OntologyError, match="不允许修改已有定义"):
            O.load()
    finally:
        os.environ["AUTOBIZ_HOME"] = old or ""


# ── J-3：出口检查器 ────────────────────────────────────
def test_j3_redact_catches_personal_info():
    from autobiz import redact
    assert any("手机号" in h for h in redact.check("联系我 13812345678"))
    assert any("微信号" in h for h in redact.check("加微信 wxid_ab12cd34"))
    assert any("身份证号" in h for h in redact.check("身份证 11010119900307867X"))
    assert any("禁用词" in h for h in redact.check("张三女装的老板", ["张三女装"]))
    assert redact.check("实体店如何做会员复购") == []


def test_j3_requirement_to_topic_blocks_phone(act, conn, analysis_fake):
    """脱敏闸门接出口检查器：选题里带手机号直接拒绝。"""
    import json as _json
    org = act("create_object", type="organization", props={"name": "J3客户"})["result"]["id"]
    act("capture_requirement", customer=org, text="想引流")
    aid = act("analyze_requirement", target=org)["result"]["id"]
    act("confirm_analysis", target=aid)
    def fn(task, system, user):
        if task == "desensitize_topic":
            return _json.dumps({"title": "老板电话 13812345678 教你引流", "angle": "a"})
        return analysis_fake(task, system, user)

    llm.set_fake(fn)
    try:
        with pytest.raises(ValueError, match="手机号"):    # 出口检查器在模型校验层拦截
            act("requirement_to_topic", target=aid)
    finally:
        llm.set_fake(None)
