"""End-to-end: spawn ``python -m sql_mcp`` and talk MCP over stdio, exactly like VS Code does."""

from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .conftest import COMBOS, PASSWORD, USER

EXPECTED_TOOLS = {
    "list_connections", "add_connection", "remove_connection", "test_connection", "execute_sql",
    "execute_procedure", "begin_transaction", "commit_transaction", "rollback_transaction", "transaction_status",
    "list_databases", "list_objects", "describe_table", "get_object_definition", "list_jobs", "get_job",
    "create_job", "update_job", "delete_job", "start_job", "stop_job", "add_job_step", "update_job_step",
    "delete_job_step", "add_job_schedule", "delete_job_schedule", "get_job_history", "list_running_jobs",
    "list_sessions", "kill_session",
}


def _env(**extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith("SQL_MCP_")}
    env.update(extra)
    return env


async def _session(env, fn):
    params = StdioServerParameters(command=sys.executable, args=["-m", "sql_mcp"], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await fn(session)


async def _call(session, name, **args):
    res = await session.call_tool(name, args)
    text = res.content[0].text
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def test_stdio_without_configuration_lists_tools_and_explains():
    async def scenario(s):
        tools = await s.list_tools()
        names = {t.name for t in tools.tools}
        assert EXPECTED_TOOLS <= names
        execute = next(t for t in tools.tools if t.name == "execute_sql")
        assert execute.annotations.destructiveHint is True
        assert next(t for t in tools.tools if t.name == "list_jobs").annotations.readOnlyHint is True
        res = await _call(s, "execute_sql", sql="SELECT 1")
        assert res["success"] is False and "SQL_MCP_SERVER" in res["errors"][0]["message"]
        assert (await _call(s, "list_connections"))["connections"] == []
    asyncio.run(_session(_env(), scenario))


def test_stdio_invalid_configuration_is_reported_not_crashing():
    async def scenario(s):
        res = await _call(s, "list_connections")
        assert res["success"] is False and "auth" in res["errors"][0]["message"]
    asyncio.run(_session(_env(SQL_MCP_SERVER="x", SQL_MCP_AUTH="kerberos"), scenario))


@pytest.mark.integration
@pytest.mark.parametrize("combo", COMBOS or [None], ids=[f"{t[0]}-{d}" for t, d in COMBOS] or ["no-server"])
def test_stdio_end_to_end(combo):
    if combo is None:
        pytest.skip("SQL_MCP_TEST_TARGETS not set")
    (name, host, port), driver = combo
    env = _env(SQL_MCP_SERVER=host, SQL_MCP_PORT=str(port), SQL_MCP_AUTH="sql", SQL_MCP_USER=USER,
               SQL_MCP_PASSWORD=PASSWORD, SQL_MCP_DRIVER=driver, SQL_MCP_NAME="e2e")
    job = f"sqlmcp e2e {driver}"

    async def scenario(s):
        info = await _call(s, "test_connection")
        assert info["success"] and info["server"]["connection"] == "e2e", info
        script = ("IF OBJECT_ID('tempdb..#e2e') IS NOT NULL DROP TABLE #e2e\nGO\n"
                  "CREATE TABLE #e2e (id int, ad nvarchar(20))\nGO\n"
                  "INSERT #e2e VALUES (1, N'Gökhan'), (2, N'Şule')\nGO\nSELECT * FROM #e2e ORDER BY id")
        res = await _call(s, "execute_sql", sql=script)
        assert res["success"] and res["result_sets"][0]["rows"] == [[1, "Gökhan"], [2, "Şule"]], res
        md = await _call(s, "execute_sql", sql="SELECT ad FROM #e2e WHERE id = ?", params=[2],
                         output_format="markdown")
        assert "| Şule |" in md
        res = await _call(s, "execute_procedure", procedure="sys.sp_executesql",
                          params={"stmt": "SELECT @x * 2 AS v", "params": "@x int", "x": 21})
        assert res["result_sets"][0]["rows"] == [[42]] and res["return_value"] == 0, res
        await _call(s, "execute_sql", sql="IF EXISTS (SELECT 1 FROM msdb.dbo.sysjobs WHERE name = ?) "
                                          "EXEC msdb.dbo.sp_delete_job @job_name = ?", params=[job, job])
        res = await _call(s, "create_job", job_name=job, steps=[{"name": "s1", "command": "SELECT 1"}],
                          schedules=[{"name": "every hour", "type": "daily", "repeat_every_hours": 1}])
        assert res["success"], res
        got = await _call(s, "get_job", job_name=job)
        assert got["schedules"][0]["freq_subday_type"] == 8
        assert (await _call(s, "delete_job", job_name=job))["success"]
        await _call(s, "begin_transaction")
        res = await _call(s, "execute_sql", sql="DELETE #e2e")
        assert res["open_transactions"] == 1
        await _call(s, "rollback_transaction")
        res = await _call(s, "execute_sql", sql="SELECT COUNT(*) FROM #e2e")
        assert res["result_sets"][0]["rows"] == [[2]]
        res = await _call(s, "execute_sql", sql="SELECT * FROM dbo.nope_e2e")
        assert res["success"] is False and "Invalid object name" in res["errors"][0]["message"]

    asyncio.run(_session(env, scenario))


def test_cli_check_and_version():
    import subprocess
    out = subprocess.run([sys.executable, "-m", "sql_mcp", "--version"], capture_output=True, text=True)
    assert out.returncode == 0 and "sql-mcp" in out.stdout
    out = subprocess.run([sys.executable, "-m", "sql_mcp", "--check"], capture_output=True, text=True, env=_env())
    assert out.returncode == 1 and "No connection configured" in out.stdout
    if COMBOS:
        (name, host, port), driver = COMBOS[0]
        out = subprocess.run([sys.executable, "-m", "sql_mcp", "--check"], capture_output=True, text=True,
                             env=_env(SQL_MCP_SERVER=host, SQL_MCP_PORT=str(port), SQL_MCP_USER=USER,
                                      SQL_MCP_PASSWORD=PASSWORD, SQL_MCP_DRIVER=driver))
        assert out.returncode == 0, out.stdout + out.stderr
        assert json.loads(out.stdout)["connections"]["default"]["success"] is True
