"""Action 引擎：唯一写入口（CONTEXT.md「行动」）。

每次调用：权限检查 → 参数校验 →（L1 预览校验 / L2 审批核销）→ 执行 → 写行动日志。
confirm=False 时在事务内完整执行后回滚，返回"将会发生什么"的预览。

生产化加固（2026-10-16，评估报告 B/C 组）：
- B-3 L1：非 direct 行动，agent/assistant 确认前必须有同参数的 preview 记录，且目标未被改过；
- B-3 L2：闸门行动预览时生成 6 位审批码（approvals 表），用户在终端 `autobiz approve` 后
  Agent 带 approval 参数确认，一次性核销；agent 角色无审批码不能确认闸门；
- C-1：目标对象 FOR UPDATE 行锁；
- C-4：带 <handler>_prepare 的行动分两段——先在事务外调模型（结果缓存，预览即所得），
  再开写事务、加锁、复核前置条件与 updated_at 后落库。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

import psycopg

from autobiz import handlers, llm, store
from autobiz.ontology import ActionType, Ontology
from autobiz.store import ActionError, Ctx


def run(conn: psycopg.Connection, onto: Ontology, name: str, params: dict | None = None, *,
        actor: str = "agent", role: str = "agent", confirm: bool = True) -> dict:
    params = dict(params or {})
    approval_code = params.pop("approval", None)        # L2 审批码：引擎内部通道，不进参数校验
    a = onto.action_types.get(name)
    log_id = conn.execute("INSERT INTO action_log (action,actor,role,params,status) VALUES (%s,%s,%s,%s,'running') RETURNING id",
                          (name, actor, role, json.dumps(params, ensure_ascii=False, default=str))).fetchone()["id"]
    conn.commit()
    if a and a.direct:
        confirm = True
    ctx = Ctx(conn, onto, actor, role, log_id=log_id, dry_run=not confirm)
    try:
        if not a:
            raise ActionError(f"未知行动 {name}；可用：{list(onto.action_types)}")
        if not onto.role_allows(role, name):
            raise ActionError(f"角色 {role} 无权执行「{a.label}」")
        clean = _validate_params(onto, a, params)
        if confirm and not a.direct and role not in ("owner", "system"):
            _require_preview(conn, a, actor, clean)                       # B-3 L1
        if confirm and a.gate:
            _require_approval(conn, onto, a, clean, approval_code, role)  # B-3 L2
        result = _execute(ctx, a, clean)
        if confirm:
            conn.commit()
            for fn in ctx.after_commit:                    # C-5：文件副作用在提交后执行，回滚不留孤儿文件
                try:
                    fn()
                except Exception as e:                     # 不让落盘失败掩盖已提交的数据变更
                    print(f"⚠ 提交后副作用失败：{e}", file=__import__("sys").stderr)
            status = "ok"
        else:
            conn.rollback()
            status = "preview"
        out = {"ok": True, "action": name, "status": status, "changes": ctx.changes, "result": result}
        _finish(conn, log_id, status, out)
        if a.gate and not confirm:                       # L2：预览即登记待审批（独立连接，回滚不影响）
            from autobiz import approve as _approve
            tv = None
            if a.target:
                t = store.get(conn, clean["target"])
                tv = t["updated_at"] if t else None
            code = _approve.create_approval(name, _params_hash(name, clean), tv, out)
            out["审批码"] = code
            out["下一步"] = (f"「{a.label}」是人工闸门：请用户在终端执行 `autobiz approve {code}`，"
                          "批准后再带 approval 参数（值=审批码）加 --confirm 执行")
        return out
    except ActionError as e:
        conn.rollback()
        _finish(conn, log_id, "rejected", None, str(e))
        return {"ok": False, "action": name, "status": "rejected", "error": str(e)}
    except Exception as e:  # 不静默吞错：记录后抛出
        conn.rollback()
        _finish(conn, log_id, "failed", None, f"{type(e).__name__}: {e}")
        raise


def _finish(conn, log_id, status, result, error=None):
    conn.execute("UPDATE action_log SET status=%s, result=%s, error=%s, finished_at=now() WHERE id=%s",
                 (status, json.dumps(result, ensure_ascii=False, default=str) if result else None, error, log_id))
    conn.commit()


def _validate_params(onto: Ontology, a: ActionType, params: dict) -> dict:
    declared = dict(a.params)
    if a.target:
        from autobiz.ontology import PropType
        declared = {"target": PropType("text", required=True), **declared}
    unknown = set(params) - set(declared)
    if unknown:
        raise ActionError(f"「{a.label}」不接受参数 {sorted(unknown)}；可用参数：{list(declared)}")
    out = {}
    for k, pt in declared.items():
        v = store.coerce(onto, pt, params.get(k), f"参数 {k}")
        if pt.required and v in (None, "", []):
            raise ActionError(f"「{a.label}」缺少必填参数 {k}")
        out[k] = v
    return out


# ── B-3 L1：确认前必须预览过，且预览后目标未被改 ──────────
def _require_preview(conn, a: ActionType, actor: str, p: dict) -> None:
    row = conn.execute("SELECT finished_at FROM action_log WHERE action=%s AND actor=%s AND status='preview' "
                       "AND jsonb_strip_nulls(params) = jsonb_strip_nulls(%s::jsonb) ORDER BY id DESC LIMIT 1",
                       (a.name, actor, json.dumps(p, ensure_ascii=False, default=str))).fetchone()
    if not row or row["finished_at"] is None:
        raise ActionError(f"「{a.label}」必须先预览（不带 --confirm），确认时用完全相同的参数")
    if p.get("target"):                      # 通用 update_object 等无类型行动也校验陈旧预览
        o = store.get(conn, p["target"])
        if o and o["updated_at"] > row["finished_at"]:
            raise ActionError("预览之后对象已被修改，请重新预览")


# ── B-3 L2：闸门确认的审批核销 ──────────────────────────
def _params_hash(action: str, params: dict) -> str:
    payload = json.dumps({"action": action, "params": params}, ensure_ascii=False, default=str, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def _require_approval(conn, onto: Ontology, a: ActionType, p: dict, code, role: str) -> None:
    from autobiz import approve as _approve
    if not code:
        if onto.role_denies_gate(role):
            raise ActionError(f"「{a.label}」是人工闸门，{role} 角色不能自行确认：请把预览返回的审批码转告用户，"
                              "由用户在终端执行 `autobiz approve <审批码>` 后，带 approval 参数再确认")
        if _approve.pin_configured():
            raise ActionError(f"「{a.label}」是人工闸门：已启用审批 PIN，所有角色都需先由用户在终端执行 "
                              "`autobiz approve <审批码>`（老板本人直接操作也一样）")
        return
    row = conn.execute("SELECT * FROM approvals WHERE code=%s", (str(code).strip().upper(),)).fetchone()
    if not row:
        raise ActionError(f"审批码 {code} 不存在；请重新预览生成新审批码")
    if row["status"] == "used":
        raise ActionError("审批码已核销（一次性）；请重新预览")
    if row["status"] != "approved":
        raise ActionError(f"审批码状态为 {row['status']}：用户尚未批准，请先在终端执行 `autobiz approve {row['code']}`")
    if row["action"] != a.name or row["params_hash"] != _params_hash(a.name, p):
        raise ActionError("审批码与当前行动或参数不一致；请用生成审批码时的参数执行，或重新预览")
    if row["expires_at"] and row["expires_at"].replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
        raise ActionError("审批码已过期；请重新预览")
    if row["target_ver"] and a.target and p.get("target"):
        t = store.get(conn, p["target"])
        if t and t["updated_at"] != row["target_ver"]:
            raise ActionError("审批之后对象已被修改，审批码作废；请重新预览")
    cur = conn.execute("UPDATE approvals SET status='used' WHERE code=%s AND status='approved'", (row["code"],))
    if not cur.rowcount:
        raise ActionError("审批码已被并发使用；请重新预览")


def _execute(ctx: Ctx, a: ActionType, p: dict) -> Any:
    prep = getattr(handlers, a.handler + "_prepare", None) if a.handler else None
    if prep is None:
        target = store.require(ctx.conn, p["target"], a.target, lock=True) if a.target else None   # C-1
        _check_precondition(ctx, a, target)
        if a.handler:
            return getattr(handlers, a.handler)(ctx, target, p)
        return _declarative(ctx, a, target, p)
    # C-4 prepare/apply 两段式：模型调用在事务外；结果按（行动+参数+目标版本）缓存，确认复用预览结果。
    snapshot = store.require(ctx.conn, p["target"], a.target) if a.target else None
    ctx.conn.commit()                                    # 结束只读事务，不在事务/锁内等模型
    prepared = _prepared_cached(a, p, snapshot, prep, ctx)
    target = store.require(ctx.conn, p["target"], a.target, lock=True) if a.target else None
    if snapshot and target and target["updated_at"] != snapshot["updated_at"]:
        raise ActionError("对象在生成期间被修改过，请重新执行")
    _check_precondition(ctx, a, target)
    return getattr(handlers, a.handler)(ctx, target, p, prepared=prepared)


def _prepared_cached(a: ActionType, p: dict, snapshot, prep, ctx):
    ver = str(snapshot["updated_at"]) if snapshot else ""
    key = llm.content_key("prepare:" + a.name, "",
                          json.dumps({"p": p, "v": ver}, ensure_ascii=False, default=str, sort_keys=True))
    hit = llm.cache_get(key)
    if hit:
        return hit["value"]
    prepared = prep(ctx, snapshot, p)
    llm.cache_put(key, prepared, "prepare")
    return prepared


def _check_precondition(ctx: Ctx, a: ActionType, target) -> None:
    if not target:
        return
    for k, allowed in a.precondition.items():
        cur = target["props"].get(k)
        if cur not in allowed:
            t = ctx.onto.object_types[target["type"]]
            enum = t.properties[k].enum
            label = ctx.onto.enum_label(enum, cur) if enum else cur
            raise ActionError(f"「{target['title']}」当前{k}={label}，「{a.label}」只允许在 "
                              f"{[ctx.onto.enum_label(enum, x) if enum else x for x in allowed]} 时执行")


def _declarative(ctx: Ctx, a: ActionType, target, p: dict) -> Any:
    refs = {"target": target, "params": p, "new": None}
    if a.set:
        store.update(ctx, target["id"], {k: _resolve(ctx, v, refs) for k, v in a.set.items()})
    created = []
    for c in a.creates:
        new = store.create(ctx, c["type"], {k: _resolve(ctx, v, refs) for k, v in (c.get("props") or {}).items()})
        refs["new"] = {"id": new}
        created.append(new)
        if c.get("link"):
            lk = c["link"]
            store.link(ctx, lk["type"], _endpoint(lk["from"], refs), _endpoint(lk["to"], refs))
    return {"created": created} if created else None


def _endpoint(ref: str, refs: dict) -> str:
    return refs["new"]["id"] if ref == "$new" else refs["target"]["id"]


def _resolve(ctx: Ctx, v, refs: dict):
    if not (isinstance(v, str) and v.startswith("$")):
        return v
    if v == "$now":
        return store.now_iso()
    if v == "$actor":
        return ctx.actor
    if v.startswith("$param."):
        return refs["params"].get(v[7:])
    if v.startswith("$target."):
        k = v[8:]
        t = refs["target"]
        return t["title"] if k == "title" else t["props"].get(k)
    raise ActionError(f"无法解析引用 {v}")
