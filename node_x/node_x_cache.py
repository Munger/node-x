## @file node_x_cache.py
##
## @brief Abstract fetch-and-cache protocol for node_x nodes.
##
## Defines ``NodeDataSource``, ``CacheableNode``, and ``NodeCache``.
## All three are abstract — no backing store or transport is assumed.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/Munger/node-x
## @par Licence: MIT

from __future__ import annotations

import abc
from typing import Any, Generic, Optional, Type, TypeVar

try:
    from . import __version__  # noqa: F401  # package mode
except ImportError:
    from node_x import __version__  # noqa: F401  # standalone mode

T = TypeVar("T")  # node class, used in NodeCache.get()
D = TypeVar("D")  # data type flowing from NodeDataSource.fetch() into CacheableNode.populate()
K = TypeVar("K")  # key type used by the backing store — str, tuple, SQL query, etc.

# ============================================================================
# NodeDataSource
# ============================================================================


class NodeDataSource(abc.ABC, Generic[D]):
    ## @brief Abstract source of raw string content for a node.
    ##
    ## Subclass this and implement ``fetch()`` to adapt any data source —
    ## HTTP URL, local file, third-party API, test fixture — to the interface
    ## that ``NodeCache`` expects.
    ##
    ## A ``NodeDataSource`` is intentionally stateless and cheap to construct;
    ## node classes typically create a fresh instance inside ``_data_source()``
    ## on each call rather than storing one.
    ##
    ## Example::
    ##
    ##     class FileDataSource(NodeDataSource):
    ##         def __init__(self, path: str) -> None:
    ##             self.path = path
    ##         def fetch(self) -> str:
    ##             return Path(self.path).read_text(encoding="utf-8")

    @abc.abstractmethod
    def fetch(self) -> D:
        ## @brief Retrieve and return the raw content for this source.
        ##
        ## Implementations should raise an exception on failure rather than
        ## returning an empty string, so that callers can distinguish a genuine
        ## empty document from a retrieval error.
        ##
        ## @return Raw content as a UTF-8 string.
        ## @raise Exception  On any retrieval failure.
        ...


# ============================================================================
# CacheableNode
# ============================================================================


class CacheableNode(Generic[D]):
    ## @brief Mixin for nodes that can be fetched and cached.
    ##
    ## Mix this in alongside ``Serialisable`` and any other node base classes.
    ## Override ``_data_source()`` to declare where the node's content comes
    ## from, and ``populate()`` to decode that content into payload fields.
    ##
    ## ``NodeCache.get()`` is the entry point — it either restores a cached
    ## node via ``cls.restore()`` or instantiates a fresh one and calls
    ## ``populate()`` on it.  The node never needs to know which path was taken.

    def _data_source(self) -> Optional[NodeDataSource[D]]:
        ## @brief Return the data source for this node, or ``None``.
        ##
        ## Return ``None`` for structural nodes that have no upstream resource
        ## to fetch.  ``NodeCache.get()`` will instantiate and return such
        ## nodes without fetching or caching.
        ##
        ## @return A ``NodeDataSource`` instance, or ``None``.

        return None

    def populate(self, data: D) -> None:
        ## @brief Decode *data* and initialise this node's payload from it.
        ##
        ## Override this in each concrete node class to parse the raw string
        ## returned by ``_data_source().fetch()`` and store the results as
        ## payload fields on ``self``.  Any exception raised here propagates
        ## to the caller and the node is not saved to the cache.
        ##
        ## @param data  Raw string content returned by ``_data_source().fetch()``.

        pass


# ============================================================================
# NodeCache
# ============================================================================


class NodeCache(abc.ABC, Generic[K]):
    ## @brief Abstract cache for ``CacheableNode`` instances.
    ##
    ## The main entry point is ``get()``: given a node class and key it either
    ## restores the node from the backing store or instantiates a fresh one,
    ## calls ``populate()``, and saves it.  Concrete subclasses supply the
    ## backing store by implementing ``_load()``, ``_save()``, ``_delete()``,
    ## and ``_clear()``.
    ##
    ## Example (using a hypothetical concrete subclass)::
    ##
    ##     cache = MyConcreteCache(...)
    ##     node  = cache.get(ChartNode, key, date="1993-05-01", slug="singles")

    # -----------------------------------------------------------------------
    # Storage hooks — implement in subclass
    # -----------------------------------------------------------------------

    @abc.abstractmethod
    def _load(self, class_name: str, key: K) -> Optional[dict]:
        ## @brief Return the stored record dict, or ``None`` on a cache miss.
        ##
        ## @param class_name  ``cls.__qualname__``.
        ## @param key         The node's cache key.
        ## @return Deserialised record dict, or ``None``.
        ...

    @abc.abstractmethod
    def _save(self, class_name: str, key: K, record: dict) -> None:
        ## @brief Store *record* under *(class_name, key)*.
        ##
        ## Must replace any existing record for the same key (upsert semantics).
        ## *record* is the plain dict produced by ``node.serialise(deep=False)``.
        ##
        ## @param class_name  ``cls.__qualname__``.
        ## @param key         The node's cache key.
        ## @param record      Serialised node dict from ``node.serialise(deep=False)``.
        ...

    @abc.abstractmethod
    def _delete(self, class_name: str, key: K) -> None:
        ## @brief Remove the entry for *(class_name, key)*.  A no-op if absent.
        ...

    @abc.abstractmethod
    def _clear(self, class_name: Optional[str]) -> None:
        ## @brief Delete all entries for *class_name*, or everything if ``None``.
        ...

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def get(self, cls: Type[T], key: K, **kwargs: Any) -> T:
        ## @brief Return a node of type *cls* for *key*, from cache or fresh.
        ##
        ## If a record exists in the backing store, ``cls.restore()`` is used
        ## to reconstruct and return the node.  Otherwise *cls* is instantiated
        ## with *kwargs*, ``populate()`` is called with the data from
        ## ``_data_source()``, the result is saved, and the node is returned.
        ##
        ## Nodes whose ``_data_source()`` returns ``None`` are instantiated and
        ## cached without a fetch step — the node may still carry payload worth
        ## persisting (e.g. preferences or computed state).
        ##
        ## @param cls     A ``CacheableNode`` subclass that is also ``Serialisable``.
        ## @param key     Cache key for this node.
        ## @param kwargs  Constructor arguments forwarded to *cls* on a cache miss.
        ## @return An instance of *cls*, fully populated.
        ## @raise Exception  On fetch or parse failure.

        record = self._load(cls.__qualname__, key)
        if record is not None:
            return cls.restore(record)  # type: ignore[attr-defined]

        node = cls(**kwargs)
        ds = node._data_source()  # type: ignore[attr-defined]
        if ds is not None:
            node.populate(ds.fetch())  # type: ignore[attr-defined]
        self._save(cls.__qualname__, key, node.serialise(deep=False))  # type: ignore[attr-defined]
        return node

    def invalidate(self, cls: Type[Any], key: K) -> None:
        ## @brief Remove a single cached entry so it will be re-fetched on next ``get()``.
        ##
        ## @param cls  Node class whose entry should be removed.
        ## @param key  The key used when the record was saved.

        self._delete(cls.__qualname__, key)

    def clear(self, cls: Optional[Type[Any]] = None) -> None:
        ## @brief Delete cached entries for *cls*, or every entry if *cls* is ``None``.
        ##
        ## @param cls  Node class to clear; pass ``None`` to clear everything.

        self._clear(cls.__qualname__ if cls is not None else None)
