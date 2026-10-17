"""S2（ADR-0016）：内容快产线——周计划 / 批量定档 / 排产 / 初稿 + 体检 / 审改 / 出品 / 发布。模型用假实现。"""
import json
import re

import pytest

from autobiz import content, functions, llm

LABEL2FMT = {"真人口播": "live_talk", "数字人口播": "avatar_talk", "动效视频": "motion", "科普视频": "science",
             "讲解视频": "explainer", "图文视频": "image_video", "图文混排": "image_post", "图文长文": "article"}
SAY = "开店最怕的不是没钱，是选错位置。"          # 15 字一句


def sample(fmt, cited=(), extra=""):
    lines = [{"say": SAY + extra if i == 0 else SAY, "shot": "近景"} for i in range(18)]   # 约 270 字
    body = {
        "live_talk": {"lines": lines, "duration_s": 60},
        "avatar_talk": {"lines": lines, "duration_s": 60, "avatar": {}},
        "motion": {"duration_s": 45, "style": "赛博", "beats": [{"t": f"{i}s", "visual": "图形", "text_layer": "字"} for i in range(6)]},
        "science": {"outline": ["a", "b"], "scenes": [{"scene": f"场{i}", "visual": "画面", "narration": "旁白"} for i in range(5)],
                    "facts": []},
        "explainer": {"outline": ["a"], "steps": [{"step": f"步{i}", "screen": "屏", "say": "讲"} for i in range(4)], "subtitle": "s"},
        "image_video": {"slides": [{"caption": "字", "image_prompt": "图"} for _ in range(6)], "voiceover": "配音"},
        "image_post": {"cards": [{"text": "要点", "image_prompt": "水彩"} for _ in range(6)], "caption": "正文"},
        "article": {"sections": [{"heading": f"段{i}", "text": "选址要看三件事。" * 50} for i in range(4)],
                    "platform_titles": {"xiaohongshu": "a", "douyin": "b", "shipinhao": "c"}},
    }[fmt]
    return {"title": "开店选址三步排雷", "hook": "蹲店数客流？没用", "cover": {"main": "选址3步", "sub": "少亏10万"},
            "body": body, "cta": "私信发【选址】领清单", "keyword": "选址", "hashtags": ["开店"], "cited": list(cited)}


@pytest.fixture
def fake(monkeypatch):
    calls = []
    state = {"bad": 0, "extra": ""}

    def fn(task, system, user):
        calls.append((task, user))
        if task == "plan_topics":
            codes = re.findall(r"\[([PCILD]\d+)\]", user)
            return json.dumps({"topics": [
                {"title": f"候选选题{i}", "angle": "角度", "formats": ["live_talk", "image_post", "article"],
                 "sources": codes[:1]} for i in range(3)]}, ensure_ascii=False)
        if task == "content_draft":
            label = re.search(r"## 形态与平台\n(\S+)", user).group(1)
            fmt = LABEL2FMT[label]
            d = sample(fmt, re.findall(r"\[(K\d+)\]", user)[:1], state["extra"])
            if state["bad"] > 0:
                state["bad"] -= 1
                d["hook"] = "惊呆了！" + d["hook"]
            if "审稿意见" in user:
                d["changes"] = "按意见改了开头"
            return json.dumps(d, ensure_ascii=False)
        raise AssertionError(task)
    llm.set_fake(fn)
    yield calls, state
    llm.set_fake(None)


@pytest.fixture
def goals(monkeypatch):
    from autobiz import profile
    g = {"monthly_revenue": 50000, "revenue_lines": ["online"], "monthly_leads": 100, "lead_lines": ["online"],
         "weekly_posts": 50, "daily_live_talk": 2,
         "format_mix": {"live_talk": 14, "image_video": 8, "image_post": 8, "explainer": 6, "article": 6,
                        "motion": 4, "science": 4, "avatar_talk": 0}}
    monkeypatch.setattr(profile, "goals", lambda: g)
    return g


def _topic(act, conn, title, status="scheduled"):
    tid = act("propose_topic", title=title, angle="讲选址的三个盲区")["result"]["id"]
    if status != "candidate":
        conn.execute("UPDATE objects SET props = props || jsonb_build_object('status', %s::text) WHERE id=%s", (status, tid))
        conn.commit()
    return tid


def _piece(act, conn, fmt="live_talk", topic=None):
    topic = topic or _topic(act, conn, f"选题-{fmt}")
    r = act("schedule_pieces", topics=[topic])
    assert r["ok"], r
    return conn.execute("""SELECT o.id FROM links l JOIN objects o ON o.id=l.from_id WHERE l.link_type='piece_of'
                           AND l.to_id=%s AND o.props->>'format'=%s LIMIT 1""", (topic, fmt)).fetchone()["id"], topic


