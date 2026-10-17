"""运维面冒烟（收据：2026-10-17 评审发现 cli/migrate/doctor/evaluate 零测试）+ 发布前加固回归。"""
import sys

import pytest


# ── cli 冒烟 ──────────────────────────────────────────
def test_cli_check(capsys, monkeypatch):
    from autobiz import cli
    monkeypatch.setattr(sys, "argv", ["autobiz", "check"])
    cli.main()
    assert "本体" in capsys.readouterr().out


def test_cli_status_and_q(capsys, monkeypatch):
    from autobiz import cli
    monkeypatch.setattr(sys, "argv", ["autobiz", "status"])
    cli.main()
    assert "对象数量" in capsys.readouterr().out
    monkeypatch.setattr(sys, "argv", ["autobiz", "q", "overview"])
    cli.main()
    assert "待办" in capsys.readouterr().out


def test_cli_unknown_command_exits(monkeypatch):
    from autobiz import cli
    monkeypatch.setattr(sys, "argv", ["autobiz", "nope"])
    with pytest.raises(SystemExit):
        cli.main()


# ── migrate 冒烟 ──────────────────────────────────────
def test_migrate_is_idempotent(conn):
    from autobiz import migrate
    assert migrate.pending(conn) == []
    assert migrate.migrate(conn) == []          # 已应用的不重复执行


# ── 角色提权限交互终端（2026-10-17 评审风险 1）──────────
class FakeStdin:
    def __init__(self, tty):
        self._tty = tty

    def isatty(self):
        return self._tty


def test_role_elevation_only_in_tty(monkeypatch):
    """AUTOBIZ_ROLE 自报 owner 只在交互终端生效；Agent 的非交互 shell 一律 agent，
    闸门无条件走审批码（不看 PIN 是否配置）。"""
    from autobiz import approve
    monkeypatch.setattr(sys, "stdin", FakeStdin(False))
    assert approve.resolve_role("owner") == "agent"
    assert approve.resolve_role("system") == "agent"
    monkeypatch.setattr(sys, "stdin", FakeStdin(True))
    assert approve.resolve_role("owner") == "owner"
    assert approve.resolve_role("agent") == "agent"    # agent 不受影响


# ── 成本闸门 fail-closed（2026-10-17 评审风险 3）───────
def test_budget_check_fails_closed(monkeypatch):
    """记账查询失败时预算闸门拒绝放行（此前 except: pass 静默放行 = fail-open）。"""
    from autobiz import db, llm

    def boom():
        raise RuntimeError("记账表挂了")

    monkeypatch.setattr(db, "connect", boom)
    with pytest.raises(RuntimeError, match="预算检查不可用"):
        llm._check_budget("test_task")


def test_usage_logging_failure_is_loud(monkeypatch, capsys):
    """用量记账失败不再静默：调用已发生不作废，但必须报到 stderr。"""
    from autobiz import db, llm

    def boom():
        raise RuntimeError("记账表挂了")

    monkeypatch.setattr(db, "connect", boom)
    llm._log_usage({"provider": "deepseek", "model": "m", "_task": "t"}, object())
    assert "记账失败" in capsys.readouterr().err


def test_overview_surfaces_broken_usage_table(conn, onto):
    """overview 的花费查询失败要露出错误，不静默吞掉。"""
    from autobiz import functions

    class Conn:
        def execute(self, sql, params=None):
            if "llm_usage" in sql:
                raise RuntimeError("记账表挂了")
            return conn.execute(sql, params)

    out = functions.overview(Conn(), onto)
    assert "记账表不可用" in out["本月模型花费"]
