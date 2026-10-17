"""本体编译器：加载 ontology/*.yaml，校验，产出供 Action 引擎 / 数据库 / MCP 使用的结构。

本体文件是唯一权威（ADR-0006）。这里只做解析与一致性校验，不含业务逻辑。
"""
from __future__ import annotations

import glob
import os
import re
from dataclasses import dataclass, field

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE_TYPES = {"text", "longtext", "int", "number", "bool", "datetime", "url", "json"}
SYSTEM_FIELDS = {"id", "type", "title", "aliases", "verified", "verified_at", "confidence",
                 "extracted_by", "evidence", "origin", "created_at", "updated_at", "embedding"}


class OntologyError(Exception):
    pass


@dataclass
class PropType:
    base: str                 # text / int / enum ...
    enum: str | None = None   # 枚举名
    is_list: bool = False
    required: bool = False

    @classmethod
    def parse(cls, spec: str) -> "PropType":
        s = str(spec).strip()
        required = s.endswith("!")
        s = s.rstrip("!?")
        is_list = s.endswith("[]")
        s = s.removesuffix("[]")
        if s.startswith("enum:"):
            return cls("enum", s[5:], is_list, required)
        if s not in BASE_TYPES:
            raise OntologyError(f"未知属性类型: {spec}")
        return cls(s, None, is_list, required)


@dataclass
class ObjectType:
    name: str
    label: str
    domain: str
    title: str
    extractable: bool
    embed: list[str]
    properties: dict[str, PropType]
    managed: set[str] = field(default_factory=set)     # 生命周期字段：只能由流程行动修改（B-1）
    initial: dict = field(default_factory=dict)        # 通用新建允许写入的初始值（B-1）


@dataclass
class LinkType:
    name: str
    label: str
    from_types: list[str]
    to_types: list[str]
    require_verified: bool = False


@dataclass
class ActionType:
    name: str
    label: str
    mode: str
    gate: bool
    target: list[str]
    params: dict[str, PropType]
    precondition: dict[str, list]
    set: dict[str, object]
    creates: list[dict]
    handler: str | None
    direct: bool = False      # 只产生草稿、无需预览即可执行（真正的把关在后续闸门）
    domain: str | None = None # D-3：无 target 的行动在 YAML 里声明所属域（mcp 分组用）


@dataclass
class Function:
    name: str
    label: str
    description: str
    params: dict[str, PropType]


@dataclass
class Ontology:
    version: str
    enums: dict[str, dict[str, str]]
    object_types: dict[str, ObjectType]
    link_types: dict[str, LinkType]
    action_types: dict[str, ActionType]
    functions: dict[str, Function]
    roles: dict[str, dict]
    domains: dict[str, str] = field(default_factory=dict)   # D-3：域定义也在本体里
    errors: list[str] = field(default_factory=list)

    def enum_label(self, enum: str, key: str) -> str:
        return self.enums.get(enum, {}).get(key, key)

    def role_allows(self, role: str, action: str) -> bool:
        r = self.roles.get(role)
        if not r:
            return False
        return r["actions"] == "*" or action in r["actions"]

    def role_denies_gate(self, role: str) -> bool:
        """B-5：agent 角色对闸门行动只有发起权（预览 / 申请审批），没有确认权。"""
        return bool(self.roles.get(role, {}).get("deny_gate"))


def _as_list(v) -> list:
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


SECTIONS = ("enums", "domains", "object_types", "link_types", "action_types", "functions", "roles")


def _deep_merge(base: dict, ext: dict, where: str = "") -> None:
    """D-4 覆盖层合并：只允许追加，不允许改已有定义（改类型 / 覆盖值都报错）。"""
    for k, v in ext.items():
        path = f"{where}.{k}" if where else k
        if k not in base:
            base[k] = v
        elif isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge(base[k], v, path)
        elif isinstance(base[k], list) and isinstance(v, list):
            base[k] = base[k] + [x for x in v if x not in base[k]]
        elif base[k] != v:
            raise OntologyError(f"覆盖层不允许修改已有定义 {path}：{base[k]!r} → {v!r}（只能追加）")