# ── 周计划与定档 ───────────────────────────────────────
def test_plan_week_creates_candidates(act, conn, fake, goals):
    act("create_object", type="pain_point", props={"title": "开店选址靠感觉", "audience": "customer"})
    r = act("plan_week", role="agent", count=3)
    assert r["ok"], r
    ts = r["result"]["候选选题"]
    assert len(ts) == 3 and "真人口播" in ts[0]["建议形态"]
    t = conn.execute("SELECT props FROM objects WHERE id=%s", (ts[0]["id"],)).fetchone()["props"]
    assert t["status"] == "candidate" and t["suggested_formats"][0] == "live_talk"
    assert conn.execute("SELECT 1 FROM links WHERE link_type='topic_from' AND from_id=%s", (ts[0]["id"],)).fetchone()


def test_finalize_topics_is_one_gate_for_a_batch(act, conn):
    ids = [_topic(act, conn, f"候选{i}", "candidate") for i in range(3)]
    assert not act("finalize_topics", role="agent", targets=ids)["ok"]          # agent 不能自行确认闸门
    r = act("finalize_topics", targets=ids)                                    # owner（未设 PIN）直通
    assert r["ok"] and len(r["result"]["已定档"]) == 3
    assert {x["s"] for x in conn.execute("SELECT props->>'status' s FROM objects WHERE id = ANY(%s)", (ids,))} == {"scheduled"}


# ── 排产 ──────────────────────────────────────────────
def test_schedule_week_follows_mix_and_daily_live_talk(act, conn, onto, goals):
    tids = [_topic(act, conn, f"选题{i}") for i in range(12)]
    r = act("schedule_pieces", role="agent", start="2026-10-19")
    assert r["ok"], r
    assert r["result"]["新建内容件"] == 50
    rows = conn.execute("SELECT props FROM objects WHERE type='content_piece'").fetchall()
    by_day_live = {}
    for x in rows:
        if x["props"]["format"] == "live_talk":
            d = x["props"]["planned_date"][:10]
            by_day_live[d] = by_day_live.get(d, 0) + 1
    assert sorted(by_day_live.values()) == [2] * 7                              # 每天正好 2 条真人口播
    assert not any(x["props"]["format"] == "avatar_talk" for x in rows)         # 数字人配比 0
    per_topic = [conn.execute("SELECT count(*) n FROM links WHERE link_type='piece_of' AND to_id=%s", (t,)).fetchone()["n"]
                 for t in tids]
    assert max(per_topic) - min(per_topic) <= 1                                 # 负载均衡
    plan = functions.content_plan(conn, onto, start="2026-10-19")
    assert plan["合计"] == 50 and plan["真人口播缺口"] == "无"


def test_schedule_requires_mix(act, conn, monkeypatch):
    from autobiz import profile
    monkeypatch.setattr(profile, "goals", lambda: {})
    _topic(act, conn, "x")
    r = act("schedule_pieces")
    assert not r["ok"] and "format_mix" in r["error"]


# ── 起草与体检 ─────────────────────────────────────────
def test_draft_passes_checks_and_cites_only_verified(act, conn, onto, fake, goals):
    case = act("create_object", type="case", props={"title": "某奶茶店选址复盘", "case_origin": "external"})["result"]["id"]
    act("verify_object", targets=[case])
    tid = _topic(act, conn, "选址")
    act("link_objects", link_type="topic_from", **{"from": tid, "to": case})
    pid, _ = _piece(act, conn, "live_talk", tid)
    r = act("draft_piece", role="agent", target=pid)
    assert r["ok"], r
    assert r["result"]["状态"] == "待审"
    o = conn.execute("SELECT props FROM objects WHERE id=%s", (pid,)).fetchone()["props"]
    assert o["version"] == 1 and o["check_report"]["passed"]
    assert "■ 口播稿" in o["package_text"] and "私信发【选址】" in o["package_text"]
    assert conn.execute("SELECT 1 FROM links WHERE link_type='piece_cites' AND from_id=%s AND to_id=%s", (pid, case)).fetchone()


def test_draft_retries_once_then_marks_check_failed(act, conn, onto, fake, goals):
    calls, state = fake
    pid, _ = _piece(act, conn)
    state["bad"] = 2                                                             # 两次都带禁说词
    r = act("draft_piece", role="agent", target=pid)
    assert r["ok"] and r["result"]["状态"] == "体检未过"
    assert any("惊呆了" in x for x in r["result"]["体检问题"])
    assert sum(1 for t, _ in calls if t == "content_draft") == 2               # 自动重写了一次
    assert functions.review_queue(conn, onto)["体检未过"] == 1


