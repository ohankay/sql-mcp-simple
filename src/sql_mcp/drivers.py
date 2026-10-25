"""Driver selection and connection-string building.

Two DB-API drivers are supported, both talk TDS in-process (no sqlcmd / PowerShell child process):

* ``pyodbc``        - uses an installed "ODBC Driver 17/18 for SQL Server" (most corporate PCs have one,
                      SSMS installs it). Preferred when available.
* ``mssql-python``  - Microsoft's driver with the ODBC driver bundled in the wheel, so nothing has to be
                      installed system-wide. Used automatically when no ODBC driver is found.
"""

from __future__ import annotations

import datetime as _dt
import re
import struct
from typing import Any

from .config import AUTH_WINDOWS, ConfigError, ConnectionProfile

_ODBC_DRIVER_RE = re.compile(r"^ODBC Driver (\d+) for SQL Server$", re.IGNORECASE)
_LEGACY_ODBC_DRIVERS = ("SQL Server Native Client 11.0", "SQL Server")


class DriverUnavailableError(RuntimeError):
    pass


def odbc_escape(value: str) -> str:
    """Quote a connection-string value so ';', '{', '}' and spaces are safe."""
    value = str(value)
    if value == "" or any(ch in value for ch in ";{}=") or value != value.strip():
        return "{" + value.replace("}", "}}") + "}"
    return value


def _import_pyodbc():
    try:
        import pyodbc  # type: ignore

        return pyodbc
    except ImportError:
        return None


def _import_mssql_python():
    try:
        import mssql_python  # type: ignore

        return mssql_python
    except ImportError:
        return None


def pick_odbc_driver(installed: list[str]) -> str | None:
    """Return the newest 'ODBC Driver NN for SQL Server' from the installed list."""
    best: tuple[int, str] | None = None
    for name in installed:
        m = _ODBC_DRIVER_RE.match(name.strip())
        if m:
            version = int(m.group(1))
            if best is None or version > best[0]:
                best = (version, name.strip())
    if best:
        return best[1]
    for legacy in _LEGACY_ODBC_DRIVERS:
        if legacy in installed:
            return legacy
    return None


def resolve_driver(profile: ConnectionProfile) -> tuple[str, str | None]:
    """Return (driver_kind, odbc_driver_name).

    auto: "ODBC Driver 17/18 for SQL Server" via pyodbc -> mssql-python (bundled driver)
          -> legacy Windows "SQL Server" ODBC driver via pyodbc.
    """
    pyodbc = _import_pyodbc() if profile.driver in ("auto", "pyodbc") else None
    odbc_name = profile.odbc_driver
    if pyodbc is not None and odbc_name is None:
        try:
            odbc_name = pick_odbc_driver(list(pyodbc.drivers()))
        except Exception:  # pragma: no cover - driver manager missing
            odbc_name = None
    if profile.driver == "pyodbc":
        if pyodbc is None or not odbc_name:
            raise DriverUnavailableError(
                "driver=pyodbc requested but "
                + ("pyodbc is not installed" if pyodbc is None else "no SQL Server ODBC driver was found")
                + ". Install 'ODBC Driver 18 for SQL Server' or use driver=mssql-python."
            )
        return "pyodbc", odbc_name
    legacy = odbc_name in _LEGACY_ODBC_DRIVERS
    if profile.driver == "auto" and pyodbc is not None and odbc_name and (not legacy or profile.odbc_driver):
        return "pyodbc", odbc_name
    if _import_mssql_python() is not None:
        return "mssql-python", None
    if profile.driver == "mssql-python":
        raise DriverUnavailableError(
            "driver=mssql-python requested but mssql-python is not installed (it has no 32-bit build). "
            "Use driver=auto or driver=pyodbc to connect through an installed SQL Server ODBC driver."
        )
    if profile.driver == "auto" and pyodbc is not None and odbc_name:
        return "pyodbc", odbc_name
    raise DriverUnavailableError(
        "No usable SQL Server driver: mssql-python is not installed (it has no 32-bit build) and no "
        "SQL Server ODBC driver was found. Install 'ODBC Driver 18 for SQL Server', or on 64-bit "
        "Python run: pip install mssql-python"
    )


