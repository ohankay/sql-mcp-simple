"""Batch execution and result shaping."""

from __future__ import annotations

import datetime as _dt
import decimal
import re
import time
import uuid
from typing import Any, Sequence

from .sqltext import split_batches

_PREFIX_RE = re.compile(r"(\[[^\]]*\])+")
_ERRNUM_RE = re.compile(r"\((\d+)\)\s*(?:\(SQL\w+\))?\s*$")
_SQLSTATE_RE = re.compile(r"\[(\w{5})\]")
MAX_BINARY_BYTES = 4096


def to_jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return str(value)
        return value
    if isinstance(value, decimal.Decimal):
        if not value.is_finite():
            return str(value)
        # Integral decimals become ints, others keep full precision as text.
        if value == value.to_integral_value() and abs(value) < 2**53:
            return int(value)
        return str(value)
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    if isinstance(value, _dt.timedelta):
        return str(value)
    if isinstance(value, uuid.UUID):
        return str(value).upper()
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        text = "0x" + raw[:MAX_BINARY_BYTES].hex().upper()
        if len(raw) > MAX_BINARY_BYTES:
            text += f"...(+{len(raw) - MAX_BINARY_BYTES} bytes)"
        return text
    return str(value)


def clean_message(text: str) -> str:
    """Strip the '[Microsoft][ODBC Driver 18 for SQL Server][SQL Server]' style prefixes."""
    text = str(text)
    idx = text.rfind("[SQL Server]")
    if idx >= 0:
        text = text[idx + len("[SQL Server]"):]
    else:
        text = _PREFIX_RE.sub("", text)
    text = re.sub(r"\s*\(SQL\w+\)\s*$", "", text)
    text = re.sub(r"^\s*Driver Error:.*?DDBC Error:\s*", "", text)
    return text.strip()


def describe_error(exc: BaseException) -> dict[str, Any]:
    raw = " ".join(str(a) for a in getattr(exc, "args", ()) if a is not None) or str(exc)
    info: dict[str, Any] = {"type": type(exc).__name__, "message": clean_message(raw)}
    m = _SQLSTATE_RE.search(raw)
    if m:
        info["sqlstate"] = m.group(1)
    m = _ERRNUM_RE.search(raw)
    if m:
        info["sql_error_number"] = int(m.group(1))
        info["message"] = re.sub(r"\s*\(\d+\)\s*$", "", info["message"])
    return info


def is_connection_error(exc: BaseException) -> bool:
    raw = str(exc)
    markers = ("08S01", "08001", "08003", "08004", "08007", "HYT01", "Communication link failure",
               "connection is closed", "Connection is closed", "TCP Provider", "physical connection is not usable",
               "Attempt to use a closed connection", "connection is busy")
    return any(m in raw for m in markers)


def _collect_messages(cursor: Any, sink: list[str]) -> None:
    msgs = getattr(cursor, "messages", None)
    if not msgs:
        return
    for item in msgs:
        text = item[1] if isinstance(item, (tuple, list)) and len(item) > 1 else item
        cleaned = clean_message(text)
        if cleaned:
            sink.append(cleaned)
    try:
        msgs.clear()
    except Exception:
        pass


class ResultBudget:
    """Caps total characters returned so one huge result cannot flood the model context."""

    def __init__(self, max_chars: int):
        self.remaining = max_chars
        self.exhausted = False

    def take(self, row: list[Any]) -> bool:
        cost = sum(len(v) if isinstance(v, str) else 8 for v in row) + 2 * len(row)
        if cost > self.remaining:
            self.exhausted = True
            return False
        self.remaining -= cost
        return True


