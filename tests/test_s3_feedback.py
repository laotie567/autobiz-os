"""S3（ADR-0017）：批量回填 / 线索按关键词归因 / 认领 / 形态线索率 / 实测转化。"""
import pytest

from autobiz import functions


def _rec(act, conn, title, platform="douyin", fmt="live_talk", keyword=None, url=None, days_ago=1):
    tid = act("propose_topic", title=title)["result"]["id"]
    conn.execute("UPDATE objects SET props = props || '{\"status\":\"scheduled\"}' WHERE id=%s", (tid,))
    conn.commit()
    r = act("mark_published", target=tid, platform=platform, format=fmt, keyword=keyword, url=url,
            published_at=f"2026-01-01T00:00:00+08:00")
    assert r["ok"], r
    rid = r["result"]["created"][0]
    conn.execute("UPDATE objects SET props = props || jsonb_build_object('published_at', (now() - %s * interval '1 day')::text),"
                 " created_at = now() - %s * interval '1 day' WHERE id=%s", (days_ago, days_ago, rid))
    conn.commit()
    return rid


# ── 线索归因 ─────────────────────────────────────────
def test_lead_auto_attributed_by_keyword_same_platform_first(act, conn):
    old = _rec(act, conn, "选址口播", "douyin", keyword="选址", days_ago=3)
    xhs = _rec(act, conn, "选址图文", "xiaohongshu", keyword="选址", days_ago=1)
    r = act("quick_lead", role="agent", who="老周", platform="douyin", keyword="选址")
    assert r["ok"] and "选址口播" in r["result"]["来源内容"]
    assert conn.execute("SELECT 1 FROM links WHERE link_type='lead_from' AND from_id=%s AND to_id=%s",
                        (r["result"]["id"], old)).fetchone()
    assert conn.execute("SELECT props->>'attribution' a FROM objects WHERE id=%s", (r["result"]["id"],)).fetchone()["a"] == "keyword_auto"


def test_lead_not_attributed_outside_window_or_no_keyword(act, conn, onto):
    _rec(act, conn, "很久以前", keyword="课件", days_ago=30)
    r = act("quick_lead", role="agent", who="A", platform="douyin", keyword="课件")
    assert "未归因" in r["result"]["来源内容"]
    un = functions.unattributed_leads(conn, onto)["待认领"]
    assert un and un[0]["id"] == r["result"]["id"]


def test_attribute_lead_replaces_link(act, conn, onto):
    a = _rec(act, conn, "A 内容", keyword="选址")
    b = _rec(act, conn, "B 内容")
    lid = act("quick_lead", role="agent", who="X", platform="douyin", keyword="选址")["result"]["id"]
    assert act("attribute_lead", role="agent", target=lid, publish=b)["ok"]
    to = [r["to_id"] for r in conn.execute("SELECT to_id FROM links WHERE link_type='lead_from' AND from_id=%s", (lid,))]
    assert to == [b]
    assert functions.unattributed_leads(conn, onto)["待认领"] == []


def test_publish_piece_carries_keyword(act, conn, monkeypatch):
    from autobiz import store
    from autobiz.store import Ctx
    from autobiz import ontology
    onto = ontology.load()
    pid = store.create(Ctx(conn, onto), "content_piece", {"title": "件", "format": "image_post", "platform": "xiaohongshu",
                                                          "status": "approved", "version": 1,
                                                          "package": {"keyword": "探店"}})
    conn.commit()
    r = act("publish_piece", role="agent", target=pid)
    assert r["ok"], r
    assert conn.execute("SELECT props->>'keyword' k FROM objects WHERE id=%s", (r["result"]["publish_record"],)).fetchone()["k"] == "探店"


