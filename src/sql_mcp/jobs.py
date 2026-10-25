"""SQL Server Agent job management - builds parameterised msdb calls.

Every builder returns ``(sql, params)``; values are always bound as parameters, never concatenated.
"""

from __future__ import annotations

import datetime as _dt
import re
from typing import Any

_PARAM_NAME_RE = re.compile(r"^@?[A-Za-z_][A-Za-z0-9_]{0,127}$")

FREQ_TYPES = {"once": 1, "daily": 4, "weekly": 8, "monthly": 16, "agent_start": 64, "idle": 128}
WEEKDAYS = {"sun": 1, "mon": 2, "tue": 4, "wed": 8, "thu": 16, "fri": 32, "sat": 64}
_WEEKDAY_ALIASES = {
    "sunday": "sun", "monday": "mon", "tuesday": "tue", "wednesday": "wed", "thursday": "thu",
    "friday": "fri", "saturday": "sat",
    "pazar": "sun", "pazartesi": "mon", "sali": "tue", "salı": "tue", "carsamba": "wed", "çarşamba": "wed",
    "persembe": "thu", "perşembe": "thu", "cuma": "fri", "cumartesi": "sat",
    "paz": "sun", "pzt": "mon", "sal": "tue", "car": "wed", "çar": "wed", "crs": "wed", "per": "thu",
    "prs": "thu", "cum": "fri", "cmt": "sat",
}
STEP_ACTIONS = {"quit_success": 1, "quit_failure": 2, "next": 3, "goto": 4}


class JobSpecError(ValueError):
    pass


def _param_name(name: str) -> str:
    if not _PARAM_NAME_RE.match(name):
        raise JobSpecError(f"Invalid procedure parameter name: {name!r}")
    return name if name.startswith("@") else "@" + name


def _norm(value: Any) -> Any:
    if isinstance(value, bool):
        return int(value)
    return value


def exec_proc(proc: str, args: dict[str, Any], *, rc_label: str | None = None) -> tuple[str, list[Any]]:
    """``EXEC @rc = proc @a = ?, ...; IF @rc <> 0 RAISERROR`` for the non-None args."""
    parts: list[str] = []
    params: list[Any] = []
    for key, value in args.items():
        if value is None:
            continue
        if isinstance(value, _Raw):
            parts.append(f"{_param_name(key)} = {value.sql}")
            continue
        parts.append(f"{_param_name(key)} = ?")
        params.append(_norm(value))
    label = (rc_label or proc).replace("'", "''")
    sql = (f"EXEC @rc = {proc} " + ", ".join(parts) + ";\n"
           f"IF @rc <> 0 RAISERROR('{label} failed (return code %d)', 16, 1, @rc);")
    return sql, params


class _Raw:
    def __init__(self, sql: str):
        self.sql = sql


JOB_ID_VAR = _Raw("@job_id")


def wrap_transaction(statements: list[tuple[str, list[Any]]], tail: str = "") -> tuple[str, list[Any]]:
    body = "\n".join(s for s, _ in statements)
    params = [p for _, ps in statements for p in ps]
    sql = (
        "DECLARE @rc int, @job_id uniqueidentifier;\n"
        "BEGIN TRY\n  BEGIN TRANSACTION;\n" + body + "\n  COMMIT TRANSACTION;\nEND TRY\n"
        "BEGIN CATCH\n  IF @@TRANCOUNT > 0 ROLLBACK TRANSACTION;\n  THROW;\nEND CATCH;\n" + tail
    )
    return sql, params


def wrap_single(statement: tuple[str, list[Any]]) -> tuple[str, list[Any]]:
    sql, params = statement
    return "DECLARE @rc int;\n" + sql, params


# ---------------------------------------------------------------- spec parsing