def _read_result_set(cursor: Any, max_rows: int, budget: ResultBudget) -> dict[str, Any]:
    columns = [d[0] if d[0] else f"(col{i + 1})" for i, d in enumerate(cursor.description)]
    rows: list[list[Any]] = []
    truncated = False
    while True:
        chunk = cursor.fetchmany(min(500, max_rows + 1 - len(rows)) or 1)
        if not chunk:
            break
        for raw in chunk:
            if len(rows) >= max_rows:
                truncated = True
                break
            row = [to_jsonable(v) for v in raw]
            if not budget.take(row):
                truncated = True
                break
            rows.append(row)
        if truncated or len(rows) >= max_rows:
            # Peek one more to know whether we actually dropped rows.
            if not truncated and cursor.fetchone() is not None:
                truncated = True
            break
    result: dict[str, Any] = {"columns": columns, "rows": rows, "row_count": len(rows)}
    if truncated:
        result["truncated"] = True
        result["note"] = (
            f"Result truncated at {len(rows)} rows" + (" (response size limit)" if budget.exhausted else "")
            + ". Narrow the query (WHERE/TOP) or raise max_rows."
        )
    return result


def mssql_python_input_sizes(params: Sequence[Any]) -> list[tuple[int, int, int]] | None:
    """mssql-python binds ASCII ``str`` as varchar, which breaks nvarchar-only parameters
    (e.g. sp_executesql). Declare every parameter's type explicitly so text is always nvarchar."""
    if not any(isinstance(p, str) for p in params):
        return None
    import mssql_python as m  # type: ignore

    sizes: list[tuple[int, int, int]] = []
    for p in params:
        if isinstance(p, str) or p is None:
            sizes.append((m.SQL_WVARCHAR, 0, 0))
        elif isinstance(p, bool):
            sizes.append((m.SQL_BIT, 0, 0))
        elif isinstance(p, int):
            sizes.append((m.SQL_INTEGER if -2**31 <= p < 2**31 else m.SQL_BIGINT, 0, 0))
        elif isinstance(p, float):
            sizes.append((m.SQL_DOUBLE, 0, 0))
        else:
            return None  # unusual type: let the driver infer everything
    return sizes


def _is_mssql_python(cursor: Any) -> bool:
    return type(cursor).__module__.startswith("mssql_python")


def execute_batch(cursor: Any, sql: str, params: Sequence[Any] | None, max_rows: int,
                  budget: ResultBudget, out: dict[str, Any]) -> None:
    """Execute one batch, appending result sets / row counts / messages into ``out``."""
    if params:
        if _is_mssql_python(cursor):
            sizes = mssql_python_input_sizes(params)
            if sizes:
                cursor.setinputsizes(sizes)
        cursor.execute(sql, list(params))
    else:
        cursor.execute(sql)
    while True:
        _collect_messages(cursor, out["messages"])
        if cursor.description:
            out["result_sets"].append(_read_result_set(cursor, max_rows, budget))
        else:
            rc = getattr(cursor, "rowcount", -1)
            if rc is not None and rc >= 0:
                out["rows_affected"].append(rc)
        if not cursor.nextset():
            break
    _collect_messages(cursor, out["messages"])


def run_script(conn: Any, script: str, *, params: Sequence[Any] | None = None, max_rows: int = 1000,
               continue_on_error: bool = False, max_chars: int = 2_000_000) -> dict[str, Any]:
    batches = split_batches(script)
    out: dict[str, Any] = {
        "success": True,
        "batches": len(batches),
        "result_sets": [],
        "rows_affected": [],
        "messages": [],
    }
    if not batches:
        out["success"] = False
        out["errors"] = [{"message": "Nothing to execute (empty SQL)"}]
        return out
    if params and len(batches) > 1:
        out["success"] = False
        out["errors"] = [{"message": "Parameters can only be used with a single batch (no GO separators)"}]
        return out
    budget = ResultBudget(max_chars)
    errors: list[dict[str, Any]] = []
    started = time.perf_counter()
    for index, batch in enumerate(batches, start=1):
        cursor = conn.cursor()
        try:
            execute_batch(cursor, batch, params, max_rows, budget, out)
        except Exception as exc:  # noqa: BLE001 - every driver error is reported back
            _collect_messages(cursor, out["messages"])
            err = describe_error(exc)
            if len(batches) > 1:
                err["batch"] = index
            errors.append(err)
            if is_connection_error(exc):
                err["connection_lost"] = True
                break
            if not continue_on_error:
                break
        finally:
            try:
                cursor.close()
            except Exception:
                pass
    out["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
    if errors:
        out["success"] = False
        out["errors"] = errors
    if not out["messages"]:
        del out["messages"]
    return out
