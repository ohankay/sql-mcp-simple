"""MCP server exposing SQL Server tools over stdio."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from . import jobs
from .config import ConfigError, load_profiles, profile_from_mapping
from .drivers import DriverUnavailableError
from .executor import describe_error
from .session import SessionError, SessionManager
from .sqltext import ReadOnlyViolation, quote_ident, quote_multipart, split_multipart

log = logging.getLogger("sql_mcp")

INSTRUCTIONS = """\
Microsoft SQL Server (2017+) tools. Every tool takes an optional `connection` (profile name, see
list_connections) - omit it to use the default profile. Connections are persistent, so temp tables,
SET options and open transactions survive between calls on the same connection.
- execute_sql runs any T-SQL (SELECT/DML/DDL/admin). Scripts may contain GO separators.
  Use `params` with ? placeholders for values instead of string concatenation.
- For CREATE/ALTER PROCEDURE/VIEW/FUNCTION/TRIGGER put the CREATE in its own batch (GO before/after).
- Results come back as JSON: result_sets[{columns, rows}], rows_affected, messages, errors.
- Wrap risky multi-statement changes in begin_transaction ... commit_transaction / rollback_transaction.
- SQL Server Agent jobs: list_jobs, get_job, create_job, update_job, delete_job, start_job, stop_job,
  add/update/delete_job_step, add/delete_job_schedule, get_job_history, list_running_jobs.
"""

mcp = FastMCP("sql-mcp", instructions=INSTRUCTIONS)
_manager: SessionManager | None = None
_EXPECTED_ERRORS = (ConfigError, ReadOnlyViolation, jobs.JobSpecError, SessionError, DriverUnavailableError,
                    ValueError)


def manager() -> SessionManager:
    global _manager
    if _manager is None:
        profiles, default = load_profiles()
        _manager = SessionManager(profiles, default)
    return _manager


def set_manager(mgr: SessionManager | None) -> None:
    global _manager
    _manager = mgr


def _dump(result: Any) -> str:
    return json.dumps(result, ensure_ascii=False, default=str, separators=(",", ":"))


def _error(exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, _EXPECTED_ERRORS):
        return {"success": False, "errors": [{"type": type(exc).__name__, "message": str(exc)}]}
    log.debug("tool error", exc_info=exc)
    return {"success": False, "errors": [describe_error(exc)]}


async def _call(fn: Callable[[], Any]) -> str:
    def guarded() -> Any:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - reported to the model as a structured error
            return _error(exc)

    return _dump(await anyio.to_thread.run_sync(guarded))


def _run_sql(connection: str | None, sql: str, params: list[Any] | None = None, *, database: str | None = None,
             max_rows: int | None = None, continue_on_error: bool = False, timeout: int | None = None,
             enforce_readonly: bool = False) -> dict[str, Any]:
    sess = manager().get(connection)
    return sess.execute(sql, params=params, database=database, max_rows=max_rows,
                        continue_on_error=continue_on_error, timeout=timeout, enforce_readonly=enforce_readonly)


def _require_writable(connection: str | None) -> None:
    prof = manager().profile(connection)
    if prof.readonly:
        raise ReadOnlyViolation(f"Connection '{prof.name}' is read-only")


def _first_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    if not result.get("result_sets"):
        return []
    rs = result["result_sets"][0]
    return [dict(zip(rs["columns"], row)) for row in rs["rows"]]


def _markdown(result: dict[str, Any]) -> str:
    lines: list[str] = []
    for i, rs in enumerate(result.get("result_sets", []), start=1):
        if len(result["result_sets"]) > 1:
            lines.append(f"**Result set {i}** ({rs['row_count']} rows)")
        cols = [str(c).replace("|", "\\|") for c in rs["columns"]]
        lines.append("| " + " | ".join(cols) + " |")
        lines.append("|" + "---|" * len(cols))
        for row in rs["rows"]:
            cells = ["NULL" if v is None else str(v).replace("|", "\\|").replace("\n", " ") for v in row]
            lines.append("| " + " | ".join(cells) + " |")
        if rs.get("note"):
            lines.append(f"_{rs['note']}_")
        lines.append("")
    if result.get("rows_affected"):
        lines.append("Rows affected: " + ", ".join(str(n) for n in result["rows_affected"]))
    for m in result.get("messages", []):
        lines.append(f"> {m}")
    for e in result.get("errors", []):
        lines.append(f"**ERROR**: {e.get('message')}" + (f" (#{e['sql_error_number']})" if "sql_error_number" in e else ""))
    if result.get("warning"):
        lines.append(f"**WARNING**: {result['warning']}")
    return "\n".join(lines).strip() or "OK (no results)"


_READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
_DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False)


def tool(annotations: ToolAnnotations):
    def deco(fn):
        return mcp.tool(annotations=annotations, structured_output=False)(fn)
    return deco


# ====================================================================== connections

@tool(_READ)
async def list_connections() -> str:
    """List configured SQL Server connection profiles (passwords are never shown)."""
    return await _call(lambda: {"default": manager().default, "connections": manager().describe()})


@tool(_WRITE)
async def add_connection(name: str, server: str, auth: str = "windows", user: str | None = None,
                         password: str | None = None, password_env: str | None = None, database: str = "master",
                         port: int | None = None, encrypt: str = "yes", trust_server_certificate: bool = True,
                         readonly: bool = False, make_default: bool = False, driver: str = "auto",
                         query_timeout: int = 300, max_rows: int = 1000) -> str:
    """Register (or replace) a connection profile at runtime and test it.

    auth: 'windows' = the current Windows logon (integrated/trusted), 'sql' = SQL login (user + password).
    Prefer password_env (name of an environment variable) over passing a password in chat.
    server: 'host', 'host\\INSTANCE' or 'host,port'.
    """
    def run():
        data: dict[str, Any] = {"server": server, "auth": auth, "database": database, "encrypt": encrypt,
                                "trust_server_certificate": trust_server_certificate, "readonly": readonly,
                                "driver": driver, "query_timeout": query_timeout, "max_rows": max_rows}
        if port:
            data["port"] = port
        if user:
            data["user"] = user
        if password is not None:
            data["password"] = password
        if password_env:
            data["password_env"] = password_env
        prof = profile_from_mapping(name, data)
        manager().add(prof, make_default=make_default)
        info = _server_info(name)
        info["added"] = name
        return info
    return await _call(run)


@tool(_WRITE)
async def remove_connection(name: str) -> str:
    """Close and forget a connection profile."""
    def run():
        manager().remove(name)
        return {"success": True, "removed": name, "default": manager().default}
    return await _call(run)


_SERVER_INFO_SQL = """
DECLARE @auth nvarchar(40), @agent nvarchar(40) = N'Unknown (needs VIEW SERVER STATE)';
IF HAS_PERMS_BY_NAME(NULL, NULL, 'VIEW SERVER STATE') = 1
BEGIN
    SELECT @auth = auth_scheme FROM sys.dm_exec_connections WHERE session_id = @@SPID;
    SET @agent = CASE WHEN EXISTS (SELECT 1 FROM sys.sysprocesses WHERE program_name LIKE N'SQLAgent%')
                      THEN N'Running' ELSE N'Not running' END;