# ── 批量回填 ─────────────────────────────────────────
def test_backfill_batch_matches_by_title_url_and_id(act, conn):
    a = _rec(act, conn, "开店选址三步排雷", url="https://v.douyin.com/abc")
    b = _rec(act, conn, "AI 探店视频怎么拍", platform="xiaohongshu")
    c = _rec(act, conn, "3D 课件怎么做")
    r = act("backfill_batch", role="agent", items=[
        {"match": "开店选址 三步排雷", "views": 1200, "likes": 30, "leads_dm": 2},       # 标题（忽略空格）
        {"match": "https://v.douyin.com/abc", "stage": "24h", "views": 300},           # 链接
        {"match": b, "views": 800},                                                       # id
        {"match": "完全不相干的标题", "views": 5}])
    assert r["ok"], r
    assert len(r["result"]["已回填"]) == 3 and r["result"]["未写入"][0]["条目"] == "完全不相干的标题"
    pa = conn.execute("SELECT props FROM objects WHERE id=%s", (a,)).fetchone()["props"]
    assert pa["views_7d"] == 1200 and pa["views_24h"] == 300 and pa["leads_dm"] == 2


def test_backfill_batch_ambiguous_is_not_written(act, conn):
    _rec(act, conn, "选址避坑 上")
    _rec(act, conn, "选址避坑 下")
    r = act("backfill_batch", role="agent", items=[{"match": "选址避坑", "views": 9}])
    assert r["result"]["已回填"] == [] and len(r["result"]["未写入"][0]["候选"]) == 2


# ── 形态表现与转化 ─────────────────────────────────────
def test_format_performance_rates_and_insufficient_data(act, conn, onto):
    r1 = _rec(act, conn, "口播1", fmt="live_talk", keyword="选址")
    _rec(act, conn, "图文1", fmt="image_post")
    act("quick_lead", role="agent", who="L1", platform="douyin", keyword="选址")
    fp = functions.format_performance(conn, onto)
    assert fp["按形态"]["真人口播"]["线索"] == 1 and fp["按形态"]["图文混排"]["线索"] == 0
    assert fp["按关键词"]["选址"]["线索"] == 1
    assert "数据不足" in fp["配比建议"]


def test_format_performance_suggests_shift_with_enough_data(act, conn, onto, monkeypatch):
    from autobiz import profile
    monkeypatch.setattr(profile, "goals", lambda: {"format_mix": {"live_talk": 14, "image_post": 8, "article": 6}})
    for i in range(3):
        rid = _rec(act, conn, f"图文{i}", fmt="image_post", days_ago=5)
        conn.execute("UPDATE objects SET props = props || '{\"leads_dm\": 3}' WHERE id=%s", (rid,))
        _rec(act, conn, f"长文{i}", fmt="article", days_ago=5)
    _rec(act, conn, "老内容", fmt="article", days_ago=40)                         # 让数据跨度 ≥ 28 天
    conn.commit()
    fp = functions.format_performance(conn, onto, days=30)
    assert "「图文长文」减 2 条" in fp["配比建议"] and "「图文混排」加 2 条" in fp["配比建议"]


def test_conversion_stats_counts_consult_and_won(act, conn, onto):
    lid = act("quick_lead", role="agent", who="张老板", platform="douyin")["result"]["id"]
    org = act("convert_lead", target=lid, org_name="张老板烘焙", confirm_new=True)["result"]["organization"]
    act("record_consultation", role="agent", target=org, verdict="go", fee=900)
    act("quick_lead", role="agent", who="路人", platform="douyin")
    cs = functions.conversion_stats(conn, onto)
    assert cs["线索"] == 2 and cs["做过咨询"] == 1 and cs["成交"] == 1 and cs["订单金额"] == 900
    assert "说明" in cs                                                              # 不足 30 天不给转化率


def test_weekly_brief_has_feedback_sections(act, conn, onto):
    from autobiz import brief
    _rec(act, conn, "口播", keyword="选址")
    act("quick_lead", role="agent", who="L", platform="douyin", keyword="选址")
    act("quick_lead", role="agent", who="M", platform="douyin")
    text = brief.weekly(conn, onto)
    assert "【近30天形态线索率】" in text and "【关键词带客】【选址】1" in text and "【配比建议】" in text
    assert "【待认领线索】1 条" in text
