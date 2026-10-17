"""编号迁移（F-1）：`autobiz migrate` 幂等执行 autobiz/migrations/*.sql，已应用的记入 schema_migrations。

规则：0001_baseline 是原建表语句（对已有库幂等）；此后每个需要改 schema 的版本新增一个编号 SQL，
发布 CHANGELOG 标注包含哪些迁移。升级前应先备份。
"""
from __future__ import annotations

import glob
import os

import psycopg

HERE = os.path.dirname(__file__)


def pending(conn: psycopg.Connection) -> list[str]:
    _ensure_table(conn)
    done = {r["version"] for r in conn.execute("SELECT version FROM schema_migrations")}
    return [os.path.basename(f)[:4] for f in sorted(glob.glob(os.path.join(HERE, "migrations", "*.sql")))
            if os.path.basename(f)[:4] not in done]


def migrate(conn: psycopg.Connection) -> list[str]:
    _ensure_table(conn)
    done = {r["version"] for r in conn.execute("SELECT version FROM schema_migrations")}
    applied = []
    for path in sorted(glob.glob(os.path.join(HERE, "migrations", "*.sql"))):
        v = os.path.basename(path)[:4]
        if v in done:
            continue
        with conn.transaction():          # 每个迁移一个保存点，失败即回滚到该迁移前
            conn.execute(open(path, encoding="utf-8").read())
            conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (v,))
        applied.append(v)
        print(f"✓ 迁移 {os.path.basename(path)}")
    conn.commit()
    return applied


def _ensure_table(conn: psycopg.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations "
                 "(version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
    conn.commit()
