"""extract_knowledge 的实现：相关度闸门 → 抽取 → 引文校验 → 合并 → 连线。

质量兜底（docs/architecture/model-quality.md）：
- 原则 2 任务拆小：相关度判定与抽取分两次调用
- 原则 3 代码校验：结构校验失败重试；引文不在原文中的对象直接丢弃（幻觉防线 F7）
- 原则 4 置信度：低置信对象照常入库但 verified=false，由 overview 列入待确认
- 原则 7 可追溯：extracted_by 记录 模型@提示词版本
"""
from __future__ import annotations

import re

from autobiz import llm, store
from autobiz.store import Ctx

TYPES = {"concept": "name", "tool": "name", "pain_point": "title", "case": "title", "method": "title"}
FIELDS = {
    "concept": ["name", "definition"],
    "tool": ["name", "vendor", "description"],
    "pain_point": ["title", "description", "audience"],
    "case": ["title", "subject", "problem", "solution", "outcome"],
    "method": ["title", "steps"],
}
FICTION_MARK = "（虚构）"   # relevance 提示词约定的摘要前缀
LIMITS = {"tool": 8}  # 每类上限，与提示词一致
MAX_CHARS = 12000     # 长文截断（影响因子 F6）；超长文档后续改为分段抽取


def _norm(s: str) -> str:
    return re.sub(r"[\s　*_`>#\-]+", "", s or "")


def quote_in_source(quote: str, content: str) -> bool:
    q = _norm(quote)
    return len(q) >= 6 and q in _norm(content)


def _validate_relevance(d: dict):
    r = d["relevance"]
    if not isinstance(r, (int, float)) or not 0 <= r <= 10:
        raise ValueError("relevance 必须是 0-10 的数字")
    if not isinstance(d.get("summary"), str):
        raise ValueError("summary 必须是字符串")


def _validate_extract(d: dict):
    for t, title in TYPES.items():
        items = d.get(t, [])
        if not isinstance(items, list):
            raise ValueError(f"{t} 必须是数组")
        for it in items:
            if not isinstance(it, dict) or not str(it.get(title, "")).strip():
                raise ValueError(f"{t} 的每一项必须是对象且包含非空 {title}")


# 批量抽取时，CLI 先并行调用模型（analyze），再逐篇经 Action 引擎写入；引擎内的 handler 从这里取预计算结果。
precomputed: dict[str, dict] = {}


def analyze(conn, doc: dict) -> dict:
    """只调模型、不写库：相关度判定 + 抽取。可在多线程中并行。"""
    thresholds = llm.models_config()["thresholds"]
    content = (doc["props"].get("content") or "")[:MAX_CHARS]
    rows = conn.execute("SELECT title, props->>'notes' notes FROM objects WHERE type='industry' "
                        "AND props->>'stance' <> 'excluded' ORDER BY title").fetchall()
    industries = [r["title"] for r in rows]
    industry_defs = "\n".join(f"  - {r['title']}" + (f"：{r['notes']}" if r["notes"] else "") for r in rows)
    body = f"标题：{doc['title']}\n\n{content}"

    sys_r, _ = llm.prompt("relevance")
    rel, _ = llm.chat_json("relevance", sys_r.replace("{industries}", "、".join(industries)), body, _validate_relevance)
    out = {"relevance": float(rel["relevance"]), "summary": rel["summary"], "content": content}
    if out["relevance"] < thresholds["relevance_min"]:
        return out
    known = [r["title"] for r in conn.execute(
        "SELECT title FROM objects WHERE type IN ('tool','concept') ORDER BY updated_at DESC LIMIT 200").fetchall()]
    sys_e, ver_e = llm.prompt("extract")
    sys_e = (sys_e.replace("{industry_defs}", industry_defs).replace("{industries}", "、".join(industries))
             .replace("{known}", "、".join(known) or "（暂无）"))
    data, tag = llm.chat_json("extract", sys_e, body, _validate_extract)
    if out["summary"].startswith(FICTION_MARK) and data.get("case"):
        # 确定性防线（"不忽悠"底线）：虚构文档里不允许出现案例，不依赖抽取模型自觉
        out["fiction_dropped"] = [f"case:{c.get('title')}" for c in data["case"]]
        data["case"] = []
    out.update(data=data, extracted_by=f"{tag}@extract.{ver_e}")
    return out


