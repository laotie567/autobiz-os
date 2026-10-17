"""出口检查器（J-3）：所有对外产物（脱敏选题、发布内容）落库 / 发布前的个人信息扫描。

确定性代码检查，不依赖模型自觉；命中即拒绝并点名，禁止"看起来脱敏了"。
"""
from __future__ import annotations

import re

PATTERNS: dict[str, re.Pattern] = {
    "手机号": re.compile(r"1[3-9]\d{9}"),
    "微信号": re.compile(r"(?:wxid_[A-Za-z0-9-]{5,}|微信[号:：\s]*[A-Za-z][A-Za-z0-9_-]{3,})"),
    "身份证号": re.compile(r"[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:[0-2]\d|3[01])\d{3}[\dXx]"),
    "详细地址": re.compile(r"[\u4e00-\u9fa5]{2,8}(?:省|市|区|县)[\u4e00-\u9fa5]{0,12}(?:路|街|巷|大道)\d*号?\d*(?:栋|幢)?\d*单元?\d*(?:室|房)?"),
    "QQ号": re.compile(r"QQ[号:：\s]*[1-9]\d{5,10}"),
}


def check(text: str, extra_banned: "list[str] | tuple[str, ...] | set[str]" = ()) -> list[str]:
    """返回命中的问题清单（空 = 干净）。extra_banned 通常是全量客户名 / 联系人（从数据库拉，不只看当前客户）。"""
    hits = [f"{name}：{m.group(0)}" for name, pat in PATTERNS.items() for m in pat.finditer(text)]
    hits += [f"禁用词：{b}" for b in extra_banned if b and len(b) >= 2 and b in text]
    return hits
