"""MCP 服务【冻结，2026-10-04】：主入口是 AGENTS.md + autobiz 命令行；本文件保持可运行但不再扩展。

把本体交给 AI（豆包工作「自定义连接器」/ Claude）。

工具设计（model-quality.md 原则 6）：面向业务而非数据库；数量少、中文描述带示例；
写操作统一走 run_action，默认只预览，用户确认后再带 confirm=true 执行。
身份：连接器环境变量 AUTOBIZ_ACTOR（人名）与 AUTOBIZ_ROLE（owner / assistant）。
"""
from __future__ import annotations

import json
import os
from typing import Any

from mcp.server.fastmcp import FastMCP
from pgvector.psycopg import register_vector

from autobiz import actions, db, functions, ontology
from autobiz.store import ActionError

ONTO = ontology.load()


def _persona() -> tuple[str, str]:
    # B-5 + 角色提权限交互终端（approve.resolve_role）：连接器进程无 TTY，
    # AUTOBIZ_ROLE 自报的 owner 一律降为 agent，闸门无条件走审批码。
    from autobiz.approve import resolve_role
    return os.environ.get("AUTOBIZ_ACTOR", "agent"), resolve_role(os.environ.get("AUTOBIZ_ROLE", "agent"))


ACTOR, ROLE = _persona()

from autobiz.profile import owner_name  # noqa: E402

mcp = FastMCP("autobiz-ontology", instructions=(
    f"这是 {owner_name()}公司的业务本体（知识 / 内容获客 / 客户 / 售前 / 交付 五个域连成一张网）。"
    "回答业务问题前先用工具查本体，不要凭印象回答；引用对象时带上名称。"
    "修改数据只能用 run_action：先不带 confirm 预览，把预览结果讲给用户，用户同意后再 confirm=true 执行。"
    "人工闸门行动的确认需要审批码：预览返回的审批码转告用户，用户在终端 `autobiz approve <审批码>` 批准后，"
    "在参数里带 approval=审批码 再 confirm=true；没有批准的审批码，你无法确认闸门。"
    "「已确认=false」的知识（AI 抽取或助手写入）还没经过确认，不能直接用于对外内容，先 verify_object。"))

_conn = None


def conn():
    global _conn
    if _conn is None or _conn.closed:
        _conn = db.connect()
        register_vector(_conn)
    return _conn


def _call(fn, **kw) -> str:
    c = conn()
    try:
        out = fn(c, ONTO, **{k: v for k, v in kw.items() if v is not None})
        c.rollback()
    except ActionError as e:
        c.rollback()
        out = {"错误": str(e)}
    return json.dumps(out, ensure_ascii=False, default=str)


@mcp.tool()
def overview() -> str:
    """系统总览：各类对象数量，以及待我处理的事（待确认知识、候选选题、待回填数据、待跟进线索等）。
    适合"今天有什么要处理的""系统里现在有什么"这类问题。"""
    return _call(functions.overview)


@mcp.tool()
def search(query: str, types: list[str] | None = None, limit: int = 10) -> str:
    """按意思搜索本体里的任何对象。types 可限定类型（英文名，如 ["case","tool"]），
    可用类型：source_doc 来源文档, concept 概念, tool 工具, industry 行业, pain_point 痛点, case 案例, method 方法,
    insight 洞见, practice_log 实操记录, topic 选题, organization 客户组织, lead 线索, deal 商机, project 项目,
    agent_component Agent组件, course_module 课程模块, service_product 服务产品。
    例：search("门店怎么用AI做探店视频", ["case","method"])"""
    return _call(functions.search, query=query, types=types, limit=limit)


@mcp.tool()
def get_object(id: str) -> str:
    """查看一个对象的全部属性和直接关系。id 形如 tool_3fa9c1d2e4（来自其他工具的结果）。"""
    return _call(functions.get_object, id=id)


@mcp.tool()
def trace(id: str, direction: str = "both", depth: int = 4) -> str:
    """沿关系链追踪上下游，回答"它从哪来 / 带来了什么"。
    例：从一个线索追到哪条抖音、哪个选题、哪些知识；从一个案例追到哪个项目、哪个客户。direction: out / in / both。"""
    return _call(functions.trace, id=id, direction=direction, depth=depth)


@mcp.tool()
def industry_map(industry: str) -> str:
    """某行业的全景：痛点 → 案例 / 方法 / 我的产品 → 用到的工具与我的掌握度，并列出空白（有痛点没案例、没产品）。
    industry 可传行业名，如"实体门店""教培""供应链管理"。准备方案、找选题、找产品机会时先查这个。"""
    return _call(functions.industry_map, industry=industry)


@mcp.tool()
def learning_gaps(industry: str | None = None, limit: int = 10) -> str:
    """我下一步该学什么：目标行业痛点的解法里用到、但我还没实操过的工具和概念，按能解决的痛点数排序。"""
    return _call(functions.learning_gaps, industry=industry, limit=limit)


@mcp.tool()
def industry_coverage() -> str:
    """各行业的资料、痛点、案例、产品覆盖情况，标出最该补资料的行业。"""
    return _call(functions.industry_coverage)


@mcp.tool()
def presales_board() -> str:
    """售前看板：每个客户的需求推进到哪一步（碎片 → 分析 → 方案 → 成交），以及下一步该做什么。"""
    return _call(functions.presales_board)


