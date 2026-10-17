import textwrap

import pytest

from autobiz import ontology


def write(tmp_path, name, text):
    (tmp_path / name).write_text(textwrap.dedent(text), encoding="utf-8")


def test_real_ontology_loads_and_connects_all_domains():
    o = ontology.load()
    domains = {t.domain for t in o.object_types.values()}
    assert {"knowledge", "market", "customer", "presales", "delivery"} <= domains
    assert domains - {"knowledge", "market", "customer", "presales", "delivery"} <= {"opc"}   # 私有 OPC 模块（ADR-0018）
    # 飞轮的三段接缝必须存在
    assert ontology.link_allowed(o, "topic_from", "topic", "pain_point")        # 知识 → 市场
    assert ontology.link_allowed(o, "lead_from", "lead", "publish_record")      # 市场 → 客户
    assert ontology.link_allowed(o, "project_produced", "project", "case")      # 交付 → 知识


def test_every_handler_is_implemented():
    from autobiz import handlers
    o = ontology.load()
    missing = [a.handler for a in o.action_types.values() if a.handler and not hasattr(handlers, a.handler)]
    assert missing == []


def test_every_function_is_implemented():
    from autobiz import functions
    o = ontology.load()
    assert [f for f in o.functions if not hasattr(functions, f)] == []


def test_rejects_unknown_enum_and_dangling_link(tmp_path):
    write(tmp_path, "a.yaml", """
        object_types:
          thing:
            label: 东西
            domain: knowledge
            title: name
            properties: { name: text!, color: "enum:nope" }
        link_types:
          bad: { from: thing, to: ghost }
    """)
    with pytest.raises(ontology.OntologyError) as e:
        ontology.load(str(tmp_path))
    assert "nope" in str(e.value) and "ghost" in str(e.value)


def test_rejects_undeclared_param_reference(tmp_path):
    write(tmp_path, "a.yaml", """
        enums: { st: { a: A, b: B } }
        object_types:
          thing: { label: 东西, domain: knowledge, title: name, properties: { name: text!, status: "enum:st" } }
        action_types:
          go: { label: 走, mode: manual, target: thing, set: { status: $param.missing } }
    """)
    with pytest.raises(ontology.OntologyError, match="missing"):
        ontology.load(str(tmp_path))


def test_duplicate_definition_across_files(tmp_path):
    body = "object_types:\n  thing: { label: x, domain: knowledge, title: name, properties: { name: text } }\n"
    write(tmp_path, "a.yaml", body)
    write(tmp_path, "b.yaml", body)
    with pytest.raises(ontology.OntologyError, match="重复"):
        ontology.load(str(tmp_path))