def _server_value(profile: ConnectionProfile) -> str:
    server = profile.server.strip()
    if profile.port and "," not in server:
        server = f"{server},{profile.port}"
    return server


def build_connection_string(profile: ConnectionProfile, kind: str, odbc_driver: str | None = None) -> str:
    parts: list[tuple[str, str]] = []
    if kind == "pyodbc":
        if not odbc_driver:
            raise ConfigError("pyodbc needs an ODBC driver name")
        parts.append(("DRIVER", "{" + odbc_driver + "}"))
    parts.append(("Server", odbc_escape(_server_value(profile))))
    parts.append(("Database", odbc_escape(profile.database)))
    if profile.auth == AUTH_WINDOWS:
        parts.append(("Trusted_Connection", "yes"))
    else:
        parts.append(("UID", odbc_escape(profile.user or "")))
        parts.append(("PWD", odbc_escape(profile.password or "")))
    legacy = kind == "pyodbc" and odbc_driver in _LEGACY_ODBC_DRIVERS
    if not legacy:
        parts.append(("Encrypt", profile.encrypt))
        parts.append(("TrustServerCertificate", "yes" if profile.trust_server_certificate else "no"))
    if kind == "pyodbc":
        parts.append(("APP", odbc_escape(profile.app_name)))
    conn_str = ";".join(f"{k}={v}" for k, v in parts)
    if profile.extra:
        conn_str += ";" + profile.extra.strip().strip(";")
    return conn_str


def redact(conn_str: str) -> str:
    return re.sub(r"(?i)(PWD=)(\{(?:[^}]|\}\})*\}|[^;]*)", r"\1***", conn_str)


def _datetimeoffset(raw: bytes | None):
    if raw is None:
        return None
    y, mo, d, h, mi, s, ns, tzh, tzm = struct.unpack("<6hI2h", raw)
    tz = _dt.timezone(_dt.timedelta(hours=tzh, minutes=tzm))
    return _dt.datetime(y, mo, d, h, mi, s, ns // 1000, tzinfo=tz)


def _int_converter(fmt: str):
    def convert(raw: bytes | None):
        return None if raw is None else struct.unpack(fmt, raw)[0]
    return convert


# pyodbc cannot natively read these ODBC types; without converters the whole query fails.
_PYODBC_CONVERTERS = {
    -155: _datetimeoffset,           # datetimeoffset
    -151: lambda raw: raw,           # CLR UDTs (geography, geometry, hierarchyid) -> raw bytes
    -28: _int_converter("<B"),       # tinyint inside sql_variant
    -15: _int_converter("<h"),       # smallint inside sql_variant
    -16: _int_converter("<i"),       # int inside sql_variant
    -25: _int_converter("<q"),       # bigint inside sql_variant
}


def open_connection(profile: ConnectionProfile) -> tuple[Any, str]:
    """Open a DB-API connection (autocommit on). Returns (connection, driver_kind)."""
    kind, odbc_name = resolve_driver(profile)
    conn_str = build_connection_string(profile, kind, odbc_name)
    if kind == "pyodbc":
        pyodbc = _import_pyodbc()
        conn = pyodbc.connect(conn_str, autocommit=True, timeout=profile.login_timeout)
        for sql_type, func in _PYODBC_CONVERTERS.items():
            conn.add_output_converter(sql_type, func)
        label = f"pyodbc ({odbc_name})"
    else:
        mssql_python = _import_mssql_python()
        conn = mssql_python.connect(conn_str, autocommit=True, timeout=profile.login_timeout)
        label = "mssql-python"
    try:
        conn.timeout = profile.query_timeout
    except Exception:  # pragma: no cover - attribute not supported
        pass
    return conn, label