def test_draft_autofix_on_second_try(act, conn, fake, goals):
    calls, state = fake
    pid, _ = _piece(act, conn)
    state["bad"] = 1
    r = act("draft_piece", role="agent", target=pid)
    assert r["result"]["状态"] == "待审"
    assert "上一稿体检未通过" in [u for t, u in calls if t == "content_draft"][-1]


def test_unbacked_self_claim_is_blocked(act, conn, fake, goals):
    calls, state = fake
    state["extra"] = "我从0做到3.5万粉，"
    pid, _ = _piece(act, conn)
    r = act("draft_piece", role="agent", target=pid)
    assert r["result"]["状态"] == "体检未过"
    assert any("无依据的自述" in x for x in r["result"]["体检问题"])


def test_claims_ledger_allows_backed_claims(tmp_path):
    claims = [{"id": "vr", "claim": "自己开过 VR 馆，因选址亏掉 10 万", "keywords": ["VR", "亏"], "numbers": ["10万"]}]
    assert content.check_claims("我自己开VR馆亏掉10万。", claims) == []
    assert content.check_claims("我开VR馆亏了50万。", claims)                   # 数字对不上
    assert content.check_claims("我服务过200家企业。", claims)                  # 台账里没有
    assert content.check_claims("我给你讲3个方法。", claims) == []              # 小数字不算成绩
    assert content.check_claims("老板说：「我店里一个月亏20万」", claims) == []  # 引号里是人物台词


def test_avatar_disabled(act, conn, onto, fake, goals):
    from autobiz import store
    from autobiz.store import Ctx
    pid = store.create(Ctx(conn, onto), "content_piece",
                       {"title": "数字人", "format": "avatar_talk", "status": "planned", "version": 0})
    conn.commit()
    r = act("draft_piece", role="agent", target=pid)
    assert not r["ok"] and "数字人" in r["error"]


# ── 审改 / 出品 / 发布 ─────────────────────────────────
def test_review_revise_approve_produce_publish(act, conn, onto, fake, goals):
    pid, tid = _piece(act, conn)
    act("draft_piece", role="agent", target=pid)
    assert not act("review_piece", role="agent", target=pid, decision="changes")["ok"]       # 要修改必须写意见
    assert act("review_piece", role="agent", target=pid, decision="changes", feedback="开头太平，换成亏钱的故事")["ok"]
    r = act("revise_piece", role="agent", target=pid)
    assert r["ok"] and r["result"]["版本"] == 2 and r["result"]["改了什么"]
    o = conn.execute("SELECT props FROM objects WHERE id=%s", (pid,)).fetchone()["props"]
    assert [h["kind"] for h in o["history"]] == ["draft", "review", "revise"]
    assert act("review_piece", role="agent", target=pid, decision="approve")["ok"]
    assert not act("update_object", target=pid, props={"status": "published"})["ok"]         # 状态受流程保护
    assert act("mark_produced", role="agent", target=pid, url="https://example.com/v.mp4")["ok"]
    r = act("publish_piece", role="agent", target=pid, url="https://douyin.com/x")
    assert r["ok"], r
    rec = conn.execute("SELECT props FROM objects WHERE id=%s", (r["result"]["publish_record"],)).fetchone()["props"]
    assert rec["format"] == "live_talk" and rec["platform"] == "douyin"
    assert conn.execute("SELECT props->>'status' s FROM objects WHERE id=%s", (tid,)).fetchone()["s"] == "published"
    assert act("publish_piece", role="agent", target=pid, platform="shipinhao")["ok"]         # 同一条可多平台登记
    assert functions.goal_progress(conn, onto)["本周发布"]["实际"] == 2


def test_review_queue_and_package(act, conn, onto, fake, goals):
    pid, _ = _piece(act, conn, "image_post")
    act("draft_piece", role="agent", target=pid)
    q = functions.review_queue(conn, onto)
    assert q["待审总数"] == 1 and q["待审"][0]["钩子"] and q["待审"][0]["形态"] == "图文混排"
    pkg = functions.piece_package(conn, onto, id=pid)
    assert "■ 卡片" in pkg["创作包"]


def test_every_format_renders(onto):
    for f in content.FORMATS:
        text = content.render(f, content.FORMATS[f]["platform"], sample(f), onto)
        assert text.startswith("【") and "引导：" in text


def test_daily_brief_shows_review_queue(act, conn, onto, fake, goals):
    from autobiz import brief
    pid, _ = _piece(act, conn)
    act("draft_piece", role="agent", target=pid)
    assert "待审稿件 1 条" in brief.daily(conn, onto)
