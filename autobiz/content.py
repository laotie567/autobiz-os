"""内容快产线（S2 / ADR-0016）：周计划 → 批量定档 → 按配比排产 → AI 初稿 + 代码体检 → 你审 → 改稿 → 出品 → 发布。

分工（model-quality.md 原则 1）：选题来源、可引用知识、个人可宣称事实由代码检索并编号交给模型；
模型只写稿，只能引用编号；代码做体检（禁说词 / 绝对化用语 / 无依据的自述 / 篇幅 / 客户名与个人信息）。
系统不出成片：产出"创作包"文本，视频在小云雀出、图在豆包出、真人口播你本人拍。
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import yaml

from autobiz import llm, store
from autobiz.store import ActionError, Ctx

# ── 形态定义 ──────────────────────────────────────────
# body_keys：模型输出 body 里必须有的字段；platform：默认发布平台；tool：出品工具（写进创作包）
FORMATS: dict[str, dict] = {
    "live_talk":   {"platform": "douyin", "tool": "你本人出镜拍摄（剪映 / 提词器）", "body_keys": ["lines"]},
    "avatar_talk": {"platform": "douyin", "tool": "数字人（未启用：profile.avatar.enabled）", "body_keys": ["lines"]},
    "motion":      {"platform": "douyin", "tool": "小云雀·智能成片", "body_keys": ["beats"]},
    "science":     {"platform": "shipinhao", "tool": "小云雀·智能成片 / 短剧 Agent", "body_keys": ["outline", "scenes"]},
    "explainer":   {"platform": "shipinhao", "tool": "你本人录屏讲解", "body_keys": ["outline", "steps"]},
    "image_video": {"platform": "douyin", "tool": "豆包生图 → 小云雀 / 剪映合成", "body_keys": ["slides", "voiceover"]},
    "image_post":  {"platform": "xiaohongshu", "tool": "豆包生图（小红书图文）", "body_keys": ["cards", "caption"]},
    "article":     {"platform": "xiaohongshu", "tool": "平台长文（小红书 / 抖音 / 视频号自动转图片）",
                    "body_keys": ["sections", "platform_titles"]},
}

ABSOLUTE_WORDS = ("全网最", "最好", "最强", "最佳", "最牛", "第一", "唯一", "百分百", "100%", "保证", "绝对", "稳赚",
                  "包过", "顶级", "国家级", "零风险")
_FIRST_PERSON = re.compile(r"我|本人|俺")
_EXPERIENCE = re.compile(r"开过|做过|做到|亏了|亏掉|亏过|赚了|赚过|带过|服务过|帮过|交付过|签了|签下|涨粉|万粉|营收|年薪|创业|当过|拿下|操盘")
_NUM = re.compile(r"\d+(?:\.\d+)?\s*(?:万|千|百|亿|%|倍)?|[一二两三四五六七八九十几]+[百千万亿]+")
_SENT = re.compile(r"[^。！？!?\n；;]+")


def _tz() -> ZoneInfo:
    from autobiz.profile import timezone_name
    return ZoneInfo(timezone_name())


def _label(ctx_or_onto, enum: str, key: str) -> str:
    onto = getattr(ctx_or_onto, "onto", ctx_or_onto)
    return onto.enum_label(enum, key)


# ── 个人可宣称事实台账 ────────────────────────────────
def claims_path() -> str:
    from autobiz.paths import home
    return os.path.join(home(), "config", "claims.yaml")


def load_claims() -> list[dict]:
    p = claims_path()
    if not os.path.exists(p):
        return []
    return [c for c in (yaml.safe_load(open(p, encoding="utf-8")) or []) if isinstance(c, dict) and c.get("claim")]


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", str(s or ""))


def _is_claim_number(n: str) -> bool:
    """只把"像成绩的数字"当自述依据要核：带量级 / 百分比 / 倍数，或两位数以上（"3 个方法"不算）。"""
    if not n or n == "0":
        return False
    if re.search(r"[万千百亿%倍]", n):
        return True
    m = re.match(r"\d+(?:\.\d+)?", n)
    return bool(m) and float(m.group(0)) >= 10


def check_claims(text: str, claims: list[dict]) -> list[str]:
    """第一人称 + 数字 / 经历的句子，必须能在台账里找到依据（关键词命中，且句中每个数字都出现在该条事实里）。"""
    issues = []
    for m in _SENT.finditer(text):
        s = m.group(0).strip()
        if not s or "「" in s or "“" in s or not _FIRST_PERSON.search(s):
            continue                                      # 引号里的通常是人物台词（老板说：我……），不当自述
        nums = [n for n in (_norm(x) for x in _NUM.findall(s)) if _is_claim_number(n)]
        if not nums and not _EXPERIENCE.search(s):
            continue
        ok = False
        for c in claims:
            hay = _norm(c["claim"]) + "|" + "|".join(_norm(x) for x in (c.get("numbers") or []))
            kws = [str(k) for k in c.get("keywords") or []]
            if kws and not any(k in s for k in kws):
                continue
            if all(n in hay for n in nums):
                ok = True
                break
        if not ok:
            issues.append(f"无依据的自述：「{s[:40]}」——不在个人事实台账（config/claims.yaml）里，删掉或改成台账里的说法")
    return issues


# ── 体检 ─────────────────────────────────────────────
def _texts(x) -> list[str]:
    if isinstance(x, str):
        return [x]
    if isinstance(x, dict):
        return [t for k, v in x.items() if k not in ("cited", "changes") for t in _texts(v)]
    if isinstance(x, list):
        return [t for v in x for t in _texts(v)]
    return []


def _say_text(body: dict) -> str:
    return "".join(str(x.get("say", "")) for x in body.get("lines") or [] if isinstance(x, dict))


def _length_issues(fmt: str, d: dict) -> list[str]:
    b, out = d.get("body") or {}, []
    if fmt in ("live_talk", "avatar_talk"):
        n = len(_norm(_say_text(b)))
        if not 200 <= n <= 420:
            out.append(f"口播台词 {n} 字，应在 200–420 字（约 45–90 秒）")
        long = [x["say"][:12] for x in b.get("lines") or [] if isinstance(x, dict) and len(_norm(x.get("say", ""))) > 40]
        if long:
            out.append(f"有 {len(long)} 句台词超过 40 字（口播要短句）：{long[:2]}")
    elif fmt == "motion" and not 4 <= len(b.get("beats") or []) <= 24:
        out.append("动效视频节拍应为 4–24 个")
    elif fmt == "science":
        if len(b.get("scenes") or []) < 4:
            out.append("科普视频至少 4 场")
    elif fmt == "explainer" and len(b.get("steps") or []) < 3:
        out.append("讲解视频至少 3 个步骤")
    elif fmt == "image_video" and not 5 <= len(b.get("slides") or []) <= 12:
        out.append("图文视频应为 5–12 张图")
    elif fmt == "image_post":
        cards = b.get("cards") or []
        if not 5 <= len(cards) <= 10:
            out.append("图文混排应为 5–10 张卡片")
        if any(len(_norm(c.get("text", ""))) > 80 for c in cards if isinstance(c, dict)):
            out.append("有卡片文字超过 80 字（卡片要大字少字）")
    elif fmt == "article":
        n = len(_norm("".join(str(s.get("text", "")) for s in b.get("sections") or [] if isinstance(s, dict))))
        if not 1200 <= n <= 4000:
            out.append(f"长文正文 {n} 字，应在 1200–4000 字")
        if len(b.get("sections") or []) < 3:
            out.append("长文至少 3 个小标题段（方便平台转图片时自然切页）")
    return out


def run_checks(conn, fmt: str, d: dict, claims: list[dict] | None = None) -> list[str]:
    """确定性体检：返回问题清单（空 = 通过）。"""
    from autobiz import redact
    from autobiz.profile import profile
    brand = profile().get("brand") or {}
    text = "\n".join(_texts(d))
    issues = [f"品牌禁说词：{w}" for w in brand.get("banned_words") or [] if w and w in text]
    issues += [f"平台违规风险（绝对化用语）：{w}" for w in ABSOLUTE_WORDS if w in text]
    issues += check_claims(text, load_claims() if claims is None else claims)
    issues += _length_issues(fmt, d)
    kw = (d.get("keyword") or "").strip()
    if kw and kw not in (d.get("cta") or ""):
        issues.append(f"引导话术里没有出现私信关键词【{kw}】")
    names = [r["t"] for r in conn.execute(
        "SELECT title t FROM objects WHERE type='organization' UNION SELECT unnest(aliases) FROM objects WHERE type='organization'")]
    issues += [f"出口检查：{h}" for h in redact.check(text, names)]
    return issues


# ── 创作包渲染（给人复制粘贴用）──────────────────────
def render(fmt: str, platform: str | None, d: dict, onto) -> str:
    b = d.get("body") or {}
    plat = onto.enum_label("platform", platform) if platform else ""
    head = [f"【{onto.enum_label('content_format', fmt)}·{plat}】{d.get('title', '')}",
            f"出品：{FORMATS[fmt]['tool']}",
            f"钩子：{d.get('hook', '')}"]
    cover = d.get("cover") or {}
    if cover:
        head.append(f"封面：主标题「{cover.get('main', '')}」 副标「{cover.get('sub', '')}」")
    out = head + [""]
    if fmt in ("live_talk", "avatar_talk"):
        out.append("■ 口播稿（左：台词 ｜ 右：画面）")
        out += [f"{i}. {x.get('say', '')}    ｜ {x.get('shot', '')}" for i, x in enumerate(b.get("lines") or [], 1)]
        out += ["", "■ 提词器纯台词", "".join(f"{x.get('say', '')}\n" for x in b.get("lines") or [])]
    elif fmt in ("motion", "science", "image_video"):
        out.append("■ 小云雀指令（整段粘贴到「智能成片」对话框）")
        dur = b.get("duration_s") or (60 if fmt != "science" else 120)
        out.append(f"请做一条约 {dur} 秒的{onto.enum_label('content_format', fmt)}，平台{plat}。主题：{d.get('title', '')}。"
                   f"开头 3 秒钩子：{d.get('hook', '')}。画风：{b.get('style', '')}。"
                   "请严格使用下面的旁白 / 文字，不要改写，不要添加我没给的数据。")
        if fmt == "motion":
            out += [f"- {x.get('t', '')}：画面 {x.get('visual', '')}；文字层「{x.get('text_layer', '')}」" for x in b.get("beats") or []]
        elif fmt == "image_video":
            out += [f"- 第{i}张：{x.get('caption', '')}（画面：{x.get('image_prompt', '')}）" for i, x in enumerate(b.get("slides") or [], 1)]
            out += ["", f"旁白：{b.get('voiceover', '')}", "", "■ 豆包生图提示词（逐张）"]
            out += [f"{i}. {x.get('image_prompt', '')}" for i, x in enumerate(b.get("slides") or [], 1)]
        else:
            out += [f"- {x.get('scene', '')}：画面 {x.get('visual', '')}；旁白「{x.get('narration', '')}」" for x in b.get("scenes") or []]
            out += ["", "■ 剧本段（如用短剧 Agent，粘贴这段）", "大纲：" + " → ".join(b.get("outline") or [])]
            out += [f"第{i}场 {x.get('scene', '')}\n画面：{x.get('visual', '')}\n旁白：{x.get('narration', '')}"
                    for i, x in enumerate(b.get("scenes") or [], 1)]
            if b.get("facts"):
                out += ["", "■ 事实核对（出片前确认）"] + [f"- {x.get('claim', '')}（来源：{x.get('source', '')}）" for x in b["facts"]]
    elif fmt == "explainer":
        out.append("■ 录屏讲解稿")
        out.append("提纲：" + " / ".join(b.get("outline") or []))
        out += [f"{i}. 【屏幕】{x.get('screen', '')}\n   【讲】{x.get('say', '')}" for i, x in enumerate(b.get("steps") or [], 1)]
    elif fmt == "image_post":
        out.append("■ 卡片（逐张）")
        out += [f"{i}. {x.get('text', '')}\n   生图：{x.get('image_prompt', '')}" for i, x in enumerate(b.get("cards") or [], 1)]
        out += ["", "■ 正文", b.get("caption", "")]
    elif fmt == "article":
        t = b.get("platform_titles") or {}
        out.append("■ 各平台标题：" + "；".join(f"{onto.enum_label('platform', k)}「{v}」" for k, v in t.items()))
        out += ["", "■ 正文"]
        for s in b.get("sections") or []:
            out += [f"## {s.get('heading', '')}", s.get("text", ""), ""]
    out += ["", f"引导：{d.get('cta', '')}"]
    if d.get("hashtags"):
        out.append("话题：" + " ".join("#" + h.lstrip("#") for h in d["hashtags"]))
    return "\n".join(out).strip()


# ── 起草 / 改稿（prepare 在事务外调模型）────────────────
_CITABLE = ("case", "method", "insight", "practice_log", "pain_point", "tool", "concept")


def _sources(ctx: Ctx, topic: dict | None) -> tuple[list[dict], list[dict]]:
    """选题的来源：已确认知识可引用（编号），来源文档只作背景。"""
    if not topic:
        return [], []
    rows = ctx.conn.execute("""SELECT o.* FROM links l JOIN objects o ON o.id=l.to_id
                               WHERE l.link_type='topic_from' AND l.from_id=%s""", (topic["id"],)).fetchall()
    cite = [r for r in rows if r["type"] in _CITABLE and r["verified"]]
    bg = [r for r in rows if r not in cite]
    return cite, bg


def _brief_obj(o: dict) -> str:
    p = o["props"]
    keys = ("definition", "description", "subject", "problem", "solution", "outcome", "steps", "body", "what_i_did",
            "result", "lessons", "summary")
    return o["title"] + "：" + "；".join(str(p[k])[:160] for k in keys if p.get(k))


def _validator(fmt: str, codes: set[str]):
    def v(d):
        for k in ("title", "hook", "body", "cta"):
            if not d.get(k):
                raise ValueError(f"缺少字段 {k}")
        if not isinstance(d["body"], dict):
            raise ValueError("body 必须是对象")
        for k in FORMATS[fmt]["body_keys"]:
            if k not in d["body"]:
                raise ValueError(f"body 缺少 {k}")
        bad = [c for c in d.get("cited") or [] if c not in codes]
        if bad:
            raise ValueError(f"引用了清单之外的编号 {bad}")
    return v


def _system(fmt: str, platform: str | None):
    from autobiz.profile import profile
    styles = (profile().get("brand") or {}).get("platform_styles") or {}
    spec, _ = llm._load_prompt(f"format_{fmt}")
    from string import Template
    from autobiz.profile import prompt_vars
    spec = Template(spec).safe_substitute(prompt_vars())
    return llm.prompt("content_draft", format_spec=spec, platform_style=styles.get(platform or "", ""))


def _user_input(ctx: Ctx, piece: dict, topic: dict | None) -> tuple[str, dict]:
    cite, bg = _sources(ctx, topic)
    codes = {f"K{i}": o for i, o in enumerate(cite, 1)}
    claims = load_claims()
    siblings = []
    if topic:
        siblings = [r["t"] for r in ctx.conn.execute(
            "SELECT o.props->>'format' t FROM links l JOIN objects o ON o.id=l.from_id "
            "WHERE l.link_type='piece_of' AND l.to_id=%s AND o.id<>%s", (topic["id"], piece["id"]))]
    fmt, plat = piece["props"]["format"], piece["props"].get("platform")
    parts = [f"## 选题\n标题：{topic['title'] if topic else piece['title']}\n角度：{(topic or {}).get('props', {}).get('angle') or '（无）'}",
             f"## 形态与平台\n{ctx.onto.enum_label('content_format', fmt)} · {ctx.onto.enum_label('platform', plat) if plat else '—'}",
             "## 可引用的已确认知识（只能引用这些编号，写进 cited）\n" +
             ("\n".join(f"[{c}] {_brief_obj(o)}" for c, o in codes.items()) or "（无）"),
             "## 背景资料（外部内容，不得说成自己的经历）\n" +
             (llm.fence("选题来源", "\n".join(_brief_obj(o) for o in bg)) if bg else "（无）"),
             "## 个人可宣称事实（凡是\"我\"的经历与数字，只能用这些；没有的不要写）\n" +
             ("\n".join(f"- {c['claim']}" for c in claims) or "（台账为空：不要写任何关于\"我\"的经历和数字）")]
    if siblings:
        parts.append("## 同选题已有的其他形态（换个讲法，别重复）\n" + "、".join(
            ctx.onto.enum_label("content_format", s) for s in siblings if s))
    if piece["props"].get("notes"):
        parts.append("## 补充说明\n" + llm.fence("补充说明", piece["props"]["notes"]))
    return "\n\n".join(parts), {c: o["id"] for c, o in codes.items()}


def _topic_of(ctx: Ctx, piece_id: str) -> dict | None:
    r = ctx.conn.execute("SELECT o.* FROM links l JOIN objects o ON o.id=l.to_id "
                         "WHERE l.link_type='piece_of' AND l.from_id=%s LIMIT 1", (piece_id,)).fetchone()
    return r


def _generate(ctx: Ctx, piece: dict, user: str, code_ids: dict) -> dict:
    fmt, plat = piece["props"]["format"], piece["props"].get("platform")
    if fmt == "avatar_talk":
        from autobiz.profile import profile
        if not (profile().get("avatar") or {}).get("enabled"):
            raise ActionError("数字人口播未启用（profile.avatar.enabled=false）：接口跑通后再开")
    system, ver = _system(fmt, plat)
    val = _validator(fmt, set(code_ids))
    d, tag = llm.chat_json("content_draft", system, user, val, cache_key=llm.content_key("content_draft", ver, system + user))
    issues = run_checks(ctx.conn, fmt, d)
    if issues:                                       # 体检不过：带着问题自动重写一次
        u2 = user + "\n\n## 上一稿体检未通过，必须全部修正后输出完整新版 JSON\n" + "\n".join(f"- {x}" for x in issues)
        d, tag = llm.chat_json("content_draft", system, u2, val, cache_key=llm.content_key("content_draft", ver, system + u2))
        issues = run_checks(ctx.conn, fmt, d)
    return {"d": d, "tag": f"{tag}@content_draft.{ver}", "issues": issues,
            "cited_ids": [code_ids[c] for c in d.get("cited") or []]}


def draft_piece_prepare(ctx: Ctx, piece, p) -> dict:
    user, code_ids = _user_input(ctx, piece, _topic_of(ctx, piece["id"]))
    return _generate(ctx, piece, user, code_ids)


def revise_piece_prepare(ctx: Ctx, piece, p) -> dict:
    user, code_ids = _user_input(ctx, piece, _topic_of(ctx, piece["id"]))
    prev = json.dumps(piece["props"].get("package") or {}, ensure_ascii=False)
    user += ("\n\n## 上一版稿件（JSON）\n" + prev +
             "\n\n## 审稿意见（只改被指出的部分，其余保持不变；在 changes 字段用一两句话说明改了哪里）\n" +
             llm.fence("审稿意见", piece["props"].get("feedback") or ""))
    return _generate(ctx, piece, user, code_ids)


def _apply_draft(ctx: Ctx, piece: dict, prepared: dict, kind: str) -> dict:
    d, issues = prepared["d"], prepared["issues"]
    pr = piece["props"]
    ver = int(pr.get("version") or 0) + 1
    hist = list(pr.get("history") or [])
    hist.append({"v": ver, "at": store.now_iso(), "kind": kind, "by": prepared["tag"], "package": d,
                 **({"feedback": pr.get("feedback")} if kind == "revise" else {})})
    status = "check_failed" if issues else "in_review"
    store.update(ctx, piece["id"], {
        "package": d, "package_text": render(pr["format"], pr.get("platform"), d, ctx.onto), "version": ver,
        "history": hist[-10:], "status": status,
        "check_report": {"passed": not issues, "issues": issues, "at": store.now_iso()},
        "title": d.get("title") or piece["title"]})
    for r in ctx.conn.execute("SELECT to_id FROM links WHERE link_type='piece_cites' AND from_id=%s", (piece["id"],)).fetchall():
        store.unlink(ctx, "piece_cites", piece["id"], r["to_id"])
    for cid in prepared["cited_ids"]:
        store.link(ctx, "piece_cites", piece["id"], cid)
    out = {"id": piece["id"], "版本": ver, "状态": ctx.onto.enum_label("piece_status", status),
           "钩子": d.get("hook"), "封面": (d.get("cover") or {}).get("main")}
    if d.get("changes"):
        out["改了什么"] = d["changes"]
    if issues:
        out["体检问题"] = issues
        out["下一步"] = "体检两次未过，不进审稿队列：看问题后改选题角度 / 补个人事实台账，再 draft_piece 重写"
    return out


def draft_piece(ctx: Ctx, piece, p, prepared=None):
    return _apply_draft(ctx, piece, prepared or draft_piece_prepare(ctx, piece, p), "draft")


def revise_piece(ctx: Ctx, piece, p, prepared=None):
    return _apply_draft(ctx, piece, prepared or revise_piece_prepare(ctx, piece, p), "revise")


# ── 审稿 / 出品 / 发布 ─────────────────────────────────
def review_piece(ctx: Ctx, piece, p):
    pr = piece["props"]
    fb = (p.get("feedback") or "").strip()
    hist = list(pr.get("history") or [])
    hist.append({"v": pr.get("version"), "at": store.now_iso(), "kind": "review", "decision": p["decision"],
                 "feedback": fb, "by": ctx.actor})
    if p["decision"] == "changes":
        if not fb:
            raise ActionError("要修改时请写清审稿意见（feedback）")
        store.update(ctx, piece["id"], {"status": "changes_requested", "feedback": fb, "history": hist[-10:]})
        return {"id": piece["id"], "状态": "待修改", "下一步": "执行 revise_piece 按意见改稿"}
    store.update(ctx, piece["id"], {"status": "approved", "approved_at": store.now_iso(), "history": hist[-10:],
                                    **({"feedback": fb} if fb else {})})
    return {"id": piece["id"], "状态": "已通过", "下一步": "q piece_package 取创作包去出品，出完 mark_produced，发了 publish_piece"}


def mark_produced(ctx: Ctx, piece, p):
    props = {"status": "produced"}
    if p.get("url"):
        props["produced_url"] = p["url"]
    if p.get("note"):
        props["notes"] = ((piece["props"].get("notes") or "") + "\n" + p["note"]).strip()
    store.update(ctx, piece["id"], props)
    return {"id": piece["id"], "状态": "已出品"}


def publish_piece(ctx: Ctx, piece, p):
    pr = piece["props"]
    platform = p.get("platform") or pr.get("platform")
    if not platform:
        raise ActionError("请指定发布平台 platform")
    rid = store.create(ctx, "publish_record", {
        "title": piece["title"], "platform": platform, "format": pr["format"], "account": p.get("account"),
        "url": p.get("url"), "published_at": p.get("published_at") or store.now_iso(),
        "keyword": ((pr.get("package") or {}).get("keyword") or "").strip() or None})   # S3：线索按关键词归因
    store.link(ctx, "published_from", rid, piece["id"])
    topic = _topic_of(ctx, piece["id"])
    if topic:
        store.link(ctx, "published_as", rid, topic["id"])
        if topic["props"].get("status") == "scheduled":
            store.update(ctx, topic["id"], {"status": "published"})
    store.update(ctx, piece["id"], {"status": "published"})
    return {"publish_record": rid, "平台": ctx.onto.enum_label("platform", platform),
            "下一步": "24h / 72h / 7d 后用 backfill_metrics 回填数据（只强制 7d）"}


# ── 批量定档 ─────────────────────────────────────────
def finalize_topics(ctx: Ctx, _t, p):
    done, bad = [], []
    for oid in sorted(set(p["targets"])):
        o = store.require(ctx.conn, oid, ["topic"], lock=True)
        if o["props"].get("status") != "candidate":
            bad.append(f"{o['title']}（{ctx.onto.enum_label('topic_status', o['props'].get('status'))}）")
            continue
        store.update(ctx, oid, {"status": "scheduled", "finalized_at": store.now_iso(), "finalized_by": ctx.actor})
        done.append(o["title"])
    if bad and not done:
        raise ActionError("这些选题不在候选池，不能定档：" + "、".join(bad))
    return {"已定档": done, **({"跳过（不在候选池）": bad} if bad else {}),
            "下一步": "schedule_pieces 按配比排产内容件"}


# ── 排产 ─────────────────────────────────────────────
def schedule_pieces(ctx: Ctx, _t, p):
    from autobiz.profile import goals
    g = goals()
    mix = {f: int(n) for f, n in (g.get("format_mix") or {}).items() if int(n or 0) > 0 and f in FORMATS}
    if not mix:
        raise ActionError("profile.yaml 的 goals.format_mix 未设置每周形态配比")
    if p.get("topics"):
        topics = [store.require(ctx.conn, t, ["topic"]) for t in p["topics"]]
    else:
        topics = ctx.conn.execute("""SELECT * FROM objects o WHERE type='topic' AND props->>'status'='scheduled'
            AND NOT EXISTS (SELECT 1 FROM links l WHERE l.link_type='piece_of' AND l.to_id=o.id)
            ORDER BY created_at""").fetchall()
    if not topics:
        raise ActionError("没有已定档且未排产的选题：先 plan_week 出候选，再 finalize_topics 定档")
    days = int(p.get("days") or 7)
    tz = _tz()
    if p.get("start"):
        st = datetime.fromisoformat(str(p["start"]))
        start = (st.replace(tzinfo=tz) if st.tzinfo is None else st.astimezone(tz))
    else:
        start = datetime.now(tz) + timedelta(days=1)
    start = start.replace(hour=9, minute=0, second=0, microsecond=0)

    # 1. 名额：按天数缩放每周配比
    quota = {f: max(1, round(n * days / 7)) for f, n in mix.items()}
    daily_live = int(g.get("daily_live_talk") or 0)
    if daily_live and "live_talk" in quota:
        quota["live_talk"] = max(quota["live_talk"], daily_live * days)
    slots = [f for f, n in sorted(quota.items(), key=lambda x: -x[1]) for _ in range(n)]

    # 2. 分给选题：优先建议形态、同一选题尽量不重复形态、负载均衡
    load = {t["id"]: 0 for t in topics}
    has: dict[str, set] = {t["id"]: set() for t in topics}
    sugg = {t["id"]: set(t["props"].get("suggested_formats") or []) for t in topics}
    order = {t["id"]: i for i, t in enumerate(topics)}
    plan = []
    for f in slots:
        tid = min(load, key=lambda t: (f in has[t], f not in sugg[t], load[t], order[t]))
        load[tid] += 1
        has[tid].add(f)
        plan.append([f, tid, None])

    # 3. 排日期：真人口播每天固定 daily_live 条，其余按日负载均衡、同日尽量不撞同一选题
    day_load, day_topics = [0] * days, [set() for _ in range(days)]
    li = 0
    for item in plan:
        if item[0] == "live_talk" and daily_live:
            d = min(li // daily_live, days - 1)
            li += 1
            item[2] = d
            day_load[d] += 1
            day_topics[d].add(item[1])
    for item in plan:
        if item[2] is None:
            d = min(range(days), key=lambda i: (day_load[i], item[1] in day_topics[i], i))
            item[2] = d
            day_load[d] += 1
            day_topics[d].add(item[1])

    titles = {t["id"]: t["title"] for t in topics}
    created = []
    for f, tid, d in sorted(plan, key=lambda x: (x[2], x[0])):
        when = (start + timedelta(days=d)).isoformat(timespec="seconds")
        oid = store.create(ctx, "content_piece", {
            "title": f"{titles[tid]}·{ctx.onto.enum_label('content_format', f)}", "format": f,
            "platform": FORMATS[f]["platform"], "planned_date": when, "status": "planned", "version": 0})
        store.link(ctx, "piece_of", oid, tid)
        created.append(oid)
    per_day = {(start + timedelta(days=i)).strftime("%m-%d"): day_load[i] for i in range(days)}
    per_fmt = {ctx.onto.enum_label("content_format", f): n for f, n in quota.items()}
    return {"新建内容件": len(created), "选题数": len(topics), "按天": per_day, "按形态": per_fmt,
            "下一步": "autobiz draft 批量起草（或逐条 act draft_piece），起草完进审稿队列"}


# ── 周计划：出候选选题 ───────────────────────────────
def plan_week_prepare(ctx: Ctx, _t, p) -> dict:
    from autobiz.profile import goals
    q = ctx.conn.execute
    count = int(p.get("count") or 12)
    pools = {
        "P": q("""SELECT * FROM objects o WHERE type='pain_point' AND COALESCE(props->>'audience','customer')='customer'
                  AND NOT EXISTS (SELECT 1 FROM links l WHERE l.link_type='topic_from' AND l.to_id=o.id)
                  ORDER BY (props->>'willingness_to_pay')='high' DESC, updated_at DESC LIMIT 10""").fetchall(),
        "C": q("SELECT * FROM objects WHERE type='case' AND props->>'case_origin'='own' ORDER BY updated_at DESC LIMIT 5").fetchall(),
        "I": q("SELECT * FROM objects WHERE type='insight' ORDER BY created_at DESC LIMIT 8").fetchall(),
        "L": q("SELECT * FROM objects WHERE type='practice_log' AND COALESCE((props->>'publishable')::bool,true) "
               "ORDER BY created_at DESC LIMIT 5").fetchall(),
        "D": q("""SELECT * FROM objects WHERE type='source_doc' AND props->>'status'='extracted'
                  AND created_at > now() - interval '30 days' ORDER BY (props->>'relevance')::numeric DESC NULLS LAST LIMIT 8""").fetchall(),
    }
    codes, lines = {}, []
    for pre, rows in pools.items():
        for i, o in enumerate(rows, 1):
            c = f"{pre}{i}"
            codes[c] = o["id"]
            lines.append(f"[{c}] {_brief_obj(o)[:200]}")
    existing = [r["title"] for r in q("SELECT title FROM objects WHERE type='topic' AND props->>'status' IN ('candidate','scheduled')")]
    best = q("""SELECT title FROM objects WHERE type='publish_record' AND created_at > now() - interval '30 days'
                ORDER BY COALESCE((props->>'leads_dm')::int,0)+COALESCE((props->>'leads_contact')::int,0) DESC,
                         COALESCE((props->>'views_7d')::int,0) DESC LIMIT 5""").fetchall()
    mix = goals().get("format_mix") or {}
    user = "\n\n".join([
        f"## 任务\n出 {count} 个下周候选选题。每个选题给出建议拆成的形态（从配比里挑 3–5 种）。",
        "## 本周形态配比（键：数量）\n" + json.dumps({k: v for k, v in mix.items() if v}, ensure_ascii=False),
        "## 可用素材（只能引用这些编号，写进 sources）\n" + llm.fence("素材", "\n".join(lines) or "（无）"),
        "## 已在候选池 / 已定档的选题（不要重复）\n" + ("、".join(existing) or "（无）"),
        "## 最近表现好的内容（参考方向，不要照抄）\n" + ("、".join(r["title"] for r in best) or "（暂无数据）"),
        "## 补充方向\n" + llm.fence("补充方向", p.get("focus") or "无")])
    system, ver = llm.prompt("plan_topics")

    def v(d):
        ts = d.get("topics")
        if not isinstance(ts, list) or not ts:
            raise ValueError("topics 必须是非空数组")
        for t in ts:
            if not t.get("title"):
                raise ValueError("每个选题都要有 title")
            bad = [c for c in t.get("sources") or [] if c not in codes]
            if bad:
                raise ValueError(f"引用了清单之外的编号 {bad}")
            badf = [f for f in t.get("formats") or [] if f not in FORMATS]
            if badf:
                raise ValueError(f"非法形态 {badf}；可用：{list(FORMATS)}")
    d, tag = llm.chat_json("plan_topics", system, user, v, cache_key=llm.content_key("plan_topics", ver, user))
    return {"topics": d["topics"][:count], "codes": codes, "tag": f"{tag}@plan_topics.{ver}"}


def plan_week(ctx: Ctx, _t, p, prepared=None):
    prepared = prepared or plan_week_prepare(ctx, None, p)
    created, skipped = [], []
    for t in prepared["topics"]:
        title = t["title"].strip()
        if store.find_by_name(ctx.conn, "topic", title):
            skipped.append(title)
            continue
        oid = store.create(ctx, "topic", {"title": title, "angle": t.get("angle"), "status": "candidate",
                                          "suggested_formats": [f for f in t.get("formats") or [] if f in FORMATS]})
        for c in t.get("sources") or []:
            store.link(ctx, "topic_from", oid, prepared["codes"][c])
        created.append({"id": oid, "选题": title, "角度": t.get("angle"),
                        "建议形态": [ctx.onto.enum_label("content_format", f) for f in t.get("formats") or [] if f in FORMATS]})
    return {"候选选题": created, **({"已存在跳过": skipped} if skipped else {}),
            "下一步": "把候选讲给用户挑；用户定了之后 finalize_topics（闸门，一个审批码定一批）"}
