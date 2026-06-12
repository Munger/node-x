## @file node_x_sqlite.py
##
## @brief SQLite persistence companion for node_x.
##
## Provides ``NodeDB`` — a thread-safe SQLite store for ``Serialisable``
## node snapshots — and ``DBMixin``, a thin convenience mixin that wires
## ``NodeDB`` operations directly onto node classes.
##
## Usage::
##
##     from node_x_sqlite import NodeDB, DBMixin
##
##     db = NodeDB("cache.db")
##
##     # Save any Serialisable node
##     db.save(week_node)                   # uses node["_key"] automatically
##     db.save(config_node, key="config")   # explicit key for un-keyed nodes
##
##     # Restore — returns None on cache miss
##     week = db.load(WeekNode, "week/2024-01-07")
##     if week is None:
##         week = WeekNode(...)             # fall back to live fetch
##
##     # Context-manager form closes the calling thread's connection on exit
##     with NodeDB("cache.db") as db:
##         db.save(node)
##
## Thread safety
## -------------
## Each calling thread receives its own ``sqlite3`` connection via
## ``threading.local()``.  WAL journal mode allows multiple threads to
## read simultaneously; SQLite serialises concurrent writes at the file
## level.  No external locking is required.
##
## Schema
## ------
## A single table stores all node types::
##
##     snapshots (class_name TEXT, key TEXT, snapshot TEXT, saved_at REAL)
##     PRIMARY KEY (class_name, key)
##
## ``class_name`` is the node's ``__qualname__`` so inner classes and
## subclasses each occupy their own namespace in the same database.
## ``snapshot`` is the JSON string produced by ``node.snapshot()``.
##
## Dependencies
## ------------
## ``sqlite3`` is part of the Python standard library — this module adds
## no external dependencies beyond ``node_x`` itself.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/Munger/node-x
## @par Licence: MIT

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Type, TypeVar

try:
    from . import __version__  # noqa: F401  # package mode
except ImportError:
    from node_x import __version__  # noqa: F401  # standalone mode

T = TypeVar("T")

# ============================================================================
# Schema
# ============================================================================

