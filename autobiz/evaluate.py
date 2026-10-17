"""抽取回归评测（ADR-0008 / model-quality.md 第三节）：对固定文档重跑 analyze()（只调模型、不写库），按规则判分。"""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

import yaml

from autobiz import db, extract, store
from autobiz.ontology import ROOT


def _names(data: dict, type_: str) -> list[dict]:
    key = extract.TYPES[type_]
    return [{**it, "_name": str(it.get(key, ""))} for it in data.get(type_, [])]


def check(case: dict, a: dict) -> list[str]:
    fails, data = [], a.get("data") or {}
    rel = a["relevance"]
    if "relevance_max" in case and rel > case["relevance_max"]:
        fails.append(f"相关度 {rel:.0f} > {case['relevance_max']}")
    if "relevance_min" in case and rel < case["relevance_min"]:
        fails.append(f"相关度 {rel:.0f} < {case['relevance_min']}")
    if case.get("no_case") and data.get("case"):
        fails.append(f"不应有案例，却抽出 {[c['_name'] for c in _names(data, 'case')]}")
    for r in case.get("forbid", []):
        hit = [x["_name"] for x in _names(data, r["type"])
               if (x["_name"] == r["name"] if r.get("exact") else r["name"] in x["_name"])]
        if hit:
            fails.append(f"不应出现 {r['type']}：{hit}")
    for r in case.get("require", []):
        if not any(r["name"].lower() in x["_name"].lower() for x in _names(data, r["type"])):
            fails.append(f"漏抽 {r['type']}「{r['name']}」")
    pains = _names(data, "pain_point")
    for r in case.get("pain_industry_forbid", []):
        bad = [p["_name"] for p in pains if r["pain"] in p["_name"] and p.get("industry") == r["industry"]]
        if bad:
            fails.append(f"痛点错连行业 {r['industry']}：{bad}")
    for r in case.get("pain_industry_require", []):
        if not any(r["pain"] in p["_name"] and p.get("industry") == r["industry"] for p in pains):
            fails.append(f"痛点「{r['pain']}」未连到 {r['industry']}")
    for r in case.get("pain_audience", []):
        bad = [p["_name"] for p in pains if (r["pain"] == "*" or r["pain"] in p["_name"]) and p.get("audience") != r["audience"]]
        if bad:
            fails.append(f"痛点 audience 应为 {r['audience']}：{bad}")
    for p in pains:
        if p.get("audience") not in ("customer", "creator"):
            fails.append(f"痛点缺 audience：{p['_name']}")
    return fails


def _fixture_doc(title: str) -> dict | None:
    """G-5：评测样本可放 evals/fixtures/<标题>.md，不依赖本地数据库（粉丝 / CI 可跑）。"""
    import re
    slug = re.sub(r"[^\w\u4e00-\u9fff]+", "-", title).strip("-")
    p = os.path.join(ROOT, "evals", "fixtures", f"{slug}.md")
    if not os.path.exists(p):
        return None
    content = open(p, encoding="utf-8").read()
    return {"id": f"eval_{slug}", "title": title, "props": {"content": content, "title": title}}


def run(workers: int = 8, repeat: int = 2) -> dict:
    spec = yaml.safe_load(open(os.path.join(ROOT, "evals", "extract_regression.yaml"), encoding="utf-8"))
    conn = db.connect()
    docs = []
    missing = []
    for c in spec["cases"]:
        d = _fixture_doc(c["doc"])                                        # 样本文件优先
        if d is None:
            r = conn.execute("SELECT id FROM objects WHERE type='source_doc' AND title LIKE %s LIMIT 1",
                             (c["doc"] + "%",)).fetchone()
            if not r:
                missing.append(c["doc"])
                continue
            d = store.get(conn, r["id"])
        docs.append((c, d))
    if missing:
        raise SystemExit(f"评测文档不存在（库里没有、evals/fixtures/ 也没有）：{missing}")

    def one(item):
        c, d = item
        wc = db.connect()
        try:
            return c, d, extract.analyze(wc, d)
        finally:
            wc.close()

    by_doc: dict[str, dict] = {c["doc"]: {"doc": c["doc"], "runs": 0, "passes": 0, "fails": []} for c, _ in docs}
    grounded = total = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for c, d, a in pool.map(one, docs * repeat):
            fails = check(c, a)
            for t in extract.TYPES:
                for it in (a.get("data") or {}).get(t, []):
                    total += 1
                    grounded += extract.quote_in_source(str(it.get("quote", "")), a["content"])
            r = by_doc[c["doc"]]
            r["runs"] += 1
            r["passes"] += not fails
            r["fails"] += [f for f in fails if f not in r["fails"]]
    results = [{**r, "pass": r["passes"] == r["runs"],
                "precision_fails": [f for f in r["fails"] if not f.startswith("漏抽")]} for r in by_doc.values()]
    # 上线门槛：准确性问题（抽错）任何一次都不允许；召回问题（漏抽）偶发可容忍
    gate = all(not r["precision_fails"] for r in results)
    return {"gate": gate, "passed": sum(r["pass"] for r in results), "total": len(results), "repeat": repeat,
            "quote_grounded": f"{grounded}/{total}", "results": results}
