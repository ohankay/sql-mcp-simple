"""Unit tests - no SQL Server needed."""

from __future__ import annotations

import datetime as dt
import decimal
import json
import uuid

import pytest

from sql_mcp import jobs
from sql_mcp.config import ConfigError, ConnectionProfile, load_config_file, load_profiles, profile_from_env
from sql_mcp.drivers import build_connection_string, odbc_escape, pick_odbc_driver, redact
from sql_mcp.executor import clean_message, describe_error, to_jsonable
from sql_mcp.server import build_procedure_call
from sql_mcp.sqltext import (ReadOnlyViolation, check_readonly, quote_ident, quote_multipart, split_batches,
                             split_multipart)


# ---------------------------------------------------------------- GO splitting

@pytest.mark.parametrize("script,expected", [
    ("SELECT 1", ["SELECT 1"]),
    ("SELECT 1\nGO\nSELECT 2", ["SELECT 1", "SELECT 2"]),
    ("SELECT 1\r\ngo\r\nSELECT 2\r\n", ["SELECT 1", "SELECT 2"]),
    ("SELECT 1\n  Go  \nSELECT 2\nGO", ["SELECT 1", "SELECT 2"]),
    ("SELECT 1\nGO 3", ["SELECT 1"] * 3),
    ("SELECT 1\nGO -- comment\nSELECT 2", ["SELECT 1", "SELECT 2"]),
    ("GO\nGO\n", []),
    ("", []),
    ("   \n\t", []),
    ("SELECT 'a\nGO\nb'", ["SELECT 'a\nGO\nb'"]),                 # GO inside a string
    ("/* x\nGO\n*/ SELECT 1", ["/* x\nGO\n*/ SELECT 1"]),           # GO inside block comment
    ("/* a /* nested\nGO\n*/ still\nGO\n*/ SELECT 1\nGO\nSELECT 2",
     ["/* a /* nested\nGO\n*/ still\nGO\n*/ SELECT 1", "SELECT 2"]),
    ("SELECT [a\nGO\nb] FROM t", ["SELECT [a\nGO\nb] FROM t"]),     # bracketed identifier
    ('SELECT "a\nGO\nb"', ['SELECT "a\nGO\nb"']),
    ("SELECT 'it''s'\nGO\nSELECT 2", ["SELECT 'it''s'", "SELECT 2"]),
    ("-- GO in comment ' quote\nSELECT 1\nGO\nSELECT 2", ["-- GO in comment ' quote\nSELECT 1", "SELECT 2"]),
    ("SELECT 1 GO", ["SELECT 1 GO"]),                               # not on its own line
    ("GOTO lbl\nGO", ["GOTO lbl"]),
    ("SELECT 1 AS GO\nGO", ["SELECT 1 AS GO"]),
])
def test_split_batches(script, expected):
    assert split_batches(script) == expected


def test_split_batches_none():
    assert split_batches(None) == []


# ---------------------------------------------------------------- identifiers

def test_quote_ident():
    assert quote_ident("abc") == "[abc]"
    assert quote_ident("a]b") == "[a]]b]"
    with pytest.raises(ValueError):
        quote_ident("")
    with pytest.raises(ValueError):
        quote_ident("x" * 129)


@pytest.mark.parametrize("name,parts", [
    ("dbo.T", ["dbo", "T"]),
    ("T", ["T"]),
    ("[my db].[dbo].[T.x]", ["my db", "dbo", "T.x"]),
    ('"a"."b"', ["a", "b"]),
    ("[a]]b].c", ["a]b", "c"]),
    ("srv.db.dbo.t", ["srv", "db", "dbo", "t"]),
    ("db..t", ["db", "", "t"]),
])
def test_split_multipart(name, parts):
    assert split_multipart(name) == parts


@pytest.mark.parametrize("bad", ["", "   ", "a.b.c.d.e", "[unterminated", "dbo."])
def test_split_multipart_invalid(bad):
    with pytest.raises(ValueError):
        split_multipart(bad)


def test_quote_multipart_injection_is_neutralised():
    assert quote_multipart("dbo.x]; DROP TABLE y--") == "[dbo].[x]]; DROP TABLE y--]"


# ---------------------------------------------------------------- read-only guard