def load(directory: str | None = None) -> Ontology:
    """加载内核本体 + 用户覆盖层（D-4）。

    内核 = <仓库>/ontology/*.yaml；覆盖层 = $AUTOBIZ_HOME/ontology/*.yaml，
    同名定义须写 extend: true（只允许追加：新枚举值 / 新属性 / 新类型），否则报重复。
    """
    if directory:
        dirs = [directory]
    else:
        from autobiz.paths import home
        core = os.path.join(ROOT, "ontology")
        dirs = [core, os.path.join(home(), "ontology")]
        if os.path.realpath(dirs[1]) == os.path.realpath(dirs[0]):
            dirs = [dirs[0]]
    merged: dict[str, dict] = {k: {} for k in SECTIONS}
    version = "0"
    for d in dirs:
        for path in sorted(glob.glob(os.path.join(d, "*.yaml"))):
            doc = yaml.safe_load(open(path, encoding="utf-8")) or {}
            version = str(doc.get("version", version))
            for section in SECTIONS:
                for k, v in (doc.get(section) or {}).items():
                    if k not in merged[section]:
                        merged[section][k] = v
                    elif isinstance(v, dict) and v.pop("extend", False):
                        _deep_merge(merged[section][k], v, f"{section}.{k}")
                    else:
                        raise OntologyError(f"{section}.{k} 重复定义（{os.path.basename(path)}）；要扩展请写 extend: true")
    return _build(version, merged)


def _build(version: str, m: dict) -> Ontology:
    errors: list[str] = []

    def ptypes(d: dict, where: str) -> dict[str, PropType]:
        out = {}
        for k, spec in (d or {}).items():
            try:
                out[k] = PropType.parse(spec)
            except OntologyError as e:
                errors.append(f"{where}.{k}: {e}")
        return out

    objects = {}
    for name, o in m["object_types"].items():
        props = ptypes(o.get("properties"), f"object_types.{name}")
        clash = SYSTEM_FIELDS & set(props) - {"title"}
        if clash:
            errors.append(f"object_types.{name} 使用了系统保留字段 {clash}")
        objects[name] = ObjectType(name, o.get("label", name), o.get("domain", ""), o.get("title", "title"),
                                   bool(o.get("extractable")), _as_list(o.get("embed")), props,
                                   set(_as_list(o.get("managed"))), dict(o.get("initial") or {}))

    links = {n: LinkType(n, l.get("label", n), _as_list(l["from"]), _as_list(l["to"]), bool(l.get("require_verified")))
             for n, l in m["link_types"].items()}

    actions = {}
    for name, a in m["action_types"].items():
        actions[name] = ActionType(name, a.get("label", name), a.get("mode", "manual"), bool(a.get("gate")),
                                   _as_list(a.get("target")), ptypes(a.get("params"), f"action_types.{name}.params"),
                                   a.get("precondition") or {}, a.get("set") or {}, a.get("creates") or [], a.get("handler"),
                                   bool(a.get("direct")), a.get("domain"))

    # B-1：凡被任何行动的 precondition / set 引用的字段，自动视为受保护的生命周期字段，
    # 只能由对应流程行动修改；handler 内部写的字段在各 YAML 里用 managed: 显式声明。
    for a in actions.values():
        for t in a.target:
            if t in objects:
                objects[t].managed |= set(a.precondition) | set(a.set)

    functions = {n: Function(n, f.get("label", n), f.get("description", ""), ptypes(f.get("params"), f"functions.{n}.params"))
                 for n, f in m["functions"].items()}

    onto = Ontology(version, m["enums"], objects, links, actions, functions, m["roles"], m.get("domains") or {})
    errors += _validate(onto)
    if errors:
        raise OntologyError("本体校验失败：\n  - " + "\n  - ".join(errors))
    return onto


