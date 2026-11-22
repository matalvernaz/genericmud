"""Pack-confined subset of MUSHclient's bundled lsqlite3 module."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

_OK = 0
_ERROR = 1
_ROW = 100
_DONE = 101


def _no_attach(action: int, *_details: object) -> int:
    """SQLite authorizer that refuses ATTACH, which VACUUM INTO also goes through.

    open() keeps a pack's database inside its own folder, but SQL could ATTACH, or VACUUM
    INTO, any path the user can write and fill it with chosen text: a sandboxed pack being
    checked for compatibility could leave a .bat in the Startup folder.
    """
    if action == sqlite3.SQLITE_ATTACH:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def make_sqlite_bridge(lua, base_dir: str | None, anchor: Callable[[], Path | None]):
    base = Path(base_dir).resolve() if base_dir else None
    make_iterator = lua.eval(
        "function(items) local i = 0; return function() "
        "i = i + 1; return items[i] end end"
    )

    def resolve(value: object) -> Path | None:
        if base is None:
            return None
        normalized = str(value or "").replace("\\", "/")
        requested = Path(normalized)
        root = anchor() or base
        candidate = requested if requested.is_absolute() else root / requested
        try:
            candidate = candidate.resolve()
        except OSError:
            return None
        return candidate if candidate.is_relative_to(base) else None

    def named_row(row: sqlite3.Row):
        result = lua.table()
        for key in row.keys():
            value = row[key]
            if value is not None:
                result[str(key)] = value
        return result

    def numbered_row(row: sqlite3.Row):
        return lua.table_from([value for value in row if value is not None])

    def iterator(rows: list[object]):
        return make_iterator(lua.table_from(rows))

    def open_database(filename: object = "", *_args: object):
        path = resolve(filename)
        if path is None or not path.parent.is_dir():
            return None
        try:
            # SQLite's own autocommit, with no transaction control of Python's: a pack's
            # "BEGIN IMMEDIATE", its statements and its "COMMIT" are separate exec calls, and
            # Python's legacy mode commits an open transaction before each executescript, so
            # the pack's COMMIT found none and failed (issue #129).
            connection = sqlite3.connect(path, autocommit=True)
            connection.set_authorizer(_no_attach)
            connection.row_factory = sqlite3.Row
        except sqlite3.Error:
            return None

        database = lua.table()
        state = {"error": "", "closed": False}

        def run(sql: str, parameters: tuple[object, ...] = ()) -> sqlite3.Cursor | None:
            if state["closed"]:
                state["error"] = "database is closed"
                return None
            try:
                cursor = connection.execute(sql, parameters)
            except sqlite3.Error as exc:
                state["error"] = str(exc)
                return None
            state["error"] = ""
            return cursor

        def nrows(*args: object):
            cursor = run(str(args[-1]) if args else "")
            rows = [] if cursor is None else [named_row(row) for row in cursor.fetchall()]
            return iterator(rows)

        def rows(*args: object):
            cursor = run(str(args[-1]) if args else "")
            values = [] if cursor is None else [numbered_row(row) for row in cursor.fetchall()]
            return iterator(values)

        def execute(*args: object) -> int:
            sql = str(args[-1]) if args else ""
            if state["closed"]:
                state["error"] = "database is closed"
                return _ERROR
            try:
                connection.executescript(sql)
            except sqlite3.Error as exc:
                state["error"] = str(exc)
                return _ERROR
            state["error"] = ""
            return _OK

        def prepare(*args: object):
            sql = str(args[-1]) if args else ""
            statement = lua.table()
            stmt_state: dict[str, object] = {
                "bound": (),
                "cursor": None,
                "current": None,
                "done": False,
            }

            def bind_values(*values: object) -> int:
                stmt_state["bound"] = tuple(values[1:])
                stmt_state["cursor"] = None
                stmt_state["current"] = None
                stmt_state["done"] = False
                return _OK

            def step(*_args: object) -> int:
                if stmt_state["done"]:
                    return _DONE
                cursor = stmt_state["cursor"]
                if cursor is None:
                    cursor = run(sql, stmt_state["bound"])
                    if cursor is None:
                        stmt_state["done"] = True
                        return _ERROR
                    stmt_state["cursor"] = cursor
                row = cursor.fetchone()
                if row is None:
                    stmt_state["current"] = None
                    stmt_state["done"] = True
                    return _DONE
                stmt_state["current"] = row
                return _ROW

            def get_named_values(*_args: object):
                row = stmt_state["current"]
                return named_row(row) if isinstance(row, sqlite3.Row) else lua.table()

            def get_values(*_args: object):
                row = stmt_state["current"]
                return numbered_row(row) if isinstance(row, sqlite3.Row) else lua.table()

            def reset(*_args: object) -> int:
                cursor = stmt_state["cursor"]
                if isinstance(cursor, sqlite3.Cursor):
                    cursor.close()
                stmt_state["cursor"] = None
                stmt_state["current"] = None
                stmt_state["done"] = False
                return _OK

            def finalize(*_args: object) -> int:
                reset()
                stmt_state["done"] = True
                return _OK

            statement.bind_values = bind_values
            statement.step = step
            statement.get_named_values = get_named_values
            statement.get_values = get_values
            statement.reset = reset
            statement.finalize = finalize
            return statement

        def close(*_args: object) -> int:
            if not state["closed"]:
                connection.close()
                state["closed"] = True
            return _OK

        database.nrows = nrows
        database.rows = rows
        database.exec = execute
        database.execute = execute
        database.prepare = prepare
        database.errmsg = lambda *_a: str(state["error"])
        database.last_insert_rowid = lambda *_a: connection.execute(
            "SELECT last_insert_rowid()"
        ).fetchone()[0]
        database.close_vm = lambda *_a: _OK
        database.close = close
        database.busy_timeout = lambda *_a: _OK
        return database

    sqlite = lua.table()
    sqlite.OK = _OK
    sqlite.ERROR = _ERROR
    sqlite.ROW = _ROW
    sqlite.DONE = _DONE
    sqlite.open = open_database
    return sqlite