@pytest.mark.parametrize("sql", [
    "SELECT * FROM dbo.T",
    "WITH c AS (SELECT 1 a) SELECT * FROM c",
    "SELECT 'DELETE FROM x' AS txt",
    "-- DROP TABLE x\nSELECT 1",
    "/* UPDATE t */ SELECT 1",
    "SELECT [update], [insert] FROM t",
    "EXEC sp_who2",
    "sp_who2",
    "EXEC sys.sp_help 'dbo.T'",
    "EXEC dbo.sp_helptext N'dbo.v'",
    "DECLARE @x int = 1; SELECT @x",
    "SET NOCOUNT ON; SELECT 1",
    "SELECT 1\nGO\nEXEC sp_spaceused",
])
def test_readonly_allows(sql):
    check_readonly(sql)


@pytest.mark.parametrize("sql", [
    "INSERT INTO t VALUES (1)",
    "update t set a = 1",
    "DELETE t",
    "SELECT * INTO #x FROM t",
    "TRUNCATE TABLE t",
    "DROP TABLE t",
    "CREATE TABLE t (a int)",
    "ALTER DATABASE x SET OFFLINE",
    "EXEC dbo.DoWrites",
    "EXEC('DELETE t')",
    "EXEC sp_executesql N'DELETE t'",
    "DoWrites 1, 2",
    "SELECT 1\nGO\nDELETE t",
    "MERGE t USING s ON 1=1 WHEN MATCHED THEN DELETE;",
    "GRANT SELECT ON t TO u",
    "BACKUP DATABASE x TO DISK='x'",
    "DBCC FREEPROCCACHE",
    "KILL 55",
    "SELECT * FROM OPENROWSET('x','y','z')",
    "DECLARE @rc int; EXEC @rc = dbo.p",
])
def test_readonly_blocks(sql):
    with pytest.raises(ReadOnlyViolation):
        check_readonly(sql)


# ---------------------------------------------------------------- config

def _env(**kw):
    return {f"SQL_MCP_{k}": v for k, v in kw.items()}


def test_profile_from_env_sql_auth():
    p = profile_from_env(_env(SERVER="srv", USER="u", PASSWORD="p", PORT="1444", READONLY="true"))
    assert p.auth == "sql" and p.user == "u" and p.port == 1444 and p.readonly is True and p.name == "default"


def test_profile_from_env_infers_windows_auth():
    p = profile_from_env(_env(SERVER="srv\\INST"))
    assert p.auth == "windows" and p.user is None


def test_profile_from_env_ignores_unexpanded_placeholders():
    p = profile_from_env(_env(SERVER="srv", AUTH="windows", PASSWORD="${input:pw}"))
    assert p.password is None


def test_profile_from_env_none_without_server():
    assert profile_from_env({}) is None


@pytest.mark.parametrize("kwargs,msg", [
    (dict(server="", auth="windows"), "server"),
    (dict(server="s", auth="kerberos"), "auth"),
    (dict(server="s", auth="sql"), "user"),
    (dict(server="s", auth="sql", user="u"), "password"),
    (dict(server="s", auth="windows", driver="jdbc"), "driver"),
    (dict(server="s", auth="windows", encrypt="maybe"), "encrypt"),
    (dict(server="s", auth="windows", port=70000), "port"),
    (dict(server="s", auth="windows", port="abc"), "port"),
    (dict(server="s", auth="windows", max_rows=0), "max_rows"),
    (dict(server="s;x=1", auth="windows"), "server"),
    (dict(server="s", auth="windows", readonly="perhaps"), "readonly"),
])
def test_profile_validation_errors(kwargs, msg):
    with pytest.raises(ConfigError, match=msg):
        ConnectionProfile(name="x", **kwargs)


def test_auth_aliases():
    assert ConnectionProfile(name="x", server="s", auth="Integrated").auth == "windows"
    assert ConnectionProfile(name="x", server="s", auth="SQL_LOGIN", user="u", password="").auth == "sql"


def test_public_dict_hides_password():
    p = ConnectionProfile(name="x", server="s", auth="sql", user="u", password="secret")
    d = p.public_dict()
    assert "password" not in d and d["has_password"] is True
    assert "secret" not in repr(p) and "secret" not in json.dumps(d)


