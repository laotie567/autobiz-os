"""用户档案（D-1）：人设、业务定位、集成偏好从 profile.yaml 注入，代码与提示词不再写死。

加载顺序：config/profile.example.yaml（随包的中性默认值）→ $AUTOBIZ_HOME/config/profile.yaml（用户覆盖）。
档案缺失不阻断（用中性默认值运行），`autobiz doctor` 会提示补配。
"""
from __future__ import annotations

import os
from functools import lru_cache

import yaml

from autobiz.ontology import ROOT

_EXAMPLE = os.path.join(ROOT, "config", "profile.example.yaml")

DEFAULTS: dict = {
    "owner": {"name": "老板", "company": ""},
    "business": {"one_liner": "为中小企业提供 AI 咨询与落地服务",
                 "audience": "中小企业老板",
                 "delivery_preference": "客户易维护的国内方案",
                 "currency": "CNY"},
    "content": {"platforms": ["douyin", "shipinhao", "xiaohongshu"]},
    "integrations": {"publish": "local_md", "backup_remote": "none", "approval": "pin"},
    "paths": {},
    "privacy": {"send_customer_names_to_llm": False},
    "locale": {"timezone": "Asia/Shanghai"},
    # S2（ADR-0016）：品牌约束——内容快产线的提示词与体检都读这里
    "brand": {"account": "", "slogan": "", "voice": "实在、具体、不夸张，给能落地的做法",
              "catchphrases": [], "banned_words": ["惊呆了", "炸裂", "封神", "yyds"],
              "ip_character": "", "audience_pain": "",
              "platform_styles": {"douyin": "9:16 竖屏，节奏快，前 3 秒必须有钩子",
                                  "shipinhao": "3:4，真诚、现场感", "xiaohongshu": "3:4 图文，杂志感、清单感"}},
    "avatar": {"provider": "none", "enabled": False},
    # S1（ADR-0015）：经营目标。0 / 空 = 未设置（驾驶舱只显示实际值）
    "goals": {"monthly_revenue": 0, "revenue_lines": ["online"], "monthly_leads": 0, "lead_lines": ["online"],
              "weekly_posts": 0, "daily_live_talk": 0, "format_mix": {}},
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


@lru_cache(maxsize=1)
def profile() -> dict:
    cfg = dict(DEFAULTS)
    for path in (_EXAMPLE, os.path.join(_home(), "config", "profile.yaml")):
        if os.path.exists(path):
            cfg = _deep_merge(cfg, yaml.safe_load(open(path, encoding="utf-8")) or {})
    return cfg


def _home() -> str:
    from autobiz.paths import home
    return home()


def prompt_vars() -> dict:
    """提示词模板变量（llm.prompt 的 ${} 占位）。"""
    pr = profile()
    b = pr["business"]
    br = pr.get("brand") or {}
    return {"owner_name": pr["owner"]["name"], "company": pr["owner"].get("company", ""),
            "business_one_liner": b["one_liner"], "audience": b["audience"],
            "delivery_preference": b["delivery_preference"],
            "brand_account": br.get("account") or pr["owner"]["name"], "brand_slogan": br.get("slogan") or "",
            "brand_voice": br.get("voice") or "", "brand_catchphrases": "、".join(br.get("catchphrases") or []) or "（无）",
            "brand_banned": "、".join(br.get("banned_words") or []) or "（无）",
            "brand_ip": br.get("ip_character") or "（无固定出镜形象）",
            "audience_pain": br.get("audience_pain") or ""}


def owner_name() -> str:
    return profile()["owner"]["name"]


def goals() -> dict:
    return profile().get("goals") or {}


def timezone_name() -> str:
    return (profile().get("locale") or {}).get("timezone") or "Asia/Shanghai"