def _validate(o: Ontology) -> list[str]:
    errs = []
    domains = set(o.domains) or {"knowledge", "market", "customer", "presales", "delivery"}   # D-3：域定义可扩展
    name_re = re.compile(r"^[a-z][a-z0-9_]{0,40}$")   # I-2：名字进 SQL 前先约束字符集

    def check_enum(pt: PropType, where: str):
        if pt.base == "enum" and pt.enum not in o.enums:
            errs.append(f"{where}: 枚举 {pt.enum} 未定义")

    for t in o.object_types.values():
        if not name_re.match(t.name):
            errs.append(f"object_types.{t.name}: 名字必须匹配 ^[a-z][a-z0-9_]{{0,40}}$")
        if t.domain not in domains:
            errs.append(f"object_types.{t.name}: domain 必须是 {domains}")
        if t.title not in t.properties:
            errs.append(f"object_types.{t.name}: title 属性 {t.title} 不存在")
        for f in t.embed:
            if f not in t.properties:
                errs.append(f"object_types.{t.name}: embed 属性 {f} 不存在")
        for k in t.properties:
            if not name_re.match(k):
                errs.append(f"object_types.{t.name}.{k}: 属性名必须匹配 ^[a-z][a-z0-9_]{{0,40}}$")
        for k in t.managed:
            if k not in t.properties:
                errs.append(f"object_types.{t.name}: managed 字段 {k} 不在 properties 中")
        for k in t.initial:
            if k not in t.properties:
                errs.append(f"object_types.{t.name}: initial 字段 {k} 不在 properties 中")
        for k, pt in t.properties.items():
            check_enum(pt, f"object_types.{t.name}.{k}")

    for l in o.link_types.values():
        for t in l.from_types + l.to_types:
            if t not in o.object_types:
                errs.append(f"link_types.{l.name}: 对象类型 {t} 不存在")

    for a in o.action_types.values():
        if a.mode not in ("auto", "manual"):
            errs.append(f"action_types.{a.name}: mode 必须是 auto / manual")
        if bool(a.handler) == bool(a.set or a.creates):
            errs.append(f"action_types.{a.name}: handler 与声明式 set/creates 必须二选一")
        for t in a.target:
            if t not in o.object_types:
                errs.append(f"action_types.{a.name}: target 类型 {t} 不存在")
        for k, pt in a.params.items():
            check_enum(pt, f"action_types.{a.name}.params.{k}")
        for t in a.target:
            props = o.object_types.get(t)
            if not props:
                continue
            for k, allowed in a.precondition.items():
                pt = props.properties.get(k)
                if pt is None:
                    errs.append(f"action_types.{a.name}.precondition: {t} 没有属性 {k}")
                elif pt.base == "enum":
                    bad = set(allowed) - set(o.enums.get(pt.enum, {}))
                    if bad:
                        errs.append(f"action_types.{a.name}.precondition.{k}: 非法枚举值 {bad}")
            for k, v in a.set.items():
                if k not in props.properties:
                    errs.append(f"action_types.{a.name}.set: {t} 没有属性 {k}")
                else:
                    errs += _check_value_ref(o, a, v, f"action_types.{a.name}.set.{k}")
                    pt = props.properties[k]
                    if pt.base == "enum" and isinstance(v, str) and not v.startswith("$") and v not in o.enums.get(pt.enum, {}):
                        errs.append(f"action_types.{a.name}.set.{k}: 非法枚举值 {v}")
        for c in a.creates:
            ct = o.object_types.get(c.get("type"))
            if not ct:
                errs.append(f"action_types.{a.name}.creates: 类型 {c.get('type')} 不存在")
                continue
            for k, v in (c.get("props") or {}).items():
                if k not in ct.properties:
                    errs.append(f"action_types.{a.name}.creates: {ct.name} 没有属性 {k}")
                errs += _check_value_ref(o, a, v, f"action_types.{a.name}.creates.{k}")
            link = c.get("link")
            if link and link.get("type") not in o.link_types:
                errs.append(f"action_types.{a.name}.creates.link: 关系 {link.get('type')} 不存在")

    for role, r in o.roles.items():
        acts = r.get("actions")
        if acts != "*":
            for a in acts or []:
                if a not in o.action_types:
                    errs.append(f"roles.{role}: 行动 {a} 不存在")
    return errs


_REF = re.compile(r"^\$(now|actor|new|target|param\.\w+|target\.\w+)$")


def _check_value_ref(o: Ontology, a: ActionType, v, where: str) -> list[str]:
    if not (isinstance(v, str) and v.startswith("$")):
        return []
    if not _REF.match(v):
        return [f"{where}: 无法识别的引用 {v}"]
    if v.startswith("$param.") and v[7:] not in a.params:
        return [f"{where}: 参数 {v[7:]} 未在 params 中声明"]
    return []


def link_allowed(o: Ontology, link_type: str, from_type: str, to_type: str) -> bool:
    lt = o.link_types.get(link_type)
    return bool(lt) and from_type in lt.from_types and to_type in lt.to_types
