## @file cache.py
##
## @brief HTTP fetch helpers and SQLite node cache for the uk-charts demo.
##
## ``HTTPFetch``
##     Fetches a URL and returns the decoded body.  Retries on 429.
##     Nodes call this directly from ``__init__``.
##
## ``CacheableHTTPNode``
##     Mixin carrying shared request headers and a ``url()`` hook.
##
## ``SQLiteNodeCache``
##     ``NodeCache[str]`` backed by SQLite.  On a miss it constructs
##     the node (which self-populates) and saves it.  On a hit it
##     deserialises and returns.
##
## Schema
## ------
##     cache (class_name TEXT, key TEXT, record TEXT, saved_at REAL)
##     PRIMARY KEY (class_name, key)
##
## @copyright Copyright (c) 2026 Tim Hosking

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional, Type, TypeVar

from node_x.node_x_cache import NodeCache
from rest import LookupKey

_T = TypeVar("_T")

## Application-wide cache singleton.  Assigned at startup; assumed present thereafter.
gCache: Optional["SQLiteNodeCache"] = None


def SetCache(instance: "SQLiteNodeCache") -> None:
    """Assign the application-wide cache singleton.  Call once at startup."""
    global gCache
    gCache = instance


def InitCache(instance: "SQLiteNodeCache") -> None:
    """Initialise the application-wide cache.  Call once at server startup."""
    SetCache(instance)


def CacheGet(cls, key: str, **kwargs):
    """Return existing node from registry, or fetch/restore from cache."""
    existing = LookupKey(getattr(cls, 'rest_slug', ''), key)
    if existing is not None:
        return existing
    return gCache.get(cls, key, **kwargs)


# ============================================================================
# Schema
# ============================================================================

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cache (
    class_name  TEXT    NOT NULL,
    key         TEXT    NOT NULL,
    record      TEXT    NOT NULL,
    saved_at    REAL    NOT NULL,
    PRIMARY KEY (class_name, key)
);
CREATE INDEX IF NOT EXISTS idx_cache_class ON cache (class_name);
"""

# ============================================================================
# SQLiteNodeCache
# ============================================================================


class SQLiteNodeCache(NodeCache[str]):
    ## @brief SQLite-backed node cache.
    ##
    ## Example::
    ##
    ##     cache = SQLiteNodeCache("data.db")
    ##     node  = cache.get(MyNode, "some-key", param="value")

    def __init__(self, path: str | Path) -> None:
        ## @brief Open (or create) the cache database at *path*.
        ##
        ## @param path  File path for the SQLite database.

        self._path  = str(path)
        self._local = threading.local()
        self._connect()

    # -----------------------------------------------------------------------
    # Connection management
    # -----------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        ## @brief Return this thread's SQLite connection, opening it if needed.
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            self._local.conn = sqlite3.connect(self._path)
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn.executescript(_SCHEMA)
            self._local.conn.commit()
        return self._local.conn

    def get(self, cls: Type[_T], key: str, **kwargs: Any) -> Any:
        ## @brief Return a node for *(cls, key)*, from SQLite or freshly constructed.
        ##
        ## Hit: deserialise, apply ``_``-prefixed runtime kwargs, return.
        ## Miss: construct with non-``_`` kwargs (node self-populates in
        ## ``__init__``), save, return.
        ##
        ## @param cls     Node subclass (must be ``Serialisable``).
        ## @param key     Cache key (plain string).
        ## @param kwargs  Constructor args; ``_``-prefixed ones are set as attrs
        ##                rather than forwarded to ``__init__``.
        ## @return        Node instance.

        record = self.load(cls.__qualname__, key)
        if record is not None:
            return self._restore(cls, record, kwargs)

        init_kwargs = {k: v for k, v in kwargs.items() if not k.startswith("_")}
        node = cls(**init_kwargs)
        node["_key"] = str(key)
        for k, v in kwargs.items():
            if k.startswith("_") and v is not None:
                object.__setattr__(node, k, v)
        self.save(cls.__qualname__, key, node.serialise(deep=False))
        return node

    def _restore(self, cls: Type[_T], record: dict, kwargs: dict) -> Any:
        ## @brief Deserialise *record* and apply runtime kwargs.
        node = cls.deserialise(record)
        for k, v in kwargs.items():
            if k.startswith("_") and v is not None:
                object.__setattr__(node, k, v)
        return node

    # -----------------------------------------------------------------------
    # NodeCache storage hooks
    # -----------------------------------------------------------------------

    def load(self, class_name: str, key: str) -> Optional[dict]:
        ## @brief Return the stored record dict, or ``None`` on a cache miss.
        ##
        ## @param class_name  ``type(node).__qualname__``.
        ## @param key         Node's cache key.
        ## @return Deserialised record dict, or ``None``.

        row = self._connect().execute(
            "SELECT record FROM cache WHERE class_name=? AND key=?",
            (class_name, key),
        ).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, class_name: str, key: str, record: dict) -> None:
        ## @brief Upsert *record* into the cache table.
        ##
        ## ``INSERT OR REPLACE`` overwrites any existing row for the same
        ## *(class_name, key)* pair (upsert semantics).
        ##
        ## @param class_name  ``type(node).__qualname__``.
        ## @param key         Node's cache key.
        ## @param record      Serialised node dict from ``node.serialise(deep=False)``.

        conn = self._connect()
        conn.execute(
            "INSERT OR REPLACE INTO cache "
            "(class_name, key, record, saved_at) VALUES (?, ?, ?, ?)",
            (class_name, key, json.dumps(record), time.time()),
        )
        conn.commit()

    def delete(self, class_name: str, key: str) -> None:
        ## @brief Remove the entry for *(class_name, key)*.  A no-op if absent.
        ##
        ## @param class_name  ``type(node).__qualname__``.
        ## @param key         Node's cache key.

        conn = self._connect()
        conn.execute(
            "DELETE FROM cache WHERE class_name=? AND key=?",
            (class_name, key),
        )
        conn.commit()

    def _clear(self, class_name: Optional[str]) -> None:
        ## @brief Delete all entries for *class_name*, or every row if ``None``.
        ##
        ## @param class_name  ``type(node).__qualname__``, or ``None`` to wipe all.

        conn = self._connect()
        if class_name is None:
            conn.execute("DELETE FROM cache")
        else:
            conn.execute("DELETE FROM cache WHERE class_name=?", (class_name,))
        conn.commit()

    # -----------------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------------

    def close(self) -> None:
        ## @brief Close this thread's SQLite connection.

        conn = getattr(self._local, 'conn', None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def shutdown(self) -> None:
        ## @brief Close the SQLite connection.  Call at application exit.
        self.close()

    def __enter__(self) -> "SQLiteNodeCache":
        return self

    def __exit__(self, *_) -> None:
        self.shutdown()