def test_config_file(tmp_path, monkeypatch):
    monkeypatch.setenv("PROD_PW", "pw1")
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"default": "prod", "connections": {
        "dev": {"server": "devsrv", "auth": "windows"},
        "prod": {"server": "prodsrv", "auth": "sql", "username": "app", "password_env": "PROD_PW",
                 "readonly": True},
    }}), encoding="utf-8")
    profiles, default = load_config_file(str(cfg))
    assert default == "prod" and profiles["prod"].password == "pw1" and profiles["prod"].readonly
    # env profile is merged and file default kept
    profiles, default = load_profiles({"SQL_MCP_CONFIG": str(cfg), "SQL_MCP_SERVER": "x", "SQL_MCP_NAME": "envp"})
    assert set(profiles) == {"dev", "prod", "envp"} and default == "prod"


@pytest.mark.parametrize("content,msg", [
    ("not json", "not valid JSON"),
    ("[]", "root"),
    ('{"connections": {}}', "non-empty"),
    ('{"connections": {"a": 5}}', "must be an object"),
    ('{"default": "zz", "connections": {"a": {"server": "s"}}}', "Default"),
    ('{"connections": {"a": {"server": "s", "bogus": 1}}}', "unknown setting"),
    ('{"connections": {"a": {"server": "s", "auth": "sql", "user": "u", "password_env": "NOPE_X"}}}', "NOPE_X"),
])
def test_config_file_errors(tmp_path, content, msg):
    cfg = tmp_path / "c.json"
    cfg.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigError, match=msg):
        load_config_file(str(cfg))


def test_config_file_missing():
    with pytest.raises(ConfigError, match="not found"):
        load_config_file("/nonexistent/x.json")


def test_config_file_with_bom(tmp_path):
    cfg = tmp_path / "c.json"
    cfg.write_bytes(b"\xef\xbb\xbf" + b'{"connections": {"a": {"server": "s"}}}')
    profiles, default = load_config_file(str(cfg))
    assert default is None and profiles["a"].auth == "windows"


# ---------------------------------------------------------------- connection strings

def test_odbc_escape():
    assert odbc_escape("abc") == "abc"
    assert odbc_escape("a;b") == "{a;b}"
    assert odbc_escape("a}b") == "{a}}b}"
    assert odbc_escape("") == "{}"
    assert odbc_escape(" x") == "{ x}"


def test_connection_string_windows_auth():
    p = ConnectionProfile(name="x", server="SRV\\INST", auth="windows", database="Db1")
    s = build_connection_string(p, "pyodbc", "ODBC Driver 18 for SQL Server")
    assert "Trusted_Connection=yes" in s and "UID" not in s and "PWD" not in s
    assert s.startswith("DRIVER={ODBC Driver 18 for SQL Server};Server=SRV\\INST;Database=Db1")
    assert "Encrypt=yes" in s and "TrustServerCertificate=yes" in s and "APP=sql-mcp" in s


def test_connection_string_sql_auth_special_password():
    p = ConnectionProfile(name="x", server="srv", port=1500, auth="sql", user="u", password="p;w}d=1")
    s = build_connection_string(p, "mssql-python")
    assert "Server=srv,1500" in s and "PWD={p;w}}d=1}" in s and "DRIVER" not in s and "APP" not in s
    assert "p;w" not in redact(s)


def test_connection_string_legacy_driver_has_no_encrypt_keywords():
    p = ConnectionProfile(name="x", server="srv", auth="windows")
    s = build_connection_string(p, "pyodbc", "SQL Server")
    assert "Encrypt" not in s


def test_connection_string_extra():
    p = ConnectionProfile(name="x", server="srv", auth="windows", extra="MultiSubnetFailover=yes;")
    assert build_connection_string(p, "mssql-python").endswith(";MultiSubnetFailover=yes")


def test_pick_odbc_driver():
    assert pick_odbc_driver(["SQL Server", "ODBC Driver 17 for SQL Server", "ODBC Driver 18 for SQL Server"]) \
        == "ODBC Driver 18 for SQL Server"
    assert pick_odbc_driver(["SQL Server Native Client 11.0", "SQL Server"]) == "SQL Server Native Client 11.0"
    assert pick_odbc_driver(["PostgreSQL"]) is None


# ---------------------------------------------------------------- serialisation & errors

