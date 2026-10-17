import os

import psycopg
import pytest

os.environ["AUTOBIZ_FAKE_EMBED"] = "1"
TEST = os.environ.get("AUTOBIZ_TEST_DSN", "postgresql://autobiz:autobiz@127.0.0.1:5433/autobiz_test")
ADMIN = TEST.rsplit("/", 1)[0] + "/postgres"
# C-3 后 LLM 缓存 / 审批走独立连接（db.connect()），必须与行动连接同库——
# 测试进程内强制 DATABASE_URL 指向测试库，防止任何旁路连接打到真实库。
os.environ["DATABASE_URL"] = TEST


@pytest.fixture(autouse=True)
def _isolate_files(tmp_path, monkeypatch):
    """测试不得写真实工作目录（G-2）：工作区、方案落盘、视频项目一律改到 tmp。"""
    from autobiz import presales
    monkeypatch.setenv("AUTOBIZ_HOME", str(tmp_path))
    monkeypatch.setattr(presales, "PROPOSAL_DIR", str(tmp_path / "proposals"))
    try:                                                # 视频板块为可选组件（发布物不含）
        from autobiz import video
        monkeypatch.setattr(video, "PROJECTS", str(tmp_path))
    except ImportError:
        pass


@pytest.fixture(scope="session")
def onto():
    from autobiz import ontology
    return ontology.load()


@pytest.fixture(scope="session")
def _testdb(onto):
    with psycopg.connect(ADMIN, autocommit=True) as c:
        if not c.execute("SELECT 1 FROM pg_database WHERE datname='autobiz_test'").fetchone():
            c.execute("CREATE DATABASE autobiz_test")
    from autobiz import db
    conn = db.connect(TEST)
    db.init(conn, onto)
    conn.close()


@pytest.fixture
def conn(_testdb):
    from autobiz import db
    from pgvector.psycopg import register_vector
    c = db.connect(TEST)
    register_vector(c)
    c.execute("TRUNCATE objects, links, action_log, llm_cache, approvals, approver_failures, dup_candidates CASCADE")
    c.commit()
    yield c
    c.close()


@pytest.fixture
def act(conn, onto):
    from autobiz import actions

    def run(name, role="owner", confirm=True, actor="测试", **params):
        return actions.run(conn, onto, name, params, actor=actor, role=role, confirm=confirm)
    return run
