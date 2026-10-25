"""Pure T-SQL text helpers: GO batch splitting, identifier quoting and the read-only guard."""

from __future__ import annotations

import re

_GO_RE = re.compile(r"^\s*GO(?:\s+(\d+))?\s*(?:--.*)?$", re.IGNORECASE)

_NORMAL, _STRING, _BRACKET, _DQUOTE, _BLOCK = range(5)


def _scan_line(line: str, state: int, depth: int) -> tuple[int, int]:
    """Advance the lexer state over one line (without its newline)."""
    i, n = 0, len(line)
    while i < n:
        ch = line[i]
        nxt = line[i + 1] if i + 1 < n else ""
        if state == _NORMAL:
            if ch == "-" and nxt == "-":
                return state, depth  # rest of line is a comment
            if ch == "/" and nxt == "*":
                state, depth = _BLOCK, 1
                i += 2
                continue
            if ch == "'":
                state = _STRING
            elif ch == "[":
                state = _BRACKET
            elif ch == '"':
                state = _DQUOTE
        elif state == _STRING:
            if ch == "'":
                if nxt == "'":
                    i += 2
                    continue
                state = _NORMAL
        elif state == _BRACKET:
            if ch == "]":
                if nxt == "]":
                    i += 2
                    continue
                state = _NORMAL
        elif state == _DQUOTE:
            if ch == '"':
                if nxt == '"':
                    i += 2
                    continue
                state = _NORMAL
        elif state == _BLOCK:
            if ch == "/" and nxt == "*":
                depth += 1
                i += 2
                continue
            if ch == "*" and nxt == "/":
                depth -= 1
                i += 2
                if depth == 0:
                    state = _NORMAL
                continue
        i += 1
    return state, depth


def split_batches(script: str) -> list[str]:
    """Split a script on sqlcmd-style ``GO`` separators.

    ``GO`` is only honoured on its own line and outside strings, quoted identifiers and comments.
    ``GO n`` repeats the preceding batch n times. Empty batches are dropped.
    """
    if script is None:
        return []
    batches: list[str] = []
    current: list[str] = []
    state, depth = _NORMAL, 0
    for line in script.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if state == _NORMAL:
            m = _GO_RE.match(line)
            if m:
                text = "\n".join(current).strip()
                count = int(m.group(1)) if m.group(1) else 1
                if text:
                    batches.extend([text] * count)
                current = []
                continue
        current.append(line)
        state, depth = _scan_line(line, state, depth)
    text = "\n".join(current).strip()
    if text:
        batches.append(text)
    return batches


def strip_comments_and_literals(sql: str) -> str:
    """Replace comments, string literals and quoted identifiers with a space (for keyword analysis)."""
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if ch == "-" and nxt == "-":
            j = sql.find("\n", i)
            i = n if j < 0 else j
            out.append(" ")
            continue
        if ch == "/" and nxt == "*":
            depth, i = 1, i + 2
            while i < n and depth:
                if sql.startswith("/*", i):
                    depth, i = depth + 1, i + 2
                elif sql.startswith("*/", i):
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
            out.append(" ")
            continue
        if ch in ("'", "[", '"'):
            close = "]" if ch == "[" else ch
            i += 1
            while i < n:
                if sql[i] == close:
                    if i + 1 < n and sql[i + 1] == close:
                        i += 2
                        continue
                    break
                i += 1
            i += 1
            # Keep a placeholder token so "[dbo].[x]" still looks like an identifier.
            out.append(" 'lit' " if ch == "'" else " ident ")
            continue
        if ch in ("N", "n") and nxt == "'":
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def quote_ident(name: str) -> str:
    if name is None or str(name) == "":
        raise ValueError("Identifier must not be empty")
    name = str(name)
    if len(name) > 128:
        raise ValueError(f"Identifier too long (>128): {name[:40]}...")
    return "[" + name.replace("]", "]]") + "]"


def split_multipart(name: str) -> list[str]:
    """Split ``db.schema.object`` honouring [bracketed] and "quoted" parts."""
    if name is None or not str(name).strip():
        raise ValueError("Object name must not be empty")
    name = str(name).strip()
    parts: list[str] = []
    buf: list[str] = []
    i, n = 0, len(name)
    while i < n:
        ch = name[i]
        if ch in ("[", '"'):
            close = "]" if ch == "[" else '"'
            i += 1
            while i < n:
                if name[i] == close:
                    if i + 1 < n and name[i + 1] == close:
                        buf.append(close)
                        i += 2
                        continue
                    break
                buf.append(name[i])
                i += 1
            else:
                raise ValueError(f"Unterminated quoted identifier in {name!r}")
            i += 1
            continue
        if ch == ".":
            parts.append("".join(buf).strip())
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    parts.append("".join(buf).strip())
    if len(parts) > 4 or any(p == "" for p in parts[-1:]):
        raise ValueError(f"Invalid object name: {name!r}")
    return parts