def _parse_action(value: Any, field: str) -> tuple[int | None, int | None]:
    if value is None:
        return None, None
    if isinstance(value, int) and not isinstance(value, bool):
        if value not in (1, 2, 3):
            raise JobSpecError(f"{field}: numeric action must be 1, 2 or 3 (use 'goto:N' for go-to-step)")
        return value, None
    text = str(value).strip().lower().replace(" ", "_").replace("-", "_")
    if text.startswith("goto"):
        m = re.match(r"^goto[:_]?(\d+)$", text)
        if not m:
            raise JobSpecError(f"{field}: use 'goto:<step_id>'")
        return 4, int(m.group(1))
    aliases = {"success": "quit_success", "quit_with_success": "quit_success", "failure": "quit_failure",
               "quit_with_failure": "quit_failure", "next_step": "next", "go_to_next_step": "next"}
    text = aliases.get(text, text)
    if text not in STEP_ACTIONS or text == "goto":
        raise JobSpecError(f"{field}: must be one of next, quit_success, quit_failure, goto:N - got {value!r}")
    return STEP_ACTIONS[text], None


def _int(value: Any, field: str, lo: int | None = None, hi: int | None = None) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise JobSpecError(f"{field} must be an integer, got {value!r}") from None
    if (lo is not None and result < lo) or (hi is not None and result > hi):
        raise JobSpecError(f"{field} must be between {lo} and {hi}, got {result}")
    return result


def _raw_args(spec: dict[str, Any]) -> dict[str, Any]:
    raw = spec.get("raw") or {}
    if not isinstance(raw, dict):
        raise JobSpecError("'raw' must be an object of sp parameter -> value")
    return {k.lstrip("@"): v for k, v in raw.items()}


_STEP_KEYS = {"name", "step_name", "command", "subsystem", "database", "database_name", "on_success",
              "on_fail", "on_failure", "retry_attempts", "retry_interval", "output_file", "step_id",
              "proxy_name", "database_user", "raw"}


def step_args(spec: dict[str, Any], *, is_last: bool = True, require_all: bool = True) -> dict[str, Any]:
    if not isinstance(spec, dict):
        raise JobSpecError("Each step must be an object")
    unknown = set(spec) - _STEP_KEYS
    if unknown:
        raise JobSpecError(f"Unknown step field(s): {', '.join(sorted(unknown))}")
    name = spec.get("name") or spec.get("step_name")
    command = spec.get("command")
    if require_all:
        if not name:
            raise JobSpecError("Step 'name' is required")
        if command is None or str(command).strip() == "":
            raise JobSpecError(f"Step '{name}': 'command' is required")
    args: dict[str, Any] = {}
    if spec.get("step_id") is not None:
        args["step_id"] = _int(spec["step_id"], "step_id", 1)
    if name:
        args["step_name"] = name
    if require_all or spec.get("subsystem"):
        args["subsystem"] = spec.get("subsystem") or "TSQL"
    if command is not None:
        args["command"] = command
    db = spec.get("database") or spec.get("database_name")
    if db or require_all:
        args["database_name"] = db or "master"
    ok_default = None if not require_all else ("quit_success" if is_last else "next")
    ok_action, ok_step = _parse_action(spec.get("on_success", ok_default), "on_success")
    fail_action, fail_step = _parse_action(spec.get("on_fail", spec.get("on_failure")), "on_fail")
    if ok_action is not None:
        args["on_success_action"] = ok_action
        if ok_step is not None:
            args["on_success_step_id"] = ok_step
    if fail_action is not None:
        args["on_fail_action"] = fail_action
        if fail_step is not None:
            args["on_fail_step_id"] = fail_step
    if spec.get("retry_attempts") is not None:
        args["retry_attempts"] = _int(spec["retry_attempts"], "retry_attempts", 0)
    if spec.get("retry_interval") is not None:
        args["retry_interval"] = _int(spec["retry_interval"], "retry_interval", 0)
    if spec.get("output_file"):
        args["output_file_name"] = spec["output_file"]
    if spec.get("proxy_name"):
        args["proxy_name"] = spec["proxy_name"]
    if spec.get("database_user"):
        args["database_user_name"] = spec["database_user"]
    args.update(_raw_args(spec))
    return args