def test_to_jsonable():
    assert to_jsonable(decimal.Decimal("1.50")) == "1.50"
    assert to_jsonable(decimal.Decimal("42")) == 42
    assert to_jsonable(decimal.Decimal("NaN")) == "NaN"
    assert to_jsonable(dt.datetime(2024, 1, 2, 3, 4, 5)) == "2024-01-02T03:04:05"
    assert to_jsonable(dt.date(2024, 1, 2)) == "2024-01-02"
    assert to_jsonable(b"\x01\xff") == "0x01FF"
    assert to_jsonable(b"\x00" * 5000).endswith("(+904 bytes)")
    u = uuid.uuid4()
    assert to_jsonable(u) == str(u).upper()
    assert to_jsonable(float("inf")) == "inf"
    assert to_jsonable(None) is None and to_jsonable(True) is True


def test_clean_message_and_describe_error():
    raw = ("[42S02] [Microsoft][ODBC Driver 18 for SQL Server][SQL Server]Invalid object name 'nope'. "
           "(208) (SQLExecDirectW)")
    info = describe_error(Exception("42S02", raw))
    assert info["message"] == "Invalid object name 'nope'."
    assert info["sql_error_number"] == 208 and info["sqlstate"] == "42S02"
    assert clean_message("Driver Error: Syntax; DDBC Error: [Microsoft][SQL Server]boom") == "boom"


# ---------------------------------------------------------------- procedures

def test_build_procedure_call_named_and_output():
    sql, values, outs = build_procedure_call("dbo.P", {"@a": 1, "b": "x", "io": 5},
                                             {"io": "int", "msg": "nvarchar(100)"})
    assert "DECLARE @__o_io int = ?;" in sql and "DECLARE @__o_msg nvarchar(100);" in sql
    assert "EXEC @__rc = [dbo].[P] @a = ?, @b = ?, @io = @__o_io OUTPUT, @msg = @__o_msg OUTPUT;" in sql
    assert values == [5, 1, "x"] and outs == ["io", "msg"]


def test_build_procedure_call_positional_and_none():
    sql, values, _ = build_procedure_call("P", [1, None], None)
    assert "EXEC @__rc = [P] ?, ?;" in sql and values == [1, None]
    sql, values, _ = build_procedure_call("P", None, None)
    assert "EXEC @__rc = [P];" in sql and values == []


@pytest.mark.parametrize("params,outs", [
    ({"a; DROP": 1}, None),
    (None, {"x": "int; DROP TABLE t"}),
    (None, {"bad name": "int"}),
    ("notadict", None),
    ({"a": 1, "@a": 2}, None),
])
def test_build_procedure_call_rejects_injection(params, outs):
    with pytest.raises(ValueError):
        build_procedure_call("dbo.P", params, outs)


# ---------------------------------------------------------------- agent job specs

def test_schedule_args_variants():
    a = jobs.schedule_args({"name": "n", "type": "daily", "start_time": "02:30"})
    assert a["freq_type"] == 4 and a["freq_interval"] == 1 and a["active_start_time"] == 23000
    a = jobs.schedule_args({"name": "n", "type": "weekly", "days": ["mon", "Friday", "cuma"], "every_n": 2})
    assert a["freq_interval"] == 2 | 32 and a["freq_recurrence_factor"] == 2
    a = jobs.schedule_args({"name": "n", "type": "weekly", "days": "sun,sat"})
    assert a["freq_interval"] == 65
    a = jobs.schedule_args({"name": "n", "type": "daily", "repeat_every_minutes": 15, "end_time": "18:00:00"})
    assert a["freq_subday_type"] == 4 and a["freq_subday_interval"] == 15 and a["active_end_time"] == 180000
    a = jobs.schedule_args({"name": "n", "type": "monthly", "day_of_month": 31})
    assert a["freq_type"] == 16 and a["freq_interval"] == 31
    a = jobs.schedule_args({"name": "n", "type": "once", "start_date": "2030-12-31", "start_time": "23:59"})
    assert a["active_start_date"] == 20301231 and a["active_start_time"] == 235900
    a = jobs.schedule_args({"name": "n", "type": "agent_start"})
    assert a["freq_type"] == 64 and "active_start_time" not in a
    a = jobs.schedule_args({"name": "n", "raw": {"freq_type": 4, "@freq_interval": 1}})
    assert a == {"name": "n", "freq_type": 4, "freq_interval": 1}


