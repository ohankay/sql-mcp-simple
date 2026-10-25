"""Entry point: ``python -m sql_mcp`` (stdio MCP server) or ``python -m sql_mcp --check``."""

from __future__ import annotations

import argparse
import json
import logging
import sys

from . import __version__


def _check(connection: str | None) -> int:
    from .config import load_profiles
    from .drivers import _import_mssql_python, _import_pyodbc
    from .session import SessionManager
    from .server import _server_info, set_manager

    pyodbc = _import_pyodbc()
    report: dict = {
        "version": __version__,
        "python": sys.version.split()[0],
        "pyodbc": getattr(pyodbc, "version", None) if pyodbc else None,
        "odbc_drivers": [d for d in (pyodbc.drivers() if pyodbc else []) if "SQL Server" in d],
        "mssql_python": _import_mssql_python() is not None,
    }
    try:
        profiles, default = load_profiles()
    except Exception as exc:  # noqa: BLE001
        report["config_error"] = str(exc)
        print(json.dumps(report, indent=2, default=str))
        return 2
    mgr = SessionManager(profiles, default)
    set_manager(mgr)
    report["default_connection"] = default
    report["connections"] = {}
    names = [connection] if connection else list(profiles)
    ok = True
    for name in names:
        try:
            info = _server_info(name)
        except Exception as exc:  # noqa: BLE001
            info = {"success": False, "errors": [{"message": str(exc)}]}
        ok = ok and bool(info.get("success"))
        report["connections"][name] = info
    mgr.close_all()
    if not names:
        report["note"] = "No connection configured (set SQL_MCP_SERVER or SQL_MCP_CONFIG)."
    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    return 0 if ok and names else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sql-mcp", description="MCP server for Microsoft SQL Server 2017+")
    parser.add_argument("--version", action="version", version=f"sql-mcp {__version__}")
    parser.add_argument("--check", action="store_true", help="test the configured connection(s) and exit")
    parser.add_argument("--connection", help="with --check: only this profile")
    parser.add_argument("--log-level", default="WARNING", help="stderr log level (default WARNING)")
    args = parser.parse_args(argv)
    # stdout is the MCP channel - logs must go to stderr only.
    logging.basicConfig(stream=sys.stderr, level=args.log_level.upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.check:
        return _check(args.connection)
    from .server import run_stdio

    run_stdio()
    return 0


if __name__ == "__main__":
    sys.exit(main())