END
SELECT @@SERVERNAME AS server_name,
       CAST(SERVERPROPERTY('ProductVersion') AS nvarchar(128)) AS product_version,
       CAST(SERVERPROPERTY('ProductLevel') AS nvarchar(128)) AS product_level,
       CAST(SERVERPROPERTY('ProductUpdateLevel') AS nvarchar(128)) AS update_level,
       CAST(SERVERPROPERTY('Edition') AS nvarchar(128)) AS edition,
       CASE LEFT(CAST(SERVERPROPERTY('ProductVersion') AS nvarchar(128)), 2)
            WHEN '14' THEN 'SQL Server 2017' WHEN '15' THEN 'SQL Server 2019' WHEN '16' THEN 'SQL Server 2022'
            WHEN '17' THEN 'SQL Server 2025' WHEN '13' THEN 'SQL Server 2016' WHEN '12' THEN 'SQL Server 2014'
            ELSE 'SQL Server' END AS product,
       SUSER_SNAME() AS login_name, USER_NAME() AS database_user, DB_NAME() AS current_database,
       @auth AS auth_scheme, IS_SRVROLEMEMBER('sysadmin') AS is_sysadmin, @@SPID AS session_id,
       @agent AS sql_agent;
"""


def _server_info(connection: str | None) -> dict[str, Any]:
    result = _run_sql(connection, _SERVER_INFO_SQL)
    if not result["success"]:
        return result
    sess = manager().get(connection)
    info = _first_rows(result)[0]
    info["connection"] = sess.profile.name
    info["driver"] = sess.driver_label
    info["readonly_profile"] = sess.profile.readonly
    return {"success": True, "server": info}


@tool(_READ)
async def test_connection(connection: str | None = None) -> str:
    """Connect and return server/version/login info (SQL 2017/2019/2022 ...), auth scheme and Agent status."""
    return await _call(lambda: _server_info(connection))


# ====================================================================== query execution

@tool(_DESTRUCTIVE)
async def execute_sql(sql: str, connection: str | None = None, database: str | None = None,
                      params: list[Any] | None = None, max_rows: int | None = None,
                      continue_on_error: bool = False, timeout_seconds: int | None = None,
                      output_format: str = "json") -> str:
    """Execute any T-SQL: SELECT, INSERT/UPDATE/DELETE/MERGE, DDL (CREATE/ALTER/DROP), EXEC, DBCC, BACKUP, ...

    - Scripts may contain `GO` batch separators (`GO n` repeats). CREATE PROCEDURE/VIEW/FUNCTION/TRIGGER
      must be alone in its batch.
    - params: values for `?` placeholders (single batch only), e.g. sql="SELECT * FROM t WHERE id = ?", params=[5].
    - database: run in this database (the connection's previous database is restored afterwards).
    - max_rows: per result set (default from the profile, 1000). continue_on_error: keep running later batches.
    - output_format: 'json' (default) or 'markdown'.
    Returns result_sets, rows_affected, messages (PRINT / info), errors, open transaction warnings.
    """
    def run():
        if output_format not in ("json", "markdown"):
            raise ValueError("output_format must be 'json' or 'markdown'")
        result = _run_sql(connection, sql, params, database=database, max_rows=max_rows,
                          continue_on_error=continue_on_error, timeout=timeout_seconds, enforce_readonly=True)
        return result
    if output_format == "markdown":
        text = await _call(run)
        data = json.loads(text)
        return _markdown(data) if "result_sets" in data or "errors" in data else text
    return await _call(run)


_PARAM_RE = re.compile(r"^@?[A-Za-z_][A-Za-z0-9_]{0,127}$")
_TYPE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_ ]{0,40}(\(\s*(\d{1,4}|max)\s*(,\s*\d{1,2}\s*)?\))?$", re.IGNORECASE)


def build_procedure_call(procedure: str, params: dict[str, Any] | list[Any] | None,
                         output_params: dict[str, str] | None) -> tuple[str, list[Any], list[str]]:
    proc = quote_multipart(procedure)
    output_params = output_params or {}
    if params is not None and not isinstance(params, (dict, list)):
        raise ValueError("params must be an object {name: value} or a list of positional values")
    if not isinstance(output_params, dict):
        raise ValueError("output_params must be an object {name: sql_type}")
    declares: list[str] = []
    declare_values: list[Any] = []
    args: list[str] = []
    exec_values: list[Any] = []
    named: dict[str, Any] = {}
    if isinstance(params, list):
        args.extend("?" for _ in params)
        exec_values.extend(params)
    elif params:
        for name, value in params.items():
            if not _PARAM_RE.match(str(name)):
                raise ValueError(f"Invalid parameter name {name!r}")
            bare = str(name).lstrip("@")
            if bare.lower() in named:
                raise ValueError(f"Duplicate parameter {name!r}")
            named[bare.lower()] = (bare, value)
    out_names = {str(k).lstrip("@").lower() for k in output_params}
    for key, (bare, value) in named.items():
        if key not in out_names:
            args.append(f"@{bare} = ?")
            exec_values.append(value)
    outs: list[str] = []
    for name, sqltype in output_params.items():
        if not _PARAM_RE.match(str(name)):
            raise ValueError(f"Invalid output parameter name {name!r}")
        if not _TYPE_RE.match(str(sqltype).strip()):
            raise ValueError(f"Invalid SQL type {sqltype!r} for output parameter {name!r}")
        bare = str(name).lstrip("@")
        var = f"@__o_{bare}"
        decl = f"DECLARE {var} {str(sqltype).strip()}"
        if bare.lower() in named:  # INPUT/OUTPUT parameter
            decl += " = ?"
            declare_values.append(named[bare.lower()][1])
        declares.append(decl + ";")
        args.append(f"@{bare} = {var} OUTPUT")
        outs.append(bare)
    select_cols = ", ".join(["@__rc AS [return_value]"] + [f"@__o_{o} AS {quote_ident(o)}" for o in outs])
    sql = ("DECLARE @__rc int;\n" + "".join(d + "\n" for d in declares)
           + f"EXEC @__rc = {proc}" + (" " + ", ".join(args) if args else "") + ";\n"
           + f"SELECT {select_cols};")
    return sql, declare_values + exec_values, outs


@tool(_DESTRUCTIVE)
async def execute_procedure(procedure: str, params: dict[str, Any] | list[Any] | None = None,
                            output_params: dict[str, str] | None = None, connection: str | None = None,
                            database: str | None = None, max_rows: int | None = None) -> str:
    """Execute a stored procedure with named (or positional) parameters and capture OUTPUT params + return value.

    procedure: 'dbo.MyProc' or '[db].[schema].[proc]'.
    params: {"CustomerId": 5, "Name": "x"} (the @ is optional) or a positional list.
    output_params: {"Total": "decimal(18,2)", "Msg": "nvarchar(4000)"} - a key also present in params is INPUT/OUTPUT.
    """
    def run():
        sql, values, outs = build_procedure_call(procedure, params, output_params)
        result = _run_sql(connection, sql, values, database=database, max_rows=max_rows, enforce_readonly=True)
        sets = result.get("result_sets", [])
        if result.get("success") and sets and sets[-1]["columns"][:1] == ["return_value"]:
            last = sets.pop()
            row = dict(zip(last["columns"], last["rows"][0])) if last["rows"] else {}
            result["return_value"] = row.pop("return_value", None)
            if outs:
                result["output_params"] = row
        return result
    return await _call(run)


# ====================================================================== transactions

@tool(_WRITE)
async def begin_transaction(connection: str | None = None, isolation_level: str | None = None) -> str:
    """BEGIN TRANSACTION on the persistent connection. Later execute_sql calls run inside it until commit/rollback.
    isolation_level (optional): READ UNCOMMITTED | READ COMMITTED | REPEATABLE READ | SNAPSHOT | SERIALIZABLE.
    """
    def run():
        sql = ""
        if isolation_level:
            level = " ".join(isolation_level.upper().replace("_", " ").split())
            if level not in ("READ UNCOMMITTED", "READ COMMITTED", "REPEATABLE READ", "SNAPSHOT", "SERIALIZABLE"):
                raise ValueError(f"Unsupported isolation level {isolation_level!r}")
            sql = f"SET TRANSACTION ISOLATION LEVEL {level};\n"
        result = _run_sql(connection, sql + "BEGIN TRANSACTION;")
        result.pop("warning", None)
        return result
    return await _call(run)


@tool(_WRITE)
async def commit_transaction(connection: str | None = None) -> str:
    """COMMIT all open transactions on the connection."""
    return await _call(lambda: _run_sql(connection, "WHILE @@TRANCOUNT > 0 COMMIT TRANSACTION;"))


@tool(_WRITE)
async def rollback_transaction(connection: str | None = None) -> str:
    """ROLLBACK the open transaction on the connection (no-op when none is open)."""
    return await _call(lambda: _run_sql(connection, "IF @@TRANCOUNT > 0 ROLLBACK TRANSACTION;"))


@tool(_READ)
async def transaction_status(connection: str | None = None) -> str:
    """Show @@TRANCOUNT, XACT_STATE() and isolation level of the connection."""
    sql = """SELECT @@TRANCOUNT AS trancount, XACT_STATE() AS xact_state,
    CASE transaction_isolation_level WHEN 1 THEN 'READ UNCOMMITTED' WHEN 2 THEN 'READ COMMITTED'
      WHEN 3 THEN 'REPEATABLE READ' WHEN 4 THEN 'SERIALIZABLE' WHEN 5 THEN 'SNAPSHOT' END AS isolation_level
    FROM sys.dm_exec_sessions WHERE session_id = @@SPID;"""
    def run():
        result = _run_sql(connection, sql)
        result.pop("warning", None)
        return result
    return await _call(run)


# ====================================================================== schema exploration

@tool(_READ)
async def list_databases(connection: str | None = None) -> str:
    """List databases with state, recovery model, compatibility level and size (MB)."""
    sql = """SELECT d.name, d.database_id, d.state_desc AS state, d.recovery_model_desc AS recovery_model,
       d.compatibility_level, d.collation_name, d.create_date, d.is_read_only,
       CAST(SUM(CAST(mf.size AS bigint)) * 8 / 1024.0 AS decimal(18,2)) AS size_mb
FROM sys.databases d LEFT JOIN sys.master_files mf ON mf.database_id = d.database_id
GROUP BY d.name, d.database_id, d.state_desc, d.recovery_model_desc, d.compatibility_level, d.collation_name,
         d.create_date, d.is_read_only
ORDER BY d.name;"""
    return await _call(lambda: _run_sql(connection, sql, max_rows=10000))


_OBJECT_TYPES = {
    "table": ("U",), "view": ("V",), "procedure": ("P", "PC"), "function": ("FN", "IF", "TF", "FS", "FT"),
    "trigger": ("TR",), "synonym": ("SN",), "sequence": ("SO",), "type": ("TT",),
}


@tool(_READ)
async def list_objects(connection: str | None = None, database: str | None = None,
                       object_type: str | None = None, schema: str | None = None,
                       name_like: str | None = None, max_rows: int = 2000) -> str:
    """List user objects. object_type: table|view|procedure|function|trigger|synonym|sequence (omit = all).
    name_like uses LIKE syntax, e.g. 'Customer%'."""
    def run():
        codes = None
        if object_type:
            key = object_type.strip().lower().rstrip("s")
            key = {"proc": "procedure", "sp": "procedure", "fn": "function", "tbl": "table"}.get(key, key)
            if key not in _OBJECT_TYPES:
                raise ValueError(f"object_type must be one of {', '.join(_OBJECT_TYPES)}")
            codes = ",".join(_OBJECT_TYPES[key])
        sql = """DECLARE @types nvarchar(100) = ?, @schema sysname = ?, @name nvarchar(256) = ?;
SELECT s.name AS [schema], o.name, o.type_desc AS type, o.create_date, o.modify_date,
       CASE WHEN o.type = 'U' THEN (SELECT SUM(p.rows) FROM sys.partitions p
                                    WHERE p.object_id = o.object_id AND p.index_id IN (0, 1)) END AS row_count
FROM sys.objects o JOIN sys.schemas s ON s.schema_id = o.schema_id
WHERE o.is_ms_shipped = 0 AND o.type IN ('U','V','P','PC','FN','IF','TF','FS','FT','TR','SN','SO','TT')
  AND (@types IS NULL OR CHARINDEX(',' + RTRIM(o.type) + ',', ',' + @types + ',') > 0)
  AND (@schema IS NULL OR s.name = @schema)
  AND (@name IS NULL OR o.name LIKE @name)
ORDER BY o.type_desc, s.name, o.name;"""
        return _run_sql(connection, sql, [codes, schema, name_like], database=database, max_rows=max_rows)
    return await _call(run)


@tool(_READ)
async def describe_table(table: str, connection: str | None = None, database: str | None = None) -> str:
    """Columns (type, nullability, identity, default, computed), primary key, indexes, foreign keys and
    row count of a table or view. table: 'dbo.Customers' or 'Customers'."""
    def run():
        parts = split_multipart(table)
        db = database
        if len(parts) >= 3:
            db = db or parts[-3]
            parts = parts[-2:]
        name = ".".join(quote_ident(p) for p in parts)
        sql = """DECLARE @name nvarchar(600) = ?;
DECLARE @id int = OBJECT_ID(@name);
IF @id IS NULL OR OBJECTPROPERTY(@id, 'IsTable') = 0 AND OBJECTPROPERTY(@id, 'IsView') = 0
BEGIN DECLARE @db sysname = DB_NAME(); RAISERROR('Table or view %s was not found in database %s', 16, 1, @name, @db); RETURN; END;
SELECT c.column_id, c.name AS column_name,
       TYPE_NAME(c.user_type_id) + CASE
          WHEN TYPE_NAME(c.user_type_id) IN ('varchar','char','varbinary','binary')
               THEN '(' + CASE WHEN c.max_length = -1 THEN 'max' ELSE CAST(c.max_length AS varchar(10)) END + ')'
          WHEN TYPE_NAME(c.user_type_id) IN ('nvarchar','nchar')
               THEN '(' + CASE WHEN c.max_length = -1 THEN 'max' ELSE CAST(c.max_length / 2 AS varchar(10)) END + ')'
          WHEN TYPE_NAME(c.user_type_id) IN ('decimal','numeric')
               THEN '(' + CAST(c.precision AS varchar(5)) + ',' + CAST(c.scale AS varchar(5)) + ')'
          WHEN TYPE_NAME(c.user_type_id) IN ('datetime2','time','datetimeoffset')
               THEN '(' + CAST(c.scale AS varchar(5)) + ')'
          ELSE '' END AS data_type,
       c.is_nullable, c.is_identity, c.is_computed, dc.definition AS default_value,
       cc.definition AS computed_definition, c.collation_name
FROM sys.columns c
LEFT JOIN sys.default_constraints dc ON dc.object_id = c.default_object_id
LEFT JOIN sys.computed_columns cc ON cc.object_id = c.object_id AND cc.column_id = c.column_id
WHERE c.object_id = @id ORDER BY c.column_id;
SELECT i.name AS index_name, i.type_desc, i.is_primary_key, i.is_unique, i.is_disabled,
       STUFF((SELECT ', ' + COL_NAME(ic.object_id, ic.column_id) + CASE WHEN ic.is_descending_key = 1 THEN ' DESC' ELSE '' END
              FROM sys.index_columns ic WHERE ic.object_id = i.object_id AND ic.index_id = i.index_id AND ic.is_included_column = 0
              ORDER BY ic.key_ordinal FOR XML PATH('')), 1, 2, '') AS key_columns,
       STUFF((SELECT ', ' + COL_NAME(ic.object_id, ic.column_id)
              FROM sys.index_columns ic WHERE ic.object_id = i.object_id AND ic.index_id = i.index_id AND ic.is_included_column = 1
              FOR XML PATH('')), 1, 2, '') AS included_columns, i.filter_definition
FROM sys.indexes i WHERE i.object_id = @id AND i.type > 0 ORDER BY i.is_primary_key DESC, i.name;
SELECT fk.name AS fk_name, COL_NAME(fkc.parent_object_id, fkc.parent_column_id) AS column_name,
       OBJECT_SCHEMA_NAME(fkc.referenced_object_id) + '.' + OBJECT_NAME(fkc.referenced_object_id) AS references_table,
       COL_NAME(fkc.referenced_object_id, fkc.referenced_column_id) AS references_column,
       fk.delete_referential_action_desc AS on_delete, fk.update_referential_action_desc AS on_update
FROM sys.foreign_keys fk JOIN sys.foreign_key_columns fkc ON fkc.constraint_object_id = fk.object_id
WHERE fk.parent_object_id = @id ORDER BY fk.name, fkc.constraint_column_id;
SELECT SUM(p.rows) AS row_count FROM sys.partitions p WHERE p.object_id = @id AND p.index_id IN (0, 1);"""
        result = _run_sql(connection, sql, [name], database=db, max_rows=5000)
        if result.get("success") and len(result.get("result_sets", [])) == 4:
            cols, idx, fks, cnt = result.pop("result_sets")
            result["table"] = table
            result["columns"] = [dict(zip(cols["columns"], r)) for r in cols["rows"]]
            result["indexes"] = [dict(zip(idx["columns"], r)) for r in idx["rows"]]
            result["foreign_keys"] = [dict(zip(fks["columns"], r)) for r in fks["rows"]]
            result["row_count"] = cnt["rows"][0][0] if cnt["rows"] else None
        return result
    return await _call(run)


@tool(_READ)
async def get_object_definition(name: str, connection: str | None = None, database: str | None = None) -> str:
    """Return the T-SQL source of a procedure, view, function or trigger (like sp_helptext, but complete)."""
    def run():
        parts = split_multipart(name)
        db = database
        if len(parts) >= 3:
            db = db or parts[-3]
            parts = parts[-2:]
        qname = ".".join(quote_ident(p) for p in parts)
        sql = """DECLARE @name nvarchar(600) = ?;
DECLARE @id int = OBJECT_ID(@name);
IF @id IS NULL BEGIN DECLARE @db sysname = DB_NAME(); RAISERROR('Object %s was not found in database %s', 16, 1, @name, @db); RETURN; END;
SELECT OBJECT_SCHEMA_NAME(@id) AS [schema], OBJECT_NAME(@id) AS name,
       (SELECT type_desc FROM sys.objects WHERE object_id = @id) AS type,
       OBJECT_DEFINITION(@id) AS definition,
       CAST(OBJECTPROPERTY(@id, 'IsEncrypted') AS int) AS is_encrypted;"""
        result = _run_sql(connection, sql, [qname], database=db)
        rows = _first_rows(result)
        if result.get("success") and rows:
            row = rows[0]
            result.pop("result_sets")
            result.update(row)
            if row["definition"] is None:
                result["note"] = ("Object is encrypted" if row["is_encrypted"] else
                                  "No T-SQL definition (tables have none - use describe_table)")
        return result
    return await _call(run)


# ====================================================================== SQL Server Agent jobs

def _job_call(connection: str | None, built: tuple[str, list[Any]]) -> dict[str, Any]:
    _require_writable(connection)
    sql, params = built
    return _run_sql(connection, sql, params)


@tool(_READ)
async def list_jobs(connection: str | None = None, name_like: str | None = None,
                    enabled: bool | None = None) -> str:
    """List SQL Server Agent jobs with enabled flag, running state, last outcome and next run time."""
    params = [name_like, None if enabled is None else int(enabled)]
    return await _call(lambda: _run_sql(connection, jobs.LIST_JOBS_SQL, params, max_rows=5000))


@tool(_READ)
async def get_job(job_name: str, connection: str | None = None) -> str:
    """Job details: properties, steps (with commands) and schedules."""
    def run():
        result = _run_sql(connection, jobs.GET_JOB_SQL, [job_name])
        if result.get("success") and len(result.get("result_sets", [])) == 3:
            job, steps, scheds = result.pop("result_sets")
            result["job"] = dict(zip(job["columns"], job["rows"][0]))
            result["steps"] = [dict(zip(steps["columns"], r)) for r in steps["rows"]]
            result["schedules"] = [dict(zip(scheds["columns"], r)) for r in scheds["rows"]]
        return result
    return await _call(run)


@tool(_WRITE)
async def create_job(job_name: str, steps: list[dict[str, Any]], schedules: list[dict[str, Any]] | None = None,
                     description: str | None = None, enabled: bool = True, category: str | None = None,
                     owner_login: str | None = None, connection: str | None = None) -> str:
    """Create an Agent job with steps and optional schedules in ONE transaction (all or nothing).

    steps: [{"name": "Step 1", "command": "EXEC dbo.DoWork", "database": "MyDb",
             "subsystem": "TSQL" (default) | "CmdExec" | "PowerShell" | "SSIS",
             "on_success": "next"|"quit_success"|"quit_failure"|"goto:2", "on_fail": ..., "retry_attempts": 0,
             "retry_interval": 0, "output_file": null, "raw": {any extra sp_add_jobstep parameter}}]
    schedules: [{"name": "Nightly", "type": "daily"|"weekly"|"monthly"|"once"|"agent_start"|"idle",
                 "start_time": "02:30", "every_n": 1, "days": ["mon","fri"] (weekly), "day_of_month": 1 (monthly),
                 "repeat_every_minutes": 15 | "repeat_every_hours": 1 | "repeat_every_seconds": 30,
                 "end_time": "23:59", "start_date": "2026-01-31", "end_date": ..., "enabled": true,
                 "raw": {extra sp_add_jobschedule params}}]
    Default on_success: 'next' for every step except the last ('quit_success').
    """
    return await _call(lambda: _job_call(connection, jobs.build_create_job(
        job_name, steps, schedules=schedules, description=description, enabled=enabled, category=category,
        owner_login=owner_login)))


@tool(_WRITE)
async def update_job(job_name: str, new_name: str | None = None, description: str | None = None,
                     enabled: bool | None = None, category: str | None = None, owner_login: str | None = None,
                     start_step_id: int | None = None, connection: str | None = None) -> str:
    """Rename / enable / disable / change description, category, owner or start step of a job."""
    return await _call(lambda: _job_call(connection, jobs.build_update_job(
        job_name, new_name=new_name, description=description, enabled=enabled, category=category,
        owner_login=owner_login, start_step_id=start_step_id)))


@tool(_DESTRUCTIVE)
async def delete_job(job_name: str, delete_history: bool = True, connection: str | None = None) -> str:
    """Delete an Agent job (and by default its history)."""
    return await _call(lambda: _job_call(connection, jobs.build_delete_job(job_name, delete_history)))


@tool(_WRITE)
async def start_job(job_name: str, step_name: str | None = None, connection: str | None = None) -> str:
    """Start a job asynchronously (optionally from a given step). Check progress with list_running_jobs /
    get_job_history."""
    return await _call(lambda: _job_call(connection, jobs.build_start_job(job_name, step_name)))


@tool(_WRITE)
async def stop_job(job_name: str, connection: str | None = None) -> str:
    """Stop a running job."""
    return await _call(lambda: _job_call(connection, jobs.build_stop_job(job_name)))


@tool(_WRITE)
async def add_job_step(job_name: str, step: dict[str, Any], connection: str | None = None) -> str:
    """Add a step to an existing job. Same step fields as create_job; set "step_id" to insert at a position."""
    return await _call(lambda: _job_call(connection, jobs.build_add_step(job_name, step)))


@tool(_WRITE)
async def update_job_step(job_name: str, step_id: int, changes: dict[str, Any],
                          connection: str | None = None) -> str:
    """Change fields of a job step (name, command, database, subsystem, on_success, on_fail, retries, ...)."""
    return await _call(lambda: _job_call(connection, jobs.build_update_step(job_name, step_id, changes)))


@tool(_DESTRUCTIVE)
async def delete_job_step(job_name: str, step_id: int, connection: str | None = None) -> str:
    """Delete one job step (step_id from get_job)."""
    return await _call(lambda: _job_call(connection, jobs.build_delete_step(job_name, step_id)))


@tool(_WRITE)
async def add_job_schedule(job_name: str, schedule: dict[str, Any], connection: str | None = None) -> str:
    """Attach a new schedule to a job. Same schedule fields as create_job."""
    return await _call(lambda: _job_call(connection, jobs.build_add_schedule(job_name, schedule)))


@tool(_DESTRUCTIVE)
async def delete_job_schedule(job_name: str, schedule_name: str, connection: str | None = None) -> str:
    """Detach a schedule from a job (and delete it if no other job uses it)."""
    return await _call(lambda: _job_call(connection, jobs.build_delete_schedule(job_name, schedule_name)))


@tool(_READ)
async def get_job_history(job_name: str | None = None, top: int = 50, failed_only: bool = False,
                          connection: str | None = None) -> str:
    """Recent job/step executions with status, duration and messages (newest first)."""
    def run():
        n = int(top)
        if n < 1 or n > 10000:
            raise ValueError("top must be between 1 and 10000")
        return _run_sql(connection, jobs.JOB_HISTORY_SQL, [n, job_name, int(failed_only)], max_rows=n)
    return await _call(run)


@tool(_READ)
async def list_running_jobs(connection: str | None = None) -> str:
    """Jobs currently executing, with elapsed time and current step."""
    return await _call(lambda: _run_sql(connection, jobs.RUNNING_JOBS_SQL))


# ====================================================================== server administration

@tool(_READ)
async def list_sessions(connection: str | None = None, only_active: bool = True,
                        include_system: bool = False) -> str:
    """Server sessions with login, host, program, database, status, blocking session, wait, CPU and current SQL."""
    sql = """DECLARE @only_active bit = ?, @include_system bit = ?;
SELECT s.session_id, s.login_name, s.host_name, s.program_name, DB_NAME(s.database_id) AS database_name,
       s.status, r.command, r.blocking_session_id, r.wait_type, r.wait_time AS wait_ms,
       r.cpu_time AS cpu_ms, r.total_elapsed_time AS elapsed_ms, s.open_transaction_count,
       SUBSTRING(t.text, 1, 4000) AS sql_text
FROM sys.dm_exec_sessions s
LEFT JOIN sys.dm_exec_requests r ON r.session_id = s.session_id
OUTER APPLY sys.dm_exec_sql_text(r.sql_handle) t
WHERE (@include_system = 1 OR s.is_user_process = 1)
  AND (@only_active = 0 OR r.session_id IS NOT NULL OR s.open_transaction_count > 0)
  AND s.session_id <> @@SPID
ORDER BY r.blocking_session_id DESC, r.total_elapsed_time DESC;"""
    return await _call(lambda: _run_sql(connection, sql, [int(only_active), int(include_system)], max_rows=5000))


@tool(_DESTRUCTIVE)
async def kill_session(session_id: int, connection: str | None = None) -> str:
    """KILL a server session (requires ALTER ANY CONNECTION). Cannot kill this tool's own session."""
    def run():
        _require_writable(connection)
        spid = int(session_id)
        if spid <= 0:
            raise ValueError("session_id must be a positive integer")
        own = _first_rows(_run_sql(connection, "SELECT @@SPID AS spid"))
        if own and own[0]["spid"] == spid:
            raise ValueError("Refusing to kill this MCP server's own session")
        return _run_sql(connection, f"KILL {spid};")
    return await _call(run)


def _shutdown() -> None:
    if _manager is not None:
        _manager.close_all()


def run_stdio() -> None:
    try:
        mcp.run("stdio")
    finally:
        _shutdown()
