"""Admin SQL console: one read-only SELECT against the server's own database.

The statement is checked before it runs (single SELECT or WITH … SELECT, no
data-changing words). SQLite also installs an authorizer that denies writes;
Postgres runs the statement in a READ ONLY transaction.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import inspect
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

MAX_ROWS = 200
MAX_SQL_CHARS = 20_000
QUERY_TIMEOUT_S = 8
MAX_CELL_CHARS = 100_000

# Words that never belong in a plain SELECT. REPLACE is a function (`replace(...)`)
# as well as a write statement, so it is handled separately.
_BANNED_WORDS = frozenset({
    "alter",
    "analyze",
    "attach",
    "begin",
    "call",
    "commit",
    "copy",
    "create",
    "delete",
    "detach",
    "drop",
    "exec",
    "execute",
    "explain",
    "grant",
    "insert",
    "into",
    "load",
    "merge",
    "pragma",
    "reindex",
    "revoke",
    "rollback",
    "savepoint",
    "truncate",
    "update",
    "vacuum",
})

_BANNED_FUNCS = frozenset({
    "dblink",
    "dblink_exec",
    "fts3_tokenizer",
    "load_extension",
    "lo_export",
    "lo_import",
    "nextval",
    "pg_cancel_backend",
    "pg_ls_dir",
    "pg_read_binary_file",
    "pg_read_file",
    "pg_reload_conf",
    "pg_terminate_backend",
    "readfile",
    "set_config",
    "setval",
    "writefile",
})


class SelectOnlyError(ValueError):
    """The statement is empty, not a single SELECT, or the database rejected it."""


def validate_select_sql(sql: str) -> str:
    """Return the statement to run, or raise SelectOnlyError."""
    raw = (sql or "").strip()
    if not raw:
        raise SelectOnlyError("Enter a SELECT statement.")
    if len(raw) > MAX_SQL_CHARS:
        raise SelectOnlyError(f"Query is too long (max {MAX_SQL_CHARS} characters).")
    tokens = _tokenize(raw)
    if tokens and tokens[-1] == ("punct", ";"):
        tokens = tokens[:-1]
    if not tokens:
        raise SelectOnlyError("Enter a SELECT statement.")
    if any(tok == ("punct", ";") for tok in tokens):
        raise SelectOnlyError("Only one statement is allowed.")
    _assert_select(tokens, 0, len(tokens))
    return raw


def list_db_schema(db: Session) -> dict[str, Any]:
    """Tables and views on this connection, tables first within alphabetical order."""
    bind = db.get_bind()
    insp = inspect(bind)
    schema = None if bind.dialect.name == "sqlite" else "public"
    entries = [(name, "table") for name in insp.get_table_names(schema=schema)]
    entries += [(name, "view") for name in insp.get_view_names(schema=schema)]
    entries.sort(key=lambda item: item[0].lower())
    tables: list[dict[str, Any]] = []
    for name, kind in entries:
        columns = []
        for col in insp.get_columns(name, schema=schema):
            columns.append({
                "name": col["name"],
                "type": str(col["type"]),
                "nullable": bool(col.get("nullable", True)),
            })
        tables.append({"name": name, "kind": kind, "columns": columns})
    return {"tables": tables}


def run_admin_select(db: Session, sql: str) -> dict[str, Any]:
    """Run one SELECT and return at most MAX_ROWS rows."""
    statement = validate_select_sql(sql)
    # Auth already read from this session, so the transaction is open. Roll it
    # back so Postgres can start a READ ONLY transaction as its first command.
    db.rollback()
    bind = db.get_bind()
    dialect = bind.dialect.name
    sa_conn = db.connection()
    raw = sa_conn.connection
    authorizer_set = False
    result = None
    try:
        if dialect == "sqlite":
            _install_sqlite_guard(raw)
            authorizer_set = True
        elif dialect == "postgresql":
            sa_conn.exec_driver_sql("SET TRANSACTION READ ONLY")
            sa_conn.exec_driver_sql("SET LOCAL statement_timeout = '8s'")
        result = sa_conn.exec_driver_sql(statement)
        columns = list(result.keys())
        rows: list[list[Any]] = []
        truncated = False
        for i, row in enumerate(result):
            if i >= MAX_ROWS:
                truncated = True
                break
            rows.append([_jsonable(value) for value in row])
        return {
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "truncated": truncated,
        }
    except SelectOnlyError:
        raise
    except (SQLAlchemyError, sqlite3.Error) as exc:
        raise SelectOnlyError(_db_error_message(exc)) from exc
    finally:
        if result is not None:
            result.close()
        if authorizer_set:
            _clear_sqlite_guard(raw)
        db.rollback()


def _install_sqlite_guard(raw: Any) -> None:
    if not hasattr(raw, "set_authorizer") or not hasattr(raw, "set_progress_handler"):
        raise SelectOnlyError("Read-only queries are not available for this database.")
    deadline = time.monotonic() + QUERY_TIMEOUT_S

    def progress() -> int:
        return 1 if time.monotonic() > deadline else 0

    def authorizer(action: int, arg1: Any, arg2: Any, db_name: Any, source: Any) -> int:
        del arg2, db_name, source
        if action == sqlite3.SQLITE_FUNCTION:
            name = arg1 if isinstance(arg1, str) else ""
            if name.lower() in _BANNED_FUNCS or name.lower() == "load_extension":
                return sqlite3.SQLITE_DENY
        allowed = (
            sqlite3.SQLITE_SELECT,
            sqlite3.SQLITE_READ,
            sqlite3.SQLITE_FUNCTION,
            sqlite3.SQLITE_TRANSACTION,
        )
        recursive = getattr(sqlite3, "SQLITE_RECURSIVE", None)
        if action in allowed or (recursive is not None and action == recursive):
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    raw.set_authorizer(authorizer)
    raw.set_progress_handler(progress, 10_000)


def _clear_sqlite_guard(raw: Any) -> None:
    try:
        raw.set_authorizer(None)
    except sqlite3.Error:
        pass
    try:
        raw.set_progress_handler(None, 0)
    except sqlite3.Error:
        pass


def _db_error_message(exc: Exception) -> str:
    orig = getattr(exc, "orig", None)
    msg = str(orig or exc).strip()
    lower = msg.lower()
    if "not authorized" in lower:
        return "Only SELECT queries are allowed."
    if "interrupted" in lower:
        return "Query timed out."
    line = msg.splitlines()[0] if msg else "Query failed."
    return line[:500]


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        if isinstance(value, str) and len(value) > MAX_CELL_CHARS:
            return value[:MAX_CELL_CHARS] + "… (truncated)"
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return None
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        blob = bytes(value)
        text = blob.hex()
        if len(text) > 512:
            return text[:512] + "… (truncated)"
        return text
    text = str(value)
    if len(text) > MAX_CELL_CHARS:
        return text[:MAX_CELL_CHARS] + "… (truncated)"
    return text


def _tokenize(sql: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch.isspace():
            i += 1
            continue
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            i += 2
            while i < n and sql[i] not in "\n\r":
                i += 1
            continue
        if ch == "/" and i + 1 < n and sql[i + 1] == "*":
            end = sql.find("*/", i + 2)
            if end < 0:
                raise SelectOnlyError("Unclosed comment.")
            i = end + 2
            continue
        if ch == "'":
            i = _skip_quoted(sql, i, "'")
            tokens.append(("str", "''"))
            continue
        if ch in {'"', "`"}:
            i = _skip_quoted(sql, i, ch)
            tokens.append(("ident", ch * 2))
            continue
        if ch == "$":
            j = i + 1
            while j < n and (sql[j].isalnum() or sql[j] == "_"):
                j += 1
            if j < n and sql[j] == "$":
                tag = sql[i : j + 1]
                end = sql.find(tag, j + 1)
                if end < 0:
                    raise SelectOnlyError("Unclosed string.")
                i = end + len(tag)
                tokens.append(("str", "$$"))
                continue
            tokens.append(("punct", "$"))
            i += 1
            continue
        if ch.isalpha() or ch == "_":
            j = i + 1
            while j < n and (sql[j].isalnum() or sql[j] == "_"):
                j += 1
            word = sql[i:j]
            if j < n and sql[j] == "'" and word.lower() in {"e", "x", "b", "n"}:
                i = _skip_quoted(sql, j, "'")
                tokens.append(("str", "''"))
                continue
            tokens.append(("word", word))
            i = j
            continue
        if ch in "0123456789" or (ch == "." and i + 1 < n and sql[i + 1].isdigit()):
            j = i + 1
            while j < n and (sql[j].isalnum() or sql[j] in "._"):
                j += 1
            tokens.append(("num", sql[i:j]))
            i = j
            continue
        if ch in ":|><!" and i + 1 < n and sql[i + 1] in ":=|><":
            tokens.append(("punct", sql[i : i + 2]))
            i += 2
            continue
        tokens.append(("punct", ch))
        i += 1
    return tokens


def _skip_quoted(sql: str, start: int, quote: str) -> int:
    i = start + 1
    n = len(sql)
    while i < n:
        if sql[i] == quote:
            if i + 1 < n and sql[i + 1] == quote:
                i += 2
                continue
            return i + 1
        i += 1
    raise SelectOnlyError("Unclosed string.")


def _assert_select(tokens: list[tuple[str, str]], start: int, end: int) -> None:
    if start >= end:
        raise SelectOnlyError("Expected a SELECT statement.")
    _scan_banned(tokens, start, end)
    kind, text = tokens[start]
    if kind != "word":
        raise SelectOnlyError("Query must start with SELECT.")
    word = text.lower()
    if word == "select":
        return
    if word == "with":
        _assert_with(tokens, start, end)
        return
    raise SelectOnlyError("Query must start with SELECT.")


def _assert_with(tokens: list[tuple[str, str]], start: int, end: int) -> None:
    i = start + 1
    if i < end and _is_word(tokens[i], "recursive"):
        i += 1
    if i >= end:
        raise SelectOnlyError("WITH query is missing its SELECT.")
    while True:
        if i >= end or tokens[i][0] not in {"word", "ident"}:
            raise SelectOnlyError("WITH query is missing a name.")
        i += 1
        if i < end and tokens[i] == ("punct", "("):
            i = _skip_parens(tokens, i, end)
        if i >= end or not _is_word(tokens[i], "as"):
            raise SelectOnlyError("WITH query is missing AS.")
        i += 1
        if i < end and _is_word(tokens[i], "not"):
            i += 1
            if i >= end or not _is_word(tokens[i], "materialized"):
                raise SelectOnlyError("WITH query is missing a subquery.")
            i += 1
        elif i < end and _is_word(tokens[i], "materialized"):
            i += 1
        if i >= end or tokens[i] != ("punct", "("):
            raise SelectOnlyError("WITH query is missing a subquery.")
        body_start = i + 1
        i = _skip_parens(tokens, i, end)
        _assert_select(tokens, body_start, i - 1)
        if i < end and tokens[i] == ("punct", ","):
            i += 1
            continue
        break
    _assert_select(tokens, i, end)


def _scan_banned(tokens: list[tuple[str, str]], start: int, end: int) -> None:
    for i in range(start, end):
        kind, text = tokens[i]
        if kind != "word":
            continue
        word = text.lower()
        nxt = tokens[i + 1] if i + 1 < end else None
        if word == "replace":
            if nxt == ("punct", "("):
                continue
            raise SelectOnlyError("Only SELECT queries are allowed (replace is not).")
        if word in _BANNED_WORDS or word in _BANNED_FUNCS:
            raise SelectOnlyError(f"Only SELECT queries are allowed ({word} is not).")


def _skip_parens(tokens: list[tuple[str, str]], open_at: int, end: int) -> int:
    if open_at >= end or tokens[open_at] != ("punct", "("):
        raise SelectOnlyError("Expected '('.")
    depth = 0
    i = open_at
    while i < end:
        if tokens[i] == ("punct", "("):
            depth += 1
        elif tokens[i] == ("punct", ")"):
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise SelectOnlyError("Unclosed parenthesis.")


def _is_word(token: tuple[str, str], word: str) -> bool:
    return token[0] == "word" and token[1].lower() == word
