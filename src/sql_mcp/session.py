"""Persistent per-profile connections with health checks and transaction tracking."""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Sequence

from . import drivers
from .config import ConfigError, ConnectionProfile
from .executor import is_connection_error, run_script
from .sqltext import check_readonly, quote_ident

PING_AFTER_IDLE_SECONDS = 15.0


class SessionError(RuntimeError):
    pass


class Session:
    def __init__(self, profile: ConnectionProfile, opener: Callable[[ConnectionProfile], tuple[Any, str]]):
        self.profile = profile
        self._opener = opener
        self.conn: Any = None
        self.driver_label: str | None = None
        self.lock = threading.RLock()
        self.last_used = 0.0
        self.open_transactions = 0

    # ------------------------------------------------------------------ lifecycle
    def _open(self) -> None:
        self.conn, self.driver_label = self._opener(self.profile)
        self.last_used = time.monotonic()
        self.open_transactions = 0

    def close(self) -> None:
        with self.lock:
            if self.conn is not None:
                try:
                    if self.open_transactions:
                        self.conn.cursor().execute("IF @@TRANCOUNT > 0 ROLLBACK")
                except Exception:
                    pass
                try:
                    self.conn.close()
                except Exception:
                    pass
            self.conn = None
            self.open_transactions = 0

    def _alive(self) -> bool:
        try:
            cur = self.conn.cursor()
            cur.execute("SELECT 1")
            cur.fetchall()
            cur.close()
            return True
        except Exception:
            return False

    def ensure(self) -> Any:
        if self.conn is None:
            self._open()
            return self.conn
        if time.monotonic() - self.last_used > PING_AFTER_IDLE_SECONDS and not self._alive():
            lost_tx = self.open_transactions
            self.close()
            if lost_tx:
                raise SessionError(
                    "The connection was lost while a transaction was open; the server rolled it back. "
                    "Reconnect happens on the next call - re-run the whole transaction."
                )
            self._open()
        return self.conn

    # ------------------------------------------------------------------ helpers
    def _scalar(self, sql: str) -> Any:
        cur = self.conn.cursor()
        try:
            cur.execute(sql)
            row = cur.fetchone()
            while cur.nextset():
                pass
            return row[0] if row else None
        finally:
            cur.close()

    def _refresh_trancount(self) -> None:
        try:
            self.open_transactions = int(self._scalar("SELECT @@TRANCOUNT") or 0)
        except Exception:
            pass

    def current_database(self) -> str | None:
        return self._scalar("SELECT DB_NAME()")

    # ------------------------------------------------------------------ execution
    def execute(self, sql: str, *, params: Sequence[Any] | None = None, database: str | None = None,
                max_rows: int | None = None, continue_on_error: bool = False,
                timeout: int | None = None, enforce_readonly: bool = True) -> dict[str, Any]:
        if enforce_readonly and self.profile.readonly:
            check_readonly(sql)
        with self.lock:
            conn = self.ensure()
            original_db = None
            old_timeout = None
            try:
                if timeout is not None:
                    old_timeout = getattr(conn, "timeout", None)
                    conn.timeout = int(timeout)
                if database:
                    original_db = self.current_database()
                    if original_db and original_db.lower() == database.lower():
                        original_db = None
                    else:
                        cur = conn.cursor()
                        try:
                            cur.execute("USE " + quote_ident(database))
                        finally:
                            cur.close()
                result = run_script(conn, sql, params=params,
                                    max_rows=max_rows or self.profile.max_rows,
                                    continue_on_error=continue_on_error)
            except Exception as exc:
                if is_connection_error(exc):
                    self.close()
                raise
            finally:
                if self.conn is not None:
                    if original_db:
                        try:
                            c = conn.cursor()
                            c.execute("USE " + quote_ident(original_db))
                            c.close()
                        except Exception:
                            pass
                    if old_timeout is not None:
                        try:
                            conn.timeout = old_timeout
                        except Exception:
                            pass
            if any(e.get("connection_lost") for e in result.get("errors", [])):
                had_tx = self.open_transactions
                self.close()
                result["warning"] = "Connection lost; it will be re-opened on the next call." + (
                    " The open transaction was rolled back by the server." if had_tx else "")
            else:
                self._refresh_trancount()
                if self.open_transactions:
                    result["open_transactions"] = self.open_transactions
                    result["warning"] = (
                        f"{self.open_transactions} transaction(s) still open on this connection - "
                        "call commit_transaction or rollback_transaction."
                    )
            self.last_used = time.monotonic()
            return result


class SessionManager:
    def __init__(self, profiles: dict[str, ConnectionProfile], default: str | None,
                 opener: Callable[[ConnectionProfile], tuple[Any, str]] = drivers.open_connection):
        self._profiles = dict(profiles)
        self._default = default
        self._sessions: dict[str, Session] = {}
        self._opener = opener
        self._lock = threading.Lock()

    @property
    def default(self) -> str | None:
        return self._default

    def names(self) -> list[str]:
        return list(self._profiles)

    def profile(self, name: str | None) -> ConnectionProfile:
        key = self._resolve(name)
        return self._profiles[key]

    def _resolve(self, name: str | None) -> str:
        if not self._profiles:
            raise ConfigError(
                "No SQL connection configured. Set SQL_MCP_SERVER (+ SQL_MCP_AUTH/USER/PASSWORD) in the MCP "
                "server env, point SQL_MCP_CONFIG at a JSON file, or call add_connection."
            )
        if not name:
            if self._default is None:
                raise ConfigError("No default connection; pass 'connection'")
            return self._default
        if name in self._profiles:
            return name
        lowered = {k.lower(): k for k in self._profiles}
        if name.lower() in lowered:
            return lowered[name.lower()]
        raise ConfigError(f"Unknown connection '{name}'. Available: {', '.join(self._profiles)}")

    def get(self, name: str | None) -> Session:
        key = self._resolve(name)
        with self._lock:
            sess = self._sessions.get(key)
            if sess is None:
                sess = Session(self._profiles[key], self._opener)
                self._sessions[key] = sess
            return sess

    def add(self, profile: ConnectionProfile, make_default: bool = False) -> None:
        with self._lock:
            old = self._sessions.pop(profile.name, None)
            self._profiles[profile.name] = profile
            if make_default or self._default is None:
                self._default = profile.name
        if old:
            old.close()

    def remove(self, name: str) -> None:
        key = self._resolve(name)
        with self._lock:
            sess = self._sessions.pop(key, None)
            del self._profiles[key]
            if self._default == key:
                self._default = next(iter(self._profiles), None)
        if sess:
            sess.close()

    def describe(self) -> list[dict[str, Any]]:
        out = []
        for name, prof in self._profiles.items():
            info = prof.public_dict()
            info["is_default"] = name == self._default
            sess = self._sessions.get(name)
            info["connected"] = bool(sess and sess.conn is not None)
            if sess and sess.driver_label:
                info["driver_in_use"] = sess.driver_label
            if sess and sess.open_transactions:
                info["open_transactions"] = sess.open_transactions
            out.append(info)
        return out

    def close_all(self) -> None:
        for sess in list(self._sessions.values()):
            sess.close()
