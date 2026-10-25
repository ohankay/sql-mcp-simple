"""Connection profile configuration.

Profiles can come from three places (later ones win on name clashes):

1. Environment variables ``SQL_MCP_*`` -> a single profile (named ``SQL_MCP_NAME`` or "default").
2. A JSON file pointed to by ``SQL_MCP_CONFIG`` holding several named profiles.
3. Runtime registration through the ``add_connection`` tool.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Mapping

AUTH_SQL = "sql"
AUTH_WINDOWS = "windows"
_AUTH_ALIASES = {
    "sql": AUTH_SQL,
    "sqlserver": AUTH_SQL,
    "sql_login": AUTH_SQL,
    "password": AUTH_SQL,
    "windows": AUTH_WINDOWS,
    "integrated": AUTH_WINDOWS,
    "trusted": AUTH_WINDOWS,
    "sspi": AUTH_WINDOWS,
    "ntlm": AUTH_WINDOWS,
}
DRIVERS = ("auto", "pyodbc", "mssql-python")
ENCRYPT_VALUES = ("yes", "no", "strict", "optional", "mandatory")


class ConfigError(ValueError):
    """Raised for invalid or incomplete connection configuration."""


def _to_bool(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "y", "on"):
        return True
    if text in ("0", "false", "no", "n", "off", ""):
        return False
    raise ConfigError(f"'{name}' must be a boolean, got {value!r}")


def _to_int(value: Any, name: str, minimum: int = 0) -> int:
    try:
        result = int(str(value).strip())
    except (TypeError, ValueError):
        raise ConfigError(f"'{name}' must be an integer, got {value!r}") from None
    if result < minimum:
        raise ConfigError(f"'{name}' must be >= {minimum}, got {result}")
    return result


@dataclass
class ConnectionProfile:
    name: str
    server: str
    auth: str = AUTH_SQL
    database: str = "master"
    user: str | None = None
    password: str | None = field(default=None, repr=False)
    port: int | None = None
    driver: str = "auto"
    odbc_driver: str | None = None
    encrypt: str = "yes"
    trust_server_certificate: bool = True
    readonly: bool = False
    login_timeout: int = 15
    query_timeout: int = 300
    max_rows: int = 1000
    app_name: str = "sql-mcp"
    extra: str | None = None  # raw "Key=Value;..." appended to the connection string

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if not self.name or not str(self.name).strip():
            raise ConfigError("Connection name must not be empty")
        if not self.server or not str(self.server).strip():
            raise ConfigError(f"[{self.name}] 'server' is required")
        auth = _AUTH_ALIASES.get(str(self.auth).strip().lower())
        if auth is None:
            raise ConfigError(f"[{self.name}] 'auth' must be 'sql' or 'windows', got {self.auth!r}")
        self.auth = auth
        if auth == AUTH_SQL:
            if not self.user:
                raise ConfigError(f"[{self.name}] SQL authentication requires 'user'")
            if self.password is None:
                raise ConfigError(f"[{self.name}] SQL authentication requires 'password' (or 'password_env')")
        driver = str(self.driver or "auto").strip().lower().replace("_", "-")
        if driver not in DRIVERS:
            raise ConfigError(f"[{self.name}] 'driver' must be one of {DRIVERS}, got {self.driver!r}")
        self.driver = driver
        encrypt = str(self.encrypt).strip().lower()
        if encrypt in ("true", "1"):
            encrypt = "yes"
        elif encrypt in ("false", "0"):
            encrypt = "no"
        if encrypt not in ENCRYPT_VALUES:
            raise ConfigError(f"[{self.name}] 'encrypt' must be one of {ENCRYPT_VALUES}, got {self.encrypt!r}")
        self.encrypt = encrypt
        if self.port is not None:
            self.port = _to_int(self.port, "port", 1)
            if self.port > 65535:
                raise ConfigError(f"[{self.name}] 'port' must be <= 65535")
        self.trust_server_certificate = _to_bool(self.trust_server_certificate, "trust_server_certificate")
        self.readonly = _to_bool(self.readonly, "readonly")
        self.login_timeout = _to_int(self.login_timeout, "login_timeout")
        self.query_timeout = _to_int(self.query_timeout, "query_timeout")
        self.max_rows = _to_int(self.max_rows, "max_rows", 1)
        if not self.database:
            self.database = "master"
        for ch in (";", "{", "}"):
            # These would let a value break out of its connection-string slot.
            for attr in ("server", "database", "app_name", "odbc_driver"):
                val = getattr(self, attr)
                if val and ch in val:
                    raise ConfigError(f"[{self.name}] '{attr}' must not contain '{ch}'")

    def public_dict(self) -> dict[str, Any]:
        """Profile description safe to show to the model (no secrets)."""
        data = asdict(self)
        data.pop("password", None)
        data["has_password"] = self.password is not None
        return data


_FIELD_NAMES = {f.name for f in fields(ConnectionProfile)}


def profile_from_mapping(name: str, data: Mapping[str, Any]) -> ConnectionProfile:
    kwargs: dict[str, Any] = {}
    for key, value in data.items():
        key = str(key).strip().lower()
        if key == "password_env":
            env_name = str(value)
            if env_name not in os.environ:
                raise ConfigError(f"[{name}] environment variable '{env_name}' (password_env) is not set")
            kwargs["password"] = os.environ[env_name]
            continue
        if key == "username":
            key = "user"
        if key == "host":
            key = "server"
        if key not in _FIELD_NAMES or key == "name":
            raise ConfigError(f"[{name}] unknown setting '{key}'")
        kwargs[key] = value
    if "auth" not in kwargs:
        kwargs["auth"] = AUTH_SQL if kwargs.get("user") else AUTH_WINDOWS
    return ConnectionProfile(name=name, **kwargs)


_ENV_MAP = {
    "SERVER": "server",
    "HOST": "server",
    "PORT": "port",
    "DATABASE": "database",
    "AUTH": "auth",
    "USER": "user",
    "USERNAME": "user",
    "PASSWORD": "password",
    "DRIVER": "driver",
    "ODBC_DRIVER": "odbc_driver",
    "ENCRYPT": "encrypt",
    "TRUST_SERVER_CERTIFICATE": "trust_server_certificate",
    "READONLY": "readonly",
    "LOGIN_TIMEOUT": "login_timeout",
    "QUERY_TIMEOUT": "query_timeout",
    "MAX_ROWS": "max_rows",
    "APP_NAME": "app_name",
    "EXTRA": "extra",
}


def profile_from_env(env: Mapping[str, str], prefix: str = "SQL_MCP_") -> ConnectionProfile | None:
    data: dict[str, Any] = {}
    for suffix, key in _ENV_MAP.items():
        value = env.get(prefix + suffix)
        if value is None or value == "":
            continue
        # Unexpanded VS Code placeholders mean "not provided".
        if value.startswith("${") and value.endswith("}"):
            continue
        data[key] = value
    if "server" not in data:
        return None
    name = env.get(prefix + "NAME") or "default"
    return profile_from_mapping(name, data)


def load_config_file(path: str) -> tuple[dict[str, ConnectionProfile], str | None]:
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        raise ConfigError(f"Config file not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Config file {path} is not valid JSON: {exc}") from None
    if not isinstance(raw, dict):
        raise ConfigError("Config file root must be a JSON object")
    conns = raw.get("connections")
    if not isinstance(conns, dict) or not conns:
        raise ConfigError("Config file must contain a non-empty 'connections' object")
    profiles: dict[str, ConnectionProfile] = {}
    for name, data in conns.items():
        if not isinstance(data, dict):
            raise ConfigError(f"Connection '{name}' must be an object")
        profiles[name] = profile_from_mapping(name, data)
    default = raw.get("default")
    if default is not None and default not in profiles:
        raise ConfigError(f"Default connection '{default}' is not defined in 'connections'")
    return profiles, default


def load_profiles(env: Mapping[str, str] | None = None) -> tuple[dict[str, ConnectionProfile], str | None]:
    env = os.environ if env is None else env
    profiles: dict[str, ConnectionProfile] = {}
    default: str | None = None
    path = env.get("SQL_MCP_CONFIG")
    if path and not (path.startswith("${") and path.endswith("}")):
        profiles, default = load_config_file(os.path.expanduser(os.path.expandvars(path)))
    env_profile = profile_from_env(env)
    if env_profile is not None:
        profiles[env_profile.name] = env_profile
        default = default or env_profile.name
    if default is None and profiles:
        default = next(iter(profiles))
    return profiles, default