_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    class_name  TEXT    NOT NULL,
    key         TEXT    NOT NULL,
    snapshot    TEXT    NOT NULL,
    saved_at    REAL    NOT NULL,
    PRIMARY KEY (class_name, key)
);
CREATE INDEX IF NOT EXISTS idx_snapshots_class ON snapshots (class_name);
"""

# ============================================================================
# NodeDB
# ============================================================================


class NodeDB:
    ## @brief Thread-safe SQLite store for Serialisable node snapshots.
    ##
    ## Each instance manages one database file.  Multiple ``NodeDB`` instances
    ## pointing at the same path are safe — SQLite's WAL mode handles
    ## concurrent access at the file level.
    ##
    ## All public methods may be called from any thread.  Connections are
    ## created lazily per thread and reused for the lifetime of the thread.

    def __init__(self, path: str | Path) -> None:
        ## @brief Open (or create) the database at *path* and apply the schema.
        ##
        ## The schema is applied inside ``_connect()`` on every fresh connection
        ## so new threads and reconnects after ``close()`` always have the table.
        ## ``IF NOT EXISTS`` guards make this idempotent.
        ##
        ## @param path  File path for the SQLite database.  Pass ``":memory:"``
        ##              for a transient in-process store (not shared across
        ##              threads since each thread gets its own connection).

        self._path  = str(path)
        self._local = threading.local()

        # Eagerly connect on the calling thread so any schema or file errors
        # surface here rather than on the first save() or load() call.
        self._connect()

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        ## @brief Return the thread-local connection, creating it on first call.
        ##
        ## WAL mode and NORMAL synchronous pragma are set once per connection.
        ## The schema is applied on every fresh connection so that new threads
        ## and reconnects after close() (e.g. after a context-manager exit)
        ## always have the snapshots table.  The IF NOT EXISTS guards make this
        ## a no-op on connections that already have the schema.
        ##
        ## @return Thread-local ``sqlite3.Connection``.

        if not hasattr(self._local, "conn"):
            conn = sqlite3.connect(self._path)
            conn.execute("PRAGMA journal_mode=WAL")
            # NORMAL: sync at WAL checkpoints, not every commit — much faster
            # while still safe against OS crashes.
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(_SCHEMA)
            conn.commit()
            self._local.conn = conn
        return self._local.conn

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def save(self, node: Any, key: Optional[str] = None, deep: bool = False) -> None:
        ## @brief Persist a node's serialised record to the database.
        ##
        ## If *key* is omitted the node's ``_key`` payload field is used
        ## (set automatically by ``GraphMixin.get_or_create()``).  Raises
        ## ``ValueError`` if neither source provides a key.
        ##
        ## When *deep* is ``False`` (the default) the record is produced by
        ## ``node.serialise()``, storing only the node's own scalar fields
        ## with child nodes represented as plain key strings.  Pass
        ## ``deep=True`` to store the full recursive subtree via
        ## ``node.serialise(deep=True)`` (the former ``snapshot()`` behaviour).
        ##
        ## @param node  Any ``Serialisable`` node instance.
        ## @param key   Storage key; defaults to ``node["_key"]``.
        ## @param deep  ``False`` (default) for a shallow per-node record;
        ##              ``True`` for a full recursive snapshot.
        ## @raise ValueError  If no key can be determined.

        if key is None:
            key = node.get("_key") if hasattr(node, "get") else None
        if not key:
            raise ValueError(
                f"NodeDB.save() cannot determine a key for {type(node).__name__}. "
                f"Pass key= explicitly, or use GraphMixin.get_or_create() so the "
                f"node carries _key in its payload."
            )

        serialise = getattr(node, "serialise", None) or getattr(node, "snapshot", None)
        class_name = type(node).__qualname__
        snap_json  = json.dumps(serialise(deep=deep) if serialise else {})
        conn       = self._connect()
        conn.execute(
            "INSERT OR REPLACE INTO snapshots "
            "(class_name, key, snapshot, saved_at) VALUES (?, ?, ?, ?)",
            (class_name, key, snap_json, time.time()),
        )
        conn.commit()

    def load(self, cls: Type[T], key: str) -> Optional[T]:
        ## @brief Restore a node from the database, or return ``None`` on miss.
        ##
        ## Looks up ``(cls.__qualname__, key)``, deserialises the stored JSON,
        ## and passes the plain dict to ``cls.restore()``.  The ``$ref``
        ## resolution registry is created fresh for each load call so that
        ## graph references within a single snapshot are resolved correctly.
        ##
        ## @param cls  The ``Serialisable`` subclass to restore as.
        ## @param key  The key that was used when the snapshot was saved.
        ## @return A restored instance of *cls*, or ``None`` if not cached.

        conn = self._connect()
        row  = conn.execute(
            "SELECT snapshot FROM snapshots WHERE class_name=? AND key=?",
            (cls.__qualname__, key),
        ).fetchone()

        if row is None:
            return None

        # json.loads produces plain dicts/lists — exactly what restore() expects.
        return cls.restore(json.loads(row[0]))

    def delete(self, cls: Type[Any], key: str) -> None:
        ## @brief Remove a single entry from the database.
        ##
        ## A no-op if the entry does not exist.
        ##
        ## @param cls  Node class whose entry should be removed.
        ## @param key  The key under which the snapshot was stored.

        conn = self._connect()
        conn.execute(
            "DELETE FROM snapshots WHERE class_name=? AND key=?",
            (cls.__qualname__, key),
        )
        conn.commit()

    def clear(self, cls: Optional[Type[Any]] = None) -> None:
        ## @brief Delete all entries for *cls*, or every row if *cls* is None.
        ##
        ## Useful for forcing a full re-fetch of a node type, or for wiping
        ## the entire cache between test runs.
        ##
        ## @param cls  Node class to clear; pass ``None`` to clear everything.

        conn = self._connect()
        if cls is None:
            conn.execute("DELETE FROM snapshots")
        else:
            conn.execute(
                "DELETE FROM snapshots WHERE class_name=?",
                (cls.__qualname__,),
            )
        conn.commit()

    def keys(self, cls: Type[Any]) -> list[str]:
        ## @brief Return all stored keys for *cls* in alphabetical order.
        ##
        ## Useful for inspecting what is cached or iterating the full set of
        ## persisted nodes without loading each one.
        ##
        ## @param cls  Node class to query.
        ## @return List of key strings.

        conn = self._connect()
        rows = conn.execute(
            "SELECT key FROM snapshots WHERE class_name=? ORDER BY key",
            (cls.__qualname__,),
        ).fetchall()
        return [row[0] for row in rows]

    def count(self, cls: Optional[Type[Any]] = None) -> int:
        ## @brief Return the number of cached entries for *cls*, or total rows.
        ##
        ## @param cls  Node class to count; pass ``None`` to count all rows.
        ## @return Integer row count.

        conn = self._connect()
        if cls is None:
            return conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        return conn.execute(
            "SELECT COUNT(*) FROM snapshots WHERE class_name=?",
            (cls.__qualname__,),
        ).fetchone()[0]

    def close(self) -> None:
        ## @brief Close the calling thread's connection.
        ##
        ## After this call the thread's next operation will open a fresh
        ## connection.  Other threads' connections are unaffected.
        ##
        ## @return None

        if hasattr(self._local, "conn"):
            self._local.conn.close()
            del self._local.conn

    def __enter__(self) -> "NodeDB":
        ## @brief Return self for use as a context manager.
        return self

    def __exit__(self, *_: Any) -> None:
        ## @brief Close the calling thread's connection on block exit.
        self.close()


# ============================================================================
# DBMixin
# ============================================================================


class DBMixin:
    ## @brief Convenience mixin that wires ``NodeDB`` operations onto a node class.
    ##
    ## Mix this in alongside ``Serialisable`` to give node instances ``db_save``
    ## and ``db_delete`` instance methods and a ``db_load`` classmethod.  The
    ## ``NodeDB`` instance is always passed as an argument so nodes remain
    ## decoupled from any specific database path.
    ##
    ## Example::
    ##
    ##     class WeekNode(DBMixin, GraphMixin, Serialisable, StreamMixin, Node):
    ##         ...
    ##
    ##     db = NodeDB("cache.db")
    ##
    ##     week = WeekNode.db_load("week/2024-01-07", db)
    ##     if week is None:
    ##         week = WeekNode(...)
    ##         week.db_save(db)

    def db_save(self, db: NodeDB, key: Optional[str] = None) -> None:
        ## @brief Persist this node to *db*.
        ##
        ## Delegates to ``db.save(self, key=key)``.  See ``NodeDB.save()``
        ## for key-resolution rules.
        ##
        ## @param db   An open ``NodeDB`` instance.
        ## @param key  Optional explicit key; defaults to ``self["_key"]``.

        db.save(self, key=key)

    @classmethod
    def db_load(cls: Type[T], key: str, db: NodeDB) -> Optional[T]:
        ## @brief Restore an instance of this class from *db*, or ``None``.
        ##
        ## Delegates to ``db.load(cls, key)``.
        ##
        ## @param key  The key under which the snapshot was stored.
        ## @param db   An open ``NodeDB`` instance.
        ## @return A restored instance, or ``None`` on cache miss.

        return db.load(cls, key)

    def db_delete(self, db: NodeDB, key: Optional[str] = None) -> None:
        ## @brief Remove this node's entry from *db*.
        ##
        ## If *key* is omitted the node's ``_key`` payload field is used.
        ##
        ## @param db   An open ``NodeDB`` instance.
        ## @param key  Optional explicit key; defaults to ``self["_key"]``.

        if key is None:
            key = self.get("_key") if hasattr(self, "get") else None  # type: ignore[attr-defined]
        if key:
            db.delete(type(self), key)
