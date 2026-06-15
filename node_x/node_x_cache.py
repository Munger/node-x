## @file node_x_cache.py
##
## @brief Abstract node-cache protocol for node_x.
##
## Defines ``NodeCache``: an abstract backing store for serialisable nodes.
## Subclasses implement the four storage hooks (``load``, ``save``,
## ``delete``, ``_clear``) and optionally override ``get()`` to add
## thread-safety, key assignment, or any other app-level logic.
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

T = TypeVar("T")  # node class
K = TypeVar("K")  # key type — str, tuple, etc.


class NodeCache(abc.ABC, Generic[K]):
    ## @brief Abstract cache for serialisable node instances.
    ##
    ## The four abstract storage hooks form the backing-store contract;
    ## ``get()``, ``invalidate()``, and ``clear()`` are built on top of them.
    ##
    ## ``get()`` provides a default implementation: load → deserialise on
    ## hit, construct → save on miss.  Subclasses may override ``get()`` to
    ## add thread-safety, key injection, or other app-level concerns.

    # -----------------------------------------------------------------------
    # Storage hooks — implement in subclass
    # -----------------------------------------------------------------------

    @abc.abstractmethod
    def load(self, class_name: str, key: K) -> Optional[dict]:
        ## @brief Return the stored record dict, or ``None`` on a cache miss.
        ##
        ## @param class_name  ``cls.__qualname__``.
        ## @param key         The node's cache key.
        ## @return Deserialised record dict, or ``None``.
        ...

    @abc.abstractmethod
    def save(self, class_name: str, key: K, record: dict) -> None:
        ## @brief Store *record* under *(class_name, key)*.
        ##
        ## Must replace any existing record (upsert semantics).
        ## *record* is the plain dict produced by ``node.serialise(deep=False)``.
        ##
        ## @param class_name  ``cls.__qualname__``.
        ## @param key         The node's cache key.
        ## @param record      Serialised node dict.
        ...

    @abc.abstractmethod
    def delete(self, class_name: str, key: K) -> None:
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
        ## Cache hit: deserialise and return.  Cache miss: construct with
        ## *kwargs*, serialise and save, return.  The node is responsible for
        ## its own initialisation (including any data fetching) inside
        ## ``__init__``; this method does not call ``populate()`` or ``ready()``.
        ##
        ## @param cls     A ``Serialisable`` node subclass.
        ## @param key     Cache key for this node.
        ## @param kwargs  Constructor arguments forwarded to *cls* on a miss.
        ## @return An instance of *cls*.

        record = self.load(cls.__qualname__, key)
        if record is not None:
            return cls.deserialise(record)  # type: ignore[attr-defined]
        node = cls(**kwargs)
        self.save(cls.__qualname__, key, node.serialise(deep=False))  # type: ignore[attr-defined]
        return node

    def invalidate(self, cls: Type[Any], key: K) -> None:
        ## @brief Remove a single cached entry so it will be re-fetched on next ``get()``.
        ##
        ## @param cls  Node class whose entry should be removed.
        ## @param key  The key used when the record was saved.

        self.delete(cls.__qualname__, key)

    def clear(self, cls: Optional[Type[Any]] = None) -> None:
        ## @brief Delete cached entries for *cls*, or every entry if *cls* is ``None``.
        ##
        ## @param cls  Node class to clear; pass ``None`` to clear everything.

        self._clear(cls.__qualname__ if cls is not None else None)
