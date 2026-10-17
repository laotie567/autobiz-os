"""L2 审批（B-3）：闸门行动的确认凭证。

流程：Agent 预览闸门行动 → 引擎生成 6 位审批码（approvals 表）→ Agent 把审批码转告用户
→ 用户在自己的交互式终端执行 `autobiz approve <审批码>`（看到预览、可选输 PIN）
→ Agent 带 approval 参数确认，引擎核销审批码（一次性）。

设计约束：approve 必须 isatty（Agent 从非交互 shell 调用会被拒）；PIN 只存加盐哈希。
未设置 PIN 的单人实例：owner 角色保持直通（见计划书偏离决策 2）；`autobiz setup-pin` 后全角色一律过 PIN。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import string
import sys
from datetime import timedelta

from autobiz import db

_CODE_ALPHABET = "".join(c for c in (string.ascii_uppercase + string.digits) if c not in "0O1I")
_TTL = timedelta(hours=24)


def pin_path() -> str:
    from autobiz.paths import sub
    return os.path.join(sub("config"), "approver.json")


def resolve_role(requested: str) -> str:
    """角色提权只认交互式终端（老板本人在场）：非交互 shell 一律 agent。

    AUTOBIZ_ROLE 是调用方自报的环境变量，Agent 也能设；owner 身份意味着
    「无预览直通 + 未设 PIN 时闸门直通」，只应属于坐在终端前的人。
    于是：非 TTY 环境里 owner/system/assistant 的自报一律降为 agent——
    agent 角色的闸门无条件走审批码，与是否配置 PIN 无关。
    """
    if requested != "agent" and not sys.stdin.isatty():
        return "agent"
    return requested


def pin_configured() -> bool:
    return os.path.exists(pin_path())


def _pin_ok(pin: str) -> bool:
    cfg = json.load(open(pin_path(), encoding="utf-8"))
    h = hashlib.scrypt(pin.encode(), salt=bytes.fromhex(cfg["salt"]), n=2 ** 14, r=8, p=1).hex()
    return hmac.compare_digest(h, cfg["hash"])


def setup_pin() -> None:
    """`autobiz setup-pin`：交互式设置审批 PIN（只存 scrypt 加盐哈希）。"""
    import getpass
    if not sys.stdin.isatty():
        raise SystemExit("设置 PIN 必须在交互式终端里执行")
    pin = getpass.getpass("设置审批 PIN（至少 4 位，输入不回显）：")
    if len(pin) < 4:
        raise SystemExit("PIN 至少 4 位")
    if pin != getpass.getpass("再输一次确认："):
        raise SystemExit("两次输入不一致")
    salt = secrets.token_hex(16)
    h = hashlib.scrypt(pin.encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1).hex()
    os.makedirs(os.path.dirname(pin_path()), exist_ok=True)
    open(pin_path(), "w", encoding="utf-8").write(json.dumps({"salt": salt, "hash": h}))
    os.chmod(pin_path(), 0o600)
    print(f"✓ 审批 PIN 已设置（{pin_path()}）。此后所有角色的闸门确认都要先 `autobiz approve <审批码>`。")


def gen_code() -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(6))


def create_approval(action: str, params_hash: str, target_ver, preview: dict) -> str:
    """预览闸门行动时登记待审批记录。独立连接写库，不受行动事务回滚影响。"""
    code = gen_code()
    c = db.connect()
    try:
        c.execute("INSERT INTO approvals (code, action, params_hash, target_ver, preview, expires_at) "
                  "VALUES (%s,%s,%s,%s,%s, now() + interval '24 hours')",
                  (code, action, params_hash, target_ver, json.dumps(preview, ensure_ascii=False, default=str)))
        c.commit()
    finally:
        c.close()
    return code


def approve(code: str) -> None:
    """`autobiz approve <code>`：老板本人在交互式终端审批。"""
    import getpass
    if not sys.stdin.isatty():
        raise SystemExit("审批必须在老板本人的交互式终端里执行（Agent 不得代批）")
    c = db.connect()
    try:
        row = c.execute("SELECT * FROM approvals WHERE code=%s AND status='pending' AND expires_at > now()",
                        (code.upper(),)).fetchone()
        if not row:
            raise SystemExit("审批码不存在、已用掉或已过期；请让 Agent 重新预览生成新审批码")
        recent = c.execute("SELECT count(*) n FROM approver_failures WHERE at > now() - interval '15 minutes'").fetchone()["n"]
        if recent >= 5:
            raise SystemExit("PIN 连续错误过多，15 分钟后再试")
        print(json.dumps(row["preview"], ensure_ascii=False, indent=1, default=str))
        print(f"\n行动：{row['action']} ｜ 审批码：{row['code']} ｜ 过期：{row['expires_at']:%m-%d %H:%M}")
        if pin_configured() and not _pin_ok(getpass.getpass("确认请输入 PIN：")):
            c.execute("INSERT INTO approver_failures DEFAULT VALUES")
            c.commit()
            raise SystemExit("PIN 错误")
        c.execute("UPDATE approvals SET status='approved', approved_by=%s WHERE code=%s",
                  (os.environ.get("USER") or "owner", row["code"]))
        c.commit()
        print(f"✓ 已批准 {row['code']}。请让 Agent 带该审批码（approval 参数）执行确认。")
    finally:
        c.close()