def extract(ctx: Ctx, doc: dict) -> dict:
    a = precomputed.pop(doc["id"], None)
    if a is not None and a.get("__failed__"):
        # 批量抽取时模型调用失败（CLI 回填的失败标记）。status 是托管字段，
        # 通用 update_object 写不了，只能由本流程行动落状态（B-1）。
        store.update(ctx, doc["id"], {"status": "failed"})
        return {"status": "failed", "error": a["__failed__"]}
    a = a or analyze(ctx.conn, doc)
    relevance, content = a["relevance"], a["content"]
    if "data" not in a:
        store.update(ctx, doc["id"], {"relevance": relevance, "summary": a["summary"], "status": "irrelevant"})
        return {"relevance": relevance, "status": "irrelevant"}
    data, extracted_by = a["data"], a["extracted_by"]

    ids: dict[str, dict[str, str]] = {t: {} for t in TYPES}      # 类型 → 名称 → id
    dropped, created, merged = [], 0, 0
    pending_links = []
    for t, title_field in TYPES.items():
        for it in data.get(t, [])[:LIMITS.get(t, 5)]:
            name = str(it[title_field]).strip()
            quote = str(it.get("quote", ""))
            if not quote_in_source(quote, content):
                dropped.append(f"{t}:{name}")
                continue
            hit = store.find_by_name(ctx.conn, t, name)
            if hit:
                oid, merged = hit["id"], merged + 1
            else:
                props = {f: it.get(f) for f in FIELDS[t] if it.get(f) not in (None, "")}
                if props.get("audience") not in (None, "customer", "creator"):
                    props.pop("audience")                     # 模型给了非法枚举值：丢弃该字段而不是整篇失败
                if t == "case":
                    props["case_origin"] = "external"
                try:
                    conf = float(it.get("confidence", 0.5))
                except (TypeError, ValueError):
                    conf = 0.5
                oid = store.create(ctx, t, props, origin="ai", confidence=conf, extracted_by=extracted_by, evidence=quote)
                created += 1
            ids[t][name] = oid
            store.link(ctx, "extracted_from", oid, doc["id"])
            pending_links.append((t, oid, it))

    def industry_id(name):
        name = str(name or "").strip()
        if not name:
            return None
        hit = store.find_by_name(ctx.conn, "industry", name)
        return hit["id"] if hit else None          # 行业由人维护，不让 AI 新建

    def ref(types, name):
        name = str(name or "").strip()
        for t in types:
            if name in ids[t]:
                return ids[t][name]
            hit = store.find_by_name(ctx.conn, t, name)
            if hit:
                return hit["id"]
        return None

    for t, oid, it in pending_links:
        if t == "pain_point" and (ind := industry_id(it.get("industry"))):
            store.link(ctx, "pain_in", oid, ind)
        if t == "case":
            if ind := industry_id(it.get("industry")):
                store.link(ctx, "case_in", oid, ind)
            if pid := ref(["pain_point"], it.get("pain")):
                store.link(ctx, "case_solves", oid, pid)
            for u in it.get("uses") or []:
                if uid := ref(["tool", "concept", "method"], u):
                    store.link(ctx, "case_uses", oid, uid)
        if t == "method":
            for pn in it.get("pains") or []:
                if pid := ref(["pain_point"], pn):
                    store.link(ctx, "method_targets", oid, pid)
            for u in it.get("uses") or []:
                if uid := ref(["tool", "concept"], u):
                    store.link(ctx, "method_uses", oid, uid)

    store.update(ctx, doc["id"], {"relevance": relevance, "summary": a["summary"], "status": "extracted"})
    return {"relevance": relevance, "status": "extracted", "created": created, "merged": merged,
            "dropped_no_quote": dropped, "dropped_fiction": a.get("fiction_dropped", []), "extracted_by": extracted_by}
