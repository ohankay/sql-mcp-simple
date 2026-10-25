"""Shared fixtures.

Integration tests run against every server in ``SQL_MCP_TEST_TARGETS`` x every installed driver::

    SQL_MCP_TEST_TARGETS="sql2022=localhost,14332;sql2017=localhost,14317"
    SQL_MCP_TEST_USER=sa  SQL_MCP_TEST_PASSWORD=...

Start the servers with ``docker compose -f tests/docker-compose.yml up -d``.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from sql_mcp import server
from sql_mcp.config import ConnectionProfile
from sql_mcp.drivers import _import_mssql_python, _import_pyodbc, pick_odbc_driver
from sql_mcp.session import SessionManager

TEST_DB = "sqlmcp_test"


def _targets() -> list[tuple[str, str, int]]:
    raw = os.environ.get("SQL_MCP_TEST_TARGETS", "")
    out = []
    for item in filter(None, (x.strip() for x in raw.split(";"))):
        name, _, addr = item.partition("=")
        host, _, port = addr.partition(",")
        out.append((name, host, int(port or 1433)))
    return out


def _drivers() -> list[str]:
    found = []
    pyodbc = _import_pyodbc()
    if pyodbc is not None and pick_odbc_driver(list(pyodbc.drivers())):
        found.append("pyodbc")
    if _import_mssql_python() is not None:
        found.append("mssql-python")
    return found


COMBOS = [(t, d) for t in _targets() for d in _drivers()]
USER = os.environ.get("SQL_MCP_TEST_USER", "sa")
PASSWORD = os.environ.get("SQL_MCP_TEST_PASSWORD", "")


def make_profile(target, driver, name="main", **overrides) -> ConnectionProfile:
    _, host, port = target
    kw = dict(name=name, server=host, port=port, auth="sql", user=USER, password=PASSWORD, driver=driver,
              database="master", login_timeout=10, query_timeout=120)
    kw.update(overrides)
    return ConnectionProfile(**kw)


def call(tool, **kwargs):
    """Invoke an MCP tool coroutine and decode its JSON (or return markdown text)."""
    text = asyncio.run(tool(**kwargs))
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


@pytest.fixture(scope="module", params=COMBOS or [None],
                ids=[f"{t[0]}-{d}" for t, d in COMBOS] or ["no-server"])
def ctx(request):
    if request.param is None:
        pytest.skip("SQL_MCP_TEST_TARGETS not set - integration tests skipped")
    target, driver = request.param
    mgr = SessionManager({"main": make_profile(target, driver)}, "main")
    server.set_manager(mgr)
    setup = f"""
IF DB_ID('{TEST_DB}') IS NOT NULL
BEGIN
    ALTER DATABASE [{TEST_DB}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE;
    DROP DATABASE [{TEST_DB}];
END
GO
CREATE DATABASE [{TEST_DB}];
"""
    res = call(server.execute_sql, sql=setup)
    assert res["success"], res
    yield {"target": target, "driver": driver, "mgr": mgr, "db": TEST_DB}
    mgr.close_all()
    server.set_manager(None)


@pytest.fixture
def sql(ctx):
    """Run SQL in the test database and assert success."""
    def run(text, **kw):
        kw.setdefault("database", TEST_DB)
        res = call(server.execute_sql, sql=text, **kw)
        assert res["success"], json.dumps(res.get("errors"), ensure_ascii=False)
        return res
    return run
