"""本体存储（ADR-0006 / 0009）：通用对象表 + 关系表 + 行动日志表，外加按对象类型编译出的只读视图。

通用表让本体演进不需要数据库迁移；视图（v_<类型>）给人和外部工具一张看得懂的表。
schema 变更走 autobiz/migrations/（F-1）；视图编译用 psycopg.sql 组装（I-2），枚举标签不再拼 SQL。
"""
from __future__ import annotations

import os

import psycopg
from dotenv import load_dotenv
from pgvector.psycopg import register_vector
from psycopg import sql
from psycopg.rows import dict_row

from autobiz.ontology import ROOT, Ontology

load_dotenv(os.path.join(ROOT, ".env"))


def url() -> str:
    return os.environ.get("DATABASE_URL", "postgresql://autobiz:autobiz@127.0.0.1:5433/autobiz")


def connect(dsn: str | None = None) -> psycopg.Connection:
    conn = psycopg.connect(dsn or url(), row_factory=dict_row, autocommit=False)
    return conn


def init(conn: psycopg.Connection, onto: Ontology) -> None:
    from autobiz.migrate import migrate
    applied = migrate(conn)
    if applied:
        print(f"✓ 应用迁移 {applied}")
    register_vector(conn)
    stale = [r["viewname"] for r in conn.execute("SELECT viewname FROM pg_views WHERE schemaname='public' AND viewname LIKE 'v\\_%'")
             if r["viewname"][2:] not in onto.object_types]
    for v in stale:                                   # 本体删掉的类型，视图一并清理
        conn.execute(sql.SQL("DROP VIEW IF EXISTS {}").format(sql.Identifier(v)))
    for ddl in view_ddl(onto):
        conn.execute(ddl)
    conn.commit()


def view_ddl(onto: Ontology) -> list:
    """每个对象类型编译一张只读视图 v_<类型>，属性展开为列，枚举展开为中文（psycopg.sql 组装，I-2）。"""
    out = []
    for t in onto.object_types.values():
        cols = [sql.SQL("o.id"), sql.SQL("o.title"), sql.SQL("o.verified"), sql.SQL("o.origin")]
        for k, pt in t.properties.items():
            if k == "title":
                continue
            key, alias = sql.Literal(k), sql.Identifier(k)
            if pt.base == "enum" and not pt.is_list:
                whens = [sql.SQL("WHEN {} THEN {}").format(sql.Literal(key_), sql.Literal(label))
                         for key_, label in onto.enums[pt.enum].items()]
                case = sql.SQL("CASE o.props->>{} {} ELSE o.props->>{} END").format(key, sql.SQL(" ").join(whens), key)
                cols.append(sql.SQL("{} AS {}").format(case, alias))
            elif pt.base == "int" and not pt.is_list:
                cols.append(sql.SQL("(o.props->>{})::bigint AS {}").format(key, alias))
            elif pt.base == "number" and not pt.is_list:
                cols.append(sql.SQL("(o.props->>{})::numeric AS {}").format(key, alias))
            elif pt.is_list or pt.base == "json":
                cols.append(sql.SQL("o.props->{} AS {}").format(key, alias))
            else:
                cols.append(sql.SQL("o.props->>{} AS {}").format(key, alias))
        cols += [sql.SQL("o.created_at"), sql.SQL("o.updated_at")]
        # 先 DROP 再 CREATE：CREATE OR REPLACE VIEW 不允许在中间插列 / 改列名，
        # 给已有类型加属性（本体演进的常态，ADR-0009）会让 init 直接失败。
        out.append(sql.SQL("DROP VIEW IF EXISTS {}").format(sql.Identifier(f"v_{t.name}")))
        out.append(sql.SQL("CREATE VIEW {} AS SELECT {} FROM objects o WHERE o.type = {}").format(
            sql.Identifier(f"v_{t.name}"), sql.SQL(", ").join(cols), sql.Literal(t.name)))
    return out