def _parse_time(value: Any, field: str) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return _int(value, field, 0, 235959)
    m = re.match(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$", str(value).strip())
    if not m:
        raise JobSpecError(f"{field} must be HH:MM or HH:MM:SS, got {value!r}")
    h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
    if h > 23 or mi > 59 or s > 59:
        raise JobSpecError(f"{field} is not a valid time: {value!r}")
    return h * 10000 + mi * 100 + s


def _parse_date(value: Any, field: str) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        text = str(value)
    else:
        text = str(value).strip().replace("-", "")
    try:
        d = _dt.datetime.strptime(text, "%Y%m%d").date()
    except ValueError:
        raise JobSpecError(f"{field} must be YYYY-MM-DD, got {value!r}") from None
    return d.year * 10000 + d.month * 100 + d.day


_SCHEDULE_KEYS = {"name", "type", "frequency", "every", "every_n", "days", "day_of_month", "start_time",
                  "end_time", "start_date", "end_date", "repeat_every_seconds", "repeat_every_minutes",
                  "repeat_every_hours", "enabled", "raw"}


def schedule_args(spec: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(spec, dict):
        raise JobSpecError("schedule must be an object")
    unknown = set(spec) - _SCHEDULE_KEYS
    if unknown:
        raise JobSpecError(f"Unknown schedule field(s): {', '.join(sorted(unknown))}")
    name = spec.get("name")
    if not name:
        raise JobSpecError("schedule 'name' is required")
    args: dict[str, Any] = {"name": name}
    raw = _raw_args(spec)
    kind = spec.get("type") or spec.get("frequency")
    if kind is None and "freq_type" in raw:
        args.update(raw)
        return args
    kind = str(kind or "daily").strip().lower()
    if kind not in FREQ_TYPES:
        raise JobSpecError(f"schedule type must be one of {', '.join(FREQ_TYPES)} - got {kind!r}")
    args["enabled"] = 1 if spec.get("enabled", True) else 0
    args["freq_type"] = FREQ_TYPES[kind]
    every = spec.get("every_n", spec.get("every"))
    if kind == "daily":
        args["freq_interval"] = _int(every or 1, "every_n", 1, 365)
    elif kind == "weekly":
        days = spec.get("days")
        if not days:
            raise JobSpecError("weekly schedule needs 'days', e.g. ['mon','wed','fri']")
        if isinstance(days, str):
            days = [d for d in re.split(r"[,\s]+", days) if d]
        mask = 0
        for d in days:
            key = str(d).strip().lower()
            key = _WEEKDAY_ALIASES.get(key, key[:3])
            if key not in WEEKDAYS:
                raise JobSpecError(f"Unknown weekday {d!r}")
            mask |= WEEKDAYS[key]
        args["freq_interval"] = mask
        args["freq_recurrence_factor"] = _int(every or 1, "every_n", 1, 100)
    elif kind == "monthly":
        args["freq_interval"] = _int(spec.get("day_of_month", 1), "day_of_month", 1, 31)
        args["freq_recurrence_factor"] = _int(every or 1, "every_n", 1, 100)
    if kind in ("once", "daily", "weekly", "monthly"):
        repeat = [(k, spec.get(k)) for k in ("repeat_every_seconds", "repeat_every_minutes", "repeat_every_hours")
                  if spec.get(k) is not None]
        if len(repeat) > 1:
            raise JobSpecError("Use only one of repeat_every_seconds/minutes/hours")
        if repeat and kind == "once":
            raise JobSpecError("A 'once' schedule cannot repeat")
        if repeat:
            key, val = repeat[0]
            limits = {"repeat_every_seconds": (2, 10, 59), "repeat_every_minutes": (4, 1, 59),
                      "repeat_every_hours": (8, 1, 23)}
            sub_type, lo, hi = limits[key]
            args["freq_subday_type"] = sub_type
            args["freq_subday_interval"] = _int(val, key, lo, hi)
        else:
            args["freq_subday_type"] = 1
        args["active_start_time"] = _parse_time(spec.get("start_time", "00:00"), "start_time")
        if spec.get("end_time") is not None:
            args["active_end_time"] = _parse_time(spec["end_time"], "end_time")
    if spec.get("start_date") is not None:
        args["active_start_date"] = _parse_date(spec["start_date"], "start_date")
    elif kind == "once":
        raise JobSpecError("A 'once' schedule needs 'start_date' (YYYY-MM-DD) and 'start_time'")
    if spec.get("end_date") is not None:
        args["active_end_date"] = _parse_date(spec["end_date"], "end_date")
    args.update(raw)
    return args


# ---------------------------------------------------------------- builders

def build_create_job(job_name: str, steps: list[dict[str, Any]], *, schedules: list[dict[str, Any]] | None = None,
                     description: str | None = None, enabled: bool = True, category: str | None = None,
                     owner_login: str | None = None, server_name: str = "(local)",
                     start_step_id: int | None = None) -> tuple[str, list[Any]]:
    if not job_name or not str(job_name).strip():
        raise JobSpecError("job_name is required")
    if not steps:
        raise JobSpecError("At least one step is required")
    stmts = [exec_proc("msdb.dbo.sp_add_job", {
        "job_name": job_name, "enabled": enabled, "description": description, "category_name": category,
        "owner_login_name": owner_login, "start_step_id": start_step_id,
        "job_id": _Raw("@job_id OUTPUT"),
    })]
    for i, step in enumerate(steps):
        args = {"job_id": JOB_ID_VAR}
        args.update(step_args(step, is_last=(i == len(steps) - 1)))
        stmts.append(exec_proc("msdb.dbo.sp_add_jobstep", args))
    for sched in schedules or []:
        args = {"job_id": JOB_ID_VAR}
        args.update(schedule_args(sched))
        stmts.append(exec_proc("msdb.dbo.sp_add_jobschedule", args))
    stmts.append(exec_proc("msdb.dbo.sp_add_jobserver", {"job_id": JOB_ID_VAR, "server_name": server_name}))
    return wrap_transaction(stmts, tail="SELECT @job_id AS job_id;")


def build_update_job(job_name: str, *, new_name: str | None = None, description: str | None = None,
                     enabled: bool | None = None, category: str | None = None,
                     owner_login: str | None = None, start_step_id: int | None = None) -> tuple[str, list[Any]]:
    args = {"job_name": job_name, "new_name": new_name, "description": description, "enabled": enabled,
            "category_name": category, "owner_login_name": owner_login, "start_step_id": start_step_id}
    if all(v is None for k, v in args.items() if k != "job_name"):
        raise JobSpecError("Nothing to update - pass at least one property")
    return wrap_single(exec_proc("msdb.dbo.sp_update_job", args))


def build_delete_job(job_name: str, delete_history: bool = True) -> tuple[str, list[Any]]:
    return wrap_single(exec_proc("msdb.dbo.sp_delete_job", {"job_name": job_name,
                                                             "delete_history": delete_history}))


def build_start_job(job_name: str, step_name: str | None = None) -> tuple[str, list[Any]]:
    return wrap_single(exec_proc("msdb.dbo.sp_start_job", {"job_name": job_name, "step_name": step_name}))


def build_stop_job(job_name: str) -> tuple[str, list[Any]]:
    return wrap_single(exec_proc("msdb.dbo.sp_stop_job", {"job_name": job_name}))


def build_add_step(job_name: str, step: dict[str, Any]) -> tuple[str, list[Any]]:
    args = {"job_name": job_name}
    args.update(step_args(step, is_last=True))
    return wrap_single(exec_proc("msdb.dbo.sp_add_jobstep", args))


def build_update_step(job_name: str, step_id: int, changes: dict[str, Any]) -> tuple[str, list[Any]]:
    changes = dict(changes)
    changes.pop("step_id", None)
    args = {"job_name": job_name, "step_id": _int(step_id, "step_id", 1)}
    args.update(step_args(changes, require_all=False))
    if len(args) <= 2:
        raise JobSpecError("Nothing to update for the step")
    return wrap_single(exec_proc("msdb.dbo.sp_update_jobstep", args))


def build_delete_step(job_name: str, step_id: int) -> tuple[str, list[Any]]:
    return wrap_single(exec_proc("msdb.dbo.sp_delete_jobstep",
                                 {"job_name": job_name, "step_id": _int(step_id, "step_id", 0)}))


def build_add_schedule(job_name: str, schedule: dict[str, Any]) -> tuple[str, list[Any]]:
    args = {"job_name": job_name}
    args.update(schedule_args(schedule))
    return wrap_single(exec_proc("msdb.dbo.sp_add_jobschedule", args))


def build_delete_schedule(job_name: str, schedule_name: str) -> tuple[str, list[Any]]:
    return wrap_single(exec_proc("msdb.dbo.sp_detach_schedule", {
        "job_name": job_name, "schedule_name": schedule_name, "delete_unused_schedule": True}))


LIST_JOBS_SQL = """
DECLARE @name_like nvarchar(256) = ?, @enabled int = ?;
SELECT j.name AS job_name, j.enabled, c.name AS category, SUSER_SNAME(j.owner_sid) AS owner,
       j.description, j.date_created, j.date_modified,
       CASE WHEN ja.start_execution_date IS NOT NULL AND ja.stop_execution_date IS NULL THEN 1 ELSE 0 END AS is_running,
       CASE js.last_run_outcome WHEN 0 THEN 'Failed' WHEN 1 THEN 'Succeeded' WHEN 3 THEN 'Canceled'
            WHEN 5 THEN 'Unknown' ELSE NULL END AS last_run_outcome,
       CASE WHEN js.last_run_date > 0 THEN msdb.dbo.agent_datetime(js.last_run_date, js.last_run_time) END AS last_run_at,
       (SELECT MIN(CASE WHEN s.next_run_date > 0 THEN msdb.dbo.agent_datetime(s.next_run_date, s.next_run_time) END)
          FROM msdb.dbo.sysjobschedules s WHERE s.job_id = j.job_id) AS next_run_at
FROM msdb.dbo.sysjobs j
LEFT JOIN msdb.dbo.syscategories c ON c.category_id = j.category_id
LEFT JOIN msdb.dbo.sysjobservers js ON js.job_id = j.job_id
OUTER APPLY (SELECT TOP (1) a.start_execution_date, a.stop_execution_date
             FROM msdb.dbo.sysjobactivity a
             WHERE a.job_id = j.job_id AND a.session_id = (SELECT MAX(session_id) FROM msdb.dbo.syssessions)
             ORDER BY a.start_execution_date DESC) ja
WHERE (@name_like IS NULL OR j.name LIKE @name_like) AND (@enabled IS NULL OR j.enabled = @enabled)
ORDER BY j.name;
"""

GET_JOB_SQL = """
DECLARE @job_name sysname = ?;
DECLARE @job_id uniqueidentifier = (SELECT job_id FROM msdb.dbo.sysjobs WHERE name = @job_name);
IF @job_id IS NULL BEGIN RAISERROR('Job ''%s'' was not found in msdb.dbo.sysjobs', 16, 1, @job_name); RETURN; END;
SELECT j.job_id, j.name AS job_name, j.enabled, j.description, c.name AS category,
       SUSER_SNAME(j.owner_sid) AS owner, j.start_step_id, j.date_created, j.date_modified
FROM msdb.dbo.sysjobs j LEFT JOIN msdb.dbo.syscategories c ON c.category_id = j.category_id
WHERE j.job_id = @job_id;
SELECT st.step_id, st.step_name, st.subsystem, st.database_name, st.command,
       CASE st.on_success_action WHEN 1 THEN 'quit_success' WHEN 2 THEN 'quit_failure' WHEN 3 THEN 'next'
            WHEN 4 THEN 'goto:' + CAST(st.on_success_step_id AS varchar(10)) END AS on_success,
       CASE st.on_fail_action WHEN 1 THEN 'quit_success' WHEN 2 THEN 'quit_failure' WHEN 3 THEN 'next'
            WHEN 4 THEN 'goto:' + CAST(st.on_fail_step_id AS varchar(10)) END AS on_fail,
       st.retry_attempts, st.retry_interval, st.output_file_name,
       CASE st.last_run_outcome WHEN 0 THEN 'Failed' WHEN 1 THEN 'Succeeded' WHEN 2 THEN 'Retry'
            WHEN 3 THEN 'Canceled' WHEN 5 THEN 'Unknown' END AS last_run_outcome
FROM msdb.dbo.sysjobsteps st WHERE st.job_id = @job_id ORDER BY st.step_id;
SELECT s.name AS schedule_name, s.enabled,
       CASE s.freq_type WHEN 1 THEN 'once' WHEN 4 THEN 'daily' WHEN 8 THEN 'weekly' WHEN 16 THEN 'monthly'
            WHEN 32 THEN 'monthly_relative' WHEN 64 THEN 'agent_start' WHEN 128 THEN 'idle' END AS type,
       s.freq_interval, s.freq_subday_type, s.freq_subday_interval, s.freq_recurrence_factor,
       s.active_start_date, s.active_start_time, s.active_end_date, s.active_end_time,
       CASE WHEN js.next_run_date > 0 THEN msdb.dbo.agent_datetime(js.next_run_date, js.next_run_time) END AS next_run_at
FROM msdb.dbo.sysjobschedules js JOIN msdb.dbo.sysschedules s ON s.schedule_id = js.schedule_id
WHERE js.job_id = @job_id ORDER BY s.name;
"""

JOB_HISTORY_SQL = """
DECLARE @top int = ?, @job_name sysname = ?, @failed_only int = ?;
IF @job_name IS NOT NULL AND NOT EXISTS (SELECT 1 FROM msdb.dbo.sysjobs WHERE name = @job_name)
BEGIN RAISERROR('Job ''%s'' was not found in msdb.dbo.sysjobs', 16, 1, @job_name); RETURN; END;
SELECT TOP (@top) j.name AS job_name, h.step_id, h.step_name,
       msdb.dbo.agent_datetime(h.run_date, h.run_time) AS run_at,
       CASE h.run_status WHEN 0 THEN 'Failed' WHEN 1 THEN 'Succeeded' WHEN 2 THEN 'Retry'
            WHEN 3 THEN 'Canceled' WHEN 4 THEN 'In Progress' END AS status,
       (h.run_duration / 10000) * 3600 + (h.run_duration / 100 % 100) * 60 + h.run_duration % 100 AS duration_seconds,
       h.sql_message_id, h.sql_severity, h.message
FROM msdb.dbo.sysjobhistory h JOIN msdb.dbo.sysjobs j ON j.job_id = h.job_id
WHERE (@job_name IS NULL OR j.name = @job_name) AND (@failed_only = 0 OR h.run_status = 0)
ORDER BY h.instance_id DESC;
"""

RUNNING_JOBS_SQL = """
SELECT j.name AS job_name, a.start_execution_date,
       DATEDIFF(SECOND, a.start_execution_date, GETDATE()) AS running_seconds,
       ISNULL(a.last_executed_step_id, 0) + 1 AS current_step_id
FROM msdb.dbo.sysjobactivity a JOIN msdb.dbo.sysjobs j ON j.job_id = a.job_id
WHERE a.session_id = (SELECT MAX(session_id) FROM msdb.dbo.syssessions)
  AND a.start_execution_date IS NOT NULL AND a.stop_execution_date IS NULL
ORDER BY a.start_execution_date;
"""
