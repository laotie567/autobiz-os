"""对象与关系的底层读写。只供 Action 引擎与 handler 调用——其他代码不得直接写库（Action 是唯一写入口）。"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import psycopg

from autobiz.embed import embed
from autobiz.ontology import Ontology, PropType


class ActionError(Exception):
    """业务规则拒绝（参数非法、前置条件不满足、权限不足等）。"""


@dataclass
class Ctx:
    conn: psycopg.Connection
    onto: Ontology
    actor: str = "owner"
    role: str = "owner"
    changes: list[dict] = field(default_factory=list)
    log_id: int | None = None
    dry_run: bool = False      # 预览模式：handler 不得产生外部副作用（如创建飞书文档）
    after_commit: list = field(default_factory=list)   # C-5：事务提交后才执行的文件副作用


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(type_: str) -> str:
    return f"{type_}_{uuid.uuid4().hex[:10]}"


# ── 属性校验 ─────────────────────────────────────────
def coerce(onto: Ontology, pt: PropType, value: Any, where: str) -> Any:
    if value is None or value == "":
        return None
    if pt.is_list:
        if isinstance(value, str):
            value = [v.strip() for v in value.split(",") if v.strip()]
        if not isinstance(value, list):
            raise ActionError(f"{where} 应为列表")
        return [coerce(onto, PropType(pt.base, pt.enum), v, where) for v in value]
    try:
        if pt.base == "int":
            v = int(value)
            if v < 0:
                raise ActionError(f"{where} 不能为负数")
            return v
        if pt.base == "number":
            return float(value)
    except (TypeError, ValueError):
        raise ActionError(f"{where} 应为数字，收到 {value!r}")
    if pt.base == "bool":
        return value if isinstance(value, bool) else str(value).lower() in ("true", "1", "yes", "是")
    if pt.base == "enum":
        options = onto.enums[pt.enum]
        if value in options:
            return value
        by_label = {label: key for key, label in options.items()}
        if value in by_label:                     # 允许传中文标签
            return by_label[value]
        raise ActionError(f"{where} 取值 {value!r} 不在 {list(options)} / {list(options.values())} 中")
    if pt.base == "json":
        return value
    return str(value)


def validate_props(onto: Ontology, type_: str, props: dict, *, partial: bool) -> dict:
    t = onto.object_types.get(type_)
    if not t:
        raise ActionError(f"未知对象类型 {type_}")
    unknown = set(props) - set(t.properties)
    if unknown:
        raise ActionError(f"{t.label}没有这些属性：{sorted(unknown)}；可用属性：{list(t.properties)}")
    out = {k: coerce(onto, t.properties[k], v, f"{t.label}.{k}") for k, v in props.items()}
    if not partial:
        missing = [k for k, pt in t.properties.items() if pt.required and out.get(k) in (None, "")]
        if missing:
            raise ActionError(f"新建{t.label}缺少必填属性 {missing}")
    return out


# ── 读 ────────────────────────────────────────────────
def get(conn: psycopg.Connection, oid: str) -> dict | None:
    return conn.execute("SELECT id,type,title,props,aliases,verified,verified_at,confidence,extracted_by,evidence,origin,"
                        "created_at,updated_at FROM objects WHERE id=%s", (oid,)).fetchone()


def require(conn: psycopg.Connection, oid: str, types: list[str] | None = None, *, lock: bool = False) -> dict:
    """lock=True 时加行锁（C-1）：写路径先锁目标对象，再读改写，防并发丢账。"""
    sql = ("SELECT id,type,title,props,aliases,verified,verified_at,confidence,extracted_by,evidence,origin,"
           "created_at,updated_at FROM objects WHERE id=%s") + (" FOR UPDATE" if lock else "")
    o = conn.execute(sql, (oid,)).fetchone()
    if not o:
        raise ActionError(f"对象 {oid} 不存在")
    if types and o["type"] not in types:
        raise ActionError(f"对象 {oid} 是 {o['type']}，此处需要 {types}")
    return o


def _norm_name(s: str) -> str:
    """C-6：客户名规范化——全半角统一、去空白、去常见企业后缀，再比较。"""
    import unicodedata
    s = unicodedata.normalize("NFKC", str(s))
    s = re.sub(r"\s+", "", s)
    for suf in ("有限责任公司", "有限公司", "股份有限公司"):
        if s.endswith(suf) and len(s) > len(suf):
            s = s[: -len(suf)]
            break
    return s.lower()


def find_by_name(conn: psycopg.Connection, type_: str, name: str) -> dict | None:
    """规范名 / 别名精确匹配（大小写、首尾空格不敏感）；再按规范化名比对（C-6）。"""
    n = name.strip()
    row = conn.execute("SELECT id,type,title,props,verified FROM objects WHERE type=%s AND "
                       "(lower(title)=lower(%s) OR lower(%s)=ANY(SELECT lower(a) FROM unnest(aliases) a)) LIMIT 1",
                       (type_, n, n)).fetchone()
    if row:
        return row
    want = _norm_name(n)
    if not want:
        return None
    for r in conn.execute("SELECT id,type,title,props,verified,aliases FROM objects WHERE type=%s", (type_,)).fetchall():
        if _norm_name(r["title"]) == want or any(_norm_name(a) == want for a in r["aliases"]):
            return r
    return None


def name_candidates(conn: psycopg.Connection, type_: str, name: str, limit: int = 3) -> list[dict]:
    """C-6：精确匹配落空后给相似候选，让用户选择而不是默默建重复客户。
    pg_trgm 对短中文名召回弱，补一条「包含新名前两字」的规则。"""
    head = name.strip()[:2]
    return conn.execute(
        "SELECT id, title FROM objects WHERE type=%s AND (similarity(title,%s) > 0.3 OR position(%s IN title) > 0) "
        "ORDER BY similarity(title,%s) DESC LIMIT %s", (type_, name, head, name, limit)).fetchall()


# ── 写（仅 Action 引擎调用）──────────────────────────
def _embed_text(onto: Ontology, type_: str, props: dict) -> str | None:
    fields = onto.object_types[type_].embed
    text = "\n".join(str(props[f]) for f in fields if props.get(f))
    return text or None


def create(ctx: Ctx, type_: str, props: dict, *, origin: str = "human", verified: bool | None = None,
           confidence: float | None = None, extracted_by: str | None = None, evidence: str | None = None,
           aliases: list[str] | None = None) -> str:
    t = ctx.onto.object_types[type_]
    props = {k: v for k, v in validate_props(ctx.onto, type_, props, partial=False).items() if v is not None}
    if verified is None:
        # B-2：可抽取类型（案例 / 方法 / 痛点 / 工具 / 概念…）一律以未确认状态入库，
        # 确认必须走 verify_object 闸门；仅内部通道（结项沉淀、种子导入）可直接写已确认。
        verified = (not t.extractable) or origin == "seed"
    oid = new_id(type_)
    text = _embed_text(ctx.onto, type_, props)
    vec = embed([text])[0] if text else None
    ctx.conn.execute(
        "INSERT INTO objects (id,type,title,props,aliases,verified,verified_at,confidence,extracted_by,evidence,origin,embedding)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (oid, type_, props[t.title], json.dumps(props, ensure_ascii=False), aliases or [], verified,
         now_iso() if verified else None, confidence, extracted_by, evidence, origin,
         _vec(vec)))
    ctx.changes.append({"op": "create", "id": oid, "type": type_, "title": props[t.title]})
    return oid


def update(ctx: Ctx, oid: str, props: dict) -> None:
    o = require(ctx.conn, oid)
    t = ctx.onto.object_types[o["type"]]
    clean = validate_props(ctx.onto, o["type"], props, partial=True)
    merged = {**o["props"], **clean}
    merged = {k: v for k, v in merged.items() if v is not None}
    if t.properties[t.title].required and not merged.get(t.title):
        raise ActionError(f"{t.label}.{t.title} 不能为空")
    sets, args = ["props=%s", "title=%s", "updated_at=now()"], [json.dumps(merged, ensure_ascii=False), merged.get(t.title, o["title"])]
    if set(clean) & set(t.embed):
        text = _embed_text(ctx.onto, o["type"], merged)
        sets.append("embedding=%s")
        args.append(_vec(embed([text])[0] if text else None))
    ctx.conn.execute(f"UPDATE objects SET {', '.join(sets)} WHERE id=%s", (*args, oid))
    before = {k: o["props"].get(k) for k in clean}
    ctx.changes.append({"op": "update", "id": oid, "type": o["type"], "title": o["title"], "before": before, "after": clean})


def set_system(ctx: Ctx, oid: str, **fields) -> None:
    allowed = {"verified", "verified_at", "confidence", "aliases", "evidence", "extracted_by"}
    assert set(fields) <= allowed
    sets = ", ".join(f"{k}=%s" for k in fields)
    ctx.conn.execute(f"UPDATE objects SET {sets}, updated_at=now() WHERE id=%s", (*fields.values(), oid))
    ctx.changes.append({"op": "update_system", "id": oid, "fields": {k: str(v) for k, v in fields.items()}})


def link(ctx: Ctx, link_type: str, from_id: str, to_id: str) -> bool:
    lt = ctx.onto.link_types.get(link_type)
    if not lt:
        raise ActionError(f"未知关系类型 {link_type}；可用：{list(ctx.onto.link_types)}")
    a, b = require(ctx.conn, from_id), require(ctx.conn, to_id)
    if a["type"] not in lt.from_types or b["type"] not in lt.to_types:
        raise ActionError(f"关系「{lt.label}」只能从 {lt.from_types} 指向 {lt.to_types}，"
                          f"收到 {a['type']} → {b['type']}")
    if lt.require_verified and not b["verified"]:
        raise ActionError(f"「{b['title']}」尚未确认，不能用于「{lt.label}」（对外出口闸门，先执行 verify_object）")
    cur = ctx.conn.execute("INSERT INTO links (link_type,from_id,to_id,created_by) VALUES (%s,%s,%s,%s) "
                           "ON CONFLICT DO NOTHING", (link_type, from_id, to_id, ctx.log_id))
    if cur.rowcount:
        ctx.changes.append({"op": "link", "link_type": link_type, "from": from_id, "to": to_id,
                            "text": f"{a['title']} —{lt.label}→ {b['title']}"})
    return bool(cur.rowcount)


def unlink(ctx: Ctx, link_type: str, from_id: str, to_id: str) -> None:
    cur = ctx.conn.execute("DELETE FROM links WHERE link_type=%s AND from_id=%s AND to_id=%s", (link_type, from_id, to_id))
    if cur.rowcount:
        ctx.changes.append({"op": "unlink", "link_type": link_type, "from": from_id, "to": to_id})


def delete(ctx: Ctx, oid: str) -> None:
    o = require(ctx.conn, oid)
    ctx.conn.execute("DELETE FROM objects WHERE id=%s", (oid,))
    ctx.changes.append({"op": "delete", "id": oid, "type": o["type"], "title": o["title"]})


def _vec(v):
    if v is None:
        return None
    import numpy as np
    return np.array(v, dtype=np.float32)