@mcp.tool()
def capture_requirement(customer: str, text: str, channel: str | None = None) -> str:
    """丢需求：把客户的一句话、聊天记录、截图里的文字、会议纪要原样存进该客户名下。customer 填客户名（不存在会新建）或线索 id。
    channel 可选：wechat 微信 / offline 线下电话 / meeting 会议 / douyin 抖音 / other。直接写入，无需预览。"""
    r = actions.run(conn(), ONTO, "capture_requirement", {"customer": customer, "text": text, "channel": channel},
                    actor=ACTOR, role=ROLE, confirm=True)
    return json.dumps(r, ensure_ascii=False, default=str)


@mcp.tool()
def capability_map() -> str:
    """我的能力地图：概念与工具按掌握度（听说过 / 学过 / 实操过 / 交付过）分布，以及最近的实操记录。"""
    return _call(functions.capability_map)


@mcp.tool()
def content_performance(limit: int = 20) -> str:
    """内容表现：已发布内容按获客与播放排序，附最新数据与对应选题。"""
    return _call(functions.content_performance, limit=limit)


@mcp.tool()
def pipeline() -> str:
    """销售漏斗：线索按状态、商机按阶段的数量与金额，附客户、行业和带来这个商机的内容。"""
    return _call(functions.pipeline)


@mcp.tool()
def reuse_candidates() -> str:
    """可复用资产：被多个项目使用或沉淀的方法 / Agent组件 / 课程模块——产品化候选。"""
    return _call(functions.reuse_candidates)


@mcp.tool()
def list_actions(domain: str | None = None) -> str:
    """列出可执行的行动（写操作）及其参数。执行前先查这里拿到准确的参数名与可选值。
    domain 可选：knowledge 知识 / market 内容获客 / customer 客户 / presales 售前 / delivery 交付 / general 通用。"""
    doms = {n: _action_domain(n) for n in ONTO.action_types}
    out = []
    for n, a in ONTO.action_types.items():
        if domain and doms[n] != domain:
            continue
        if not ONTO.role_allows(ROLE, n):
            continue
        params = {}
        if a.target:
            params["target"] = f"目标对象 id（类型：{'/'.join(ONTO.object_types[t].label for t in a.target)}）【必填】"
        for k, pt in a.params.items():
            desc = pt.base + ("[]" if pt.is_list else "")
            if pt.base == "enum":
                desc = "可选值：" + "、".join(f"{key}({lab})" for key, lab in ONTO.enums[pt.enum].items())
            params[k] = desc + ("【必填】" if pt.required else "")
        item = {"行动": n, "名称": a.label, "人工闸门": a.gate, "无需预览": a.direct, "参数": params}
        if a.precondition:
            item["前置条件"] = a.precondition
        out.append(item)
    if not domain or domain == "general":
        out.append({"提示": "create_object 的 type 与 props 字段名见 describe_type 工具"})
    return json.dumps(out, ensure_ascii=False)


@mcp.tool()
def describe_type(type: str) -> str:
    """查看一种对象类型的属性（字段名、类型、可选值）和它能建立的关系，用于 create_object / update_object / link_objects。"""
    t = ONTO.object_types.get(type)
    if not t:
        return json.dumps({"错误": f"未知类型 {type}", "可用类型": {k: v.label for k, v in ONTO.object_types.items()}}, ensure_ascii=False)
    props = {}
    for k, pt in t.properties.items():
        d = pt.base + ("[]" if pt.is_list else "")
        if pt.base == "enum":
            d = "可选值：" + "、".join(f"{key}({lab})" for key, lab in ONTO.enums[pt.enum].items())
        props[k] = d + ("【必填】" if pt.required else "")
    links = [{"关系": n, "名称": l.label, "方向": f"{'/'.join(l.from_types)} → {'/'.join(l.to_types)}"}
             for n, l in ONTO.link_types.items() if type in l.from_types or type in l.to_types]
    return json.dumps({"类型": type, "名称": t.label, "属性": props, "关系": links}, ensure_ascii=False)


@mcp.tool()
def run_action(action: str, params: dict[str, Any], confirm: bool = False) -> str:
    """执行一个行动（唯一的写入方式）。
    第一步 confirm=false：只预览，数据库不变，返回将发生的变化；把变化讲给用户。
    第二步 用户同意后，用完全相同的参数再调用一次，confirm=true 才真正执行。
    例：run_action("propose_topic", {"title": "开店选址三步排雷", "sources": ["pain_point_ab12cd34ef"]})"""
    r = actions.run(conn(), ONTO, action, params, actor=ACTOR, role=ROLE, confirm=confirm)
    if r.get("ok") and r.get("status") == "preview":
        r["下一步"] = "这是预览，尚未写入。请向用户说明变化，得到同意后用相同参数 confirm=true 再调用。"
    return json.dumps(r, ensure_ascii=False, default=str)


@mcp.tool()
def capture_note(text: str) -> str:
    """随手记：把我的想法、判断、灵感存为「洞见」，自动关联到相关知识。用户说"记一下……"时直接调用，无需预览。"""
    r = actions.run(conn(), ONTO, "capture_note", {"text": text}, actor=ACTOR, role=ROLE, confirm=True)
    return json.dumps(r, ensure_ascii=False, default=str)


def _action_domain(name: str) -> str:
    a = ONTO.action_types[name]
    if a.domain:                                   # D-3：YAML 声明优先
        return a.domain
    if a.target:
        return ONTO.object_types[a.target[0]].domain
    return "general"


def main():
    mcp.run()


if __name__ == "__main__":
    main()