@pytest.mark.parametrize("spec,msg", [
    ({}, "name"),
    ({"name": "n", "type": "hourly"}, "type"),
    ({"name": "n", "type": "weekly"}, "days"),
    ({"name": "n", "type": "weekly", "days": ["funday"]}, "weekday"),
    ({"name": "n", "start_time": "25:00"}, "valid time"),
    ({"name": "n", "start_time": "noon"}, "HH:MM"),
    ({"name": "n", "type": "once", "start_time": "10:00"}, "start_date"),
    ({"name": "n", "type": "once", "start_date": "2030-02-30"}, "YYYY-MM-DD"),
    ({"name": "n", "repeat_every_minutes": 0}, "between"),
    ({"name": "n", "repeat_every_minutes": 5, "repeat_every_hours": 1}, "only one"),
    ({"name": "n", "type": "once", "start_date": "2030-01-01", "repeat_every_hours": 1}, "cannot repeat"),
    ({"name": "n", "bogus": 1}, "Unknown"),
    ({"name": "n", "type": "monthly", "day_of_month": 32}, "between"),
])
def test_schedule_args_errors(spec, msg):
    with pytest.raises(jobs.JobSpecError, match=msg):
        jobs.schedule_args(spec)


def test_step_args_defaults_and_actions():
    a = jobs.step_args({"name": "s", "command": "SELECT 1"}, is_last=False)
    assert a["subsystem"] == "TSQL" and a["database_name"] == "master" and a["on_success_action"] == 3
    a = jobs.step_args({"name": "s", "command": "x", "on_success": "goto:3", "on_fail": "quit_failure"})
    assert a["on_success_action"] == 4 and a["on_success_step_id"] == 3 and a["on_fail_action"] == 2
    with pytest.raises(jobs.JobSpecError):
        jobs.step_args({"name": "s"})
    with pytest.raises(jobs.JobSpecError):
        jobs.step_args({"name": "s", "command": "x", "on_success": "explode"})
    with pytest.raises(jobs.JobSpecError):
        jobs.step_args({"name": "s", "command": "x", "nope": 1})


def test_exec_proc_is_parameterised():
    sql, params = jobs.exec_proc("msdb.dbo.sp_x", {"a": "x'; DROP--", "b": None, "c": True})
    assert "x'; DROP" not in sql and params == ["x'; DROP--", 1] and "@b" not in sql
    with pytest.raises(jobs.JobSpecError):
        jobs.exec_proc("p", {"bad name;": 1})


def test_build_create_job_requires_steps():
    with pytest.raises(jobs.JobSpecError):
        jobs.build_create_job("j", [])
    with pytest.raises(jobs.JobSpecError):
        jobs.build_create_job(" ", [{"name": "s", "command": "x"}])
    sql, params = jobs.build_create_job("j", [{"name": "a", "command": "x"}, {"name": "b", "command": "y"}],
                                        schedules=[{"name": "d"}])
    assert sql.count("EXEC @rc = msdb.dbo.sp_add_jobstep ") == 2 and "sp_add_jobschedule" in sql and "sp_add_jobserver" in sql
    assert "BEGIN TRANSACTION" in sql and "THROW" in sql


def test_build_update_requires_change():
    with pytest.raises(jobs.JobSpecError):
        jobs.build_update_job("j")
    with pytest.raises(jobs.JobSpecError):
        jobs.build_update_step("j", 1, {})


# ---------------------------------------------------------------- driver resolution

class _FakePyodbc:
    def __init__(self, drivers):
        self._drivers = drivers

    def drivers(self):
        return self._drivers


@pytest.mark.parametrize("requested,odbc,has_mssql,expected", [
    ("auto", ["ODBC Driver 18 for SQL Server"], True, ("pyodbc", "ODBC Driver 18 for SQL Server")),
    ("auto", ["SQL Server"], True, ("mssql-python", None)),
    ("auto", ["SQL Server"], False, ("pyodbc", "SQL Server")),
    ("auto", [], True, ("mssql-python", None)),
    ("mssql-python", ["ODBC Driver 18 for SQL Server"], True, ("mssql-python", None)),
    ("pyodbc", ["ODBC Driver 17 for SQL Server"], True, ("pyodbc", "ODBC Driver 17 for SQL Server")),
    ("pyodbc", ["SQL Server"], True, ("pyodbc", "SQL Server")),
])
def test_resolve_driver(monkeypatch, requested, odbc, has_mssql, expected):
    from sql_mcp import drivers
    monkeypatch.setattr(drivers, "_import_pyodbc", lambda: _FakePyodbc(odbc))
    monkeypatch.setattr(drivers, "_import_mssql_python", lambda: object() if has_mssql else None)
    p = ConnectionProfile(name="x", server="s", auth="windows", driver=requested)
    assert drivers.resolve_driver(p) == expected