def quote_multipart(name: str) -> str:
    return ".".join(quote_ident(p) if p else "" for p in split_multipart(name))


# ---------------------------------------------------------------- read-only guard

_WRITE_KEYWORDS = {
    "INSERT", "UPDATE", "DELETE", "MERGE", "TRUNCATE", "CREATE", "ALTER", "DROP", "GRANT", "REVOKE",
    "DENY", "BACKUP", "RESTORE", "DBCC", "KILL", "SHUTDOWN", "RECONFIGURE", "BULK", "INTO", "ENABLE",
    "DISABLE", "CHECKPOINT", "SEND", "RECEIVE", "OPENROWSET", "OPENDATASOURCE", "OPENQUERY", "WRITETEXT",
    "UPDATETEXT", "SETUSER",
}
_READONLY_PROCS = {
    "sp_help", "sp_helptext", "sp_helpindex", "sp_helpconstraint", "sp_helpdb", "sp_helpfile",
    "sp_helpuser", "sp_helprole", "sp_helprolemember", "sp_helplogins", "sp_helprotect", "sp_helpserver",
    "sp_helptrigger", "sp_who", "sp_who2", "sp_lock", "sp_spaceused", "sp_columns", "sp_tables",
    "sp_databases", "sp_server_info", "sp_stored_procedures", "sp_depends", "sp_pkeys", "sp_fkeys",
    "sp_statistics", "sp_describe_first_result_set", "sp_help_job", "sp_help_jobstep",
    "sp_help_jobhistory", "sp_help_jobschedule", "sp_help_schedule", "sp_help_jobactivity",
    "sp_monitor", "sp_configure_list", "xp_msver", "xp_fixeddrives", "sp_readerrorlog",
}
_STATEMENT_START = {
    "SELECT", "WITH", "SET", "DECLARE", "PRINT", "IF", "ELSE", "BEGIN", "END", "WHILE", "RETURN",
    "USE", "WAITFOR", "RAISERROR", "THROW", "TRY", "CATCH", "BREAK", "CONTINUE", "GOTO", "EXEC",
    "EXECUTE", "COMMIT", "ROLLBACK", "SAVE", "TRAN", "TRANSACTION", "FETCH", "OPEN", "CLOSE", "DEALLOCATE",
}
_TOKEN_RE = re.compile(r"@{0,2}[A-Za-z_#][A-Za-z0-9_#$@]*|\.|=|;|,|\(|\)")


class ReadOnlyViolation(PermissionError):
    pass


def check_readonly(sql: str) -> None:
    """Best-effort guard for read-only profiles. Raises :class:`ReadOnlyViolation`.

    It is a guardrail against accidental writes, not a security boundary: the real protection
    for a read-only connection is a SQL login that only has read permissions.
    """
    for batch in split_batches(sql) or [sql]:
        tokens = _TOKEN_RE.findall(strip_comments_and_literals(batch))
        words = list(tokens)
        for idx, tok in enumerate(words):
            upper = tok.upper()
            if upper in _WRITE_KEYWORDS:
                raise ReadOnlyViolation(f"Read-only connection: '{upper}' is not allowed")
            if upper in ("EXEC", "EXECUTE"):
                _check_exec_target(words, idx + 1)
        if words and words[0].upper() not in _STATEMENT_START and words[0] not in (";",):
            # A batch that starts with a bare name is an implicit EXEC.
            _check_exec_target(words, 0)


def _check_exec_target(words: list[str], start: int) -> None:
    j = start
    # EXEC @rc = proc ...
    if j + 1 < len(words) and words[j].startswith("@") and words[j + 1] == "=":
        j += 2
    if j < len(words) and words[j] == "(":
        raise ReadOnlyViolation("Read-only connection: dynamic SQL via EXEC(...) is not allowed")
    name_parts: list[str] = []
    while j < len(words):
        w = words[j]
        if w == ".":
            j += 1
            continue
        if w in (";", ",", "(", ")", "=") or w.startswith("@"):
            break
        name_parts.append(w)
        j += 1
        if j < len(words) and words[j] != ".":
            break
    target = name_parts[-1].lower() if name_parts else ""
    if target not in _READONLY_PROCS:
        raise ReadOnlyViolation(
            f"Read-only connection: executing '{target or '?'}' is not allowed "
            "(only well-known read-only system procedures such as sp_help, sp_who2)"
        )