@pytest.mark.parametrize("requested,odbc,has_mssql", [
    ("pyodbc", [], True), ("auto", [], False), ("mssql-python", ["ODBC Driver 18 for SQL Server"], False),
])
def test_resolve_driver_unavailable(monkeypatch, requested, odbc, has_mssql):
    from sql_mcp import drivers
    monkeypatch.setattr(drivers, "_import_pyodbc", lambda: _FakePyodbc(odbc))
    monkeypatch.setattr(drivers, "_import_mssql_python", lambda: object() if has_mssql else None)
    with pytest.raises(drivers.DriverUnavailableError):
        drivers.resolve_driver(ConnectionProfile(name="x", server="s", auth="windows", driver=requested))


def test_resolve_driver_mssql_python_missing_message(monkeypatch):
    from sql_mcp import drivers
    monkeypatch.setattr(drivers, "_import_pyodbc", lambda: _FakePyodbc(["ODBC Driver 17 for SQL Server"]))
    monkeypatch.setattr(drivers, "_import_mssql_python", lambda: None)
    with pytest.raises(drivers.DriverUnavailableError, match="driver=mssql-python requested.*driver=auto"):
        drivers.resolve_driver(ConnectionProfile(name="x", server="s", auth="windows", driver="mssql-python"))
    # auto still works through the ODBC driver
    p = ConnectionProfile(name="x", server="s", auth="windows")
    assert drivers.resolve_driver(p) == ("pyodbc", "ODBC Driver 17 for SQL Server")


def test_resolve_driver_explicit_odbc_name(monkeypatch):
    from sql_mcp import drivers
    monkeypatch.setattr(drivers, "_import_pyodbc", lambda: _FakePyodbc([]))
    p = ConnectionProfile(name="x", server="s", auth="windows", odbc_driver="SQL Server")
    assert drivers.resolve_driver(p) == ("pyodbc", "SQL Server")


def test_turkish_weekday_names():
    a = jobs.schedule_args({"name": "n", "type": "weekly",
                            "days": ["pzt", "sali", "carsamba", "persembe", "cuma", "Cumartesi", "Pazar"]})
    assert a["freq_interval"] == 127
    a = jobs.schedule_args({"name": "n", "type": "weekly", "days": "Salı, Çarşamba"})
    assert a["freq_interval"] == 4 | 8


def test_readme_job_example_builds():
    sql, params = jobs.build_create_job("Gece ETL", [
        {"name": "Yükle", "command": "EXEC dbo.LoadStage", "database": "Dwh", "retry_attempts": 2, "retry_interval": 5},
        {"name": "Birleştir", "command": "EXEC dbo.MergeFacts", "database": "Dwh", "on_fail": "quit_failure"},
    ], schedules=[
        {"name": "Her gece 02:30", "type": "daily", "start_time": "02:30"},
        {"name": "Mesai içi 15 dk", "type": "weekly", "days": ["pzt", "sali", "carsamba", "persembe", "cuma"],
         "repeat_every_minutes": 15, "start_time": "08:00", "end_time": "18:00"},
    ])
    assert "Gece ETL" in params and 62 in params


# ---------------------------------------------------------------- Windows bundle files

def test_bundle_templates_are_valid():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "bundle"
    for tpl in (root / "templates").glob("*.json"):
        data = json.loads(tpl.read_text(encoding="utf-8"))
        if tpl.name.startswith("mcp."):
            sql = data["servers"]["sql"]
            assert sql["type"] == "stdio" and sql["command"].endswith("sql-mcp.exe")
            assert "SQL_MCP_PASSWORD" not in sql["env"] or sql["env"]["SQL_MCP_PASSWORD"] == "${input:sql-password}"
    assert (root / "templates" / "sql-dba.agent.md").read_text(encoding="utf-8").startswith("---")


def test_install_cmd_uses_crlf_and_offline_install():
    from pathlib import Path
    raw = (Path(__file__).resolve().parent.parent / "bundle" / "install.cmd").read_bytes()
    assert raw.count(b"\r\n") == raw.count(b"\n")  # cmd.exe needs CRLF line endings
    assert b"--no-index" in raw and b"wheelhouse" in raw
