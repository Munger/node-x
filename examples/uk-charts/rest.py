## @file rest.py
##
## @brief RESTEndpoint mixin and node registry for the REST API.
##
## Nodes declare a ``rest_slug`` class variable and inherit ``RESTEndpoint``
## (via ``BaseNode``).  Two things happen automatically:
##
##   - At class-definition time, ``__init_subclass__`` registers the slug →
##     class mapping in ``_types``.
##
##   - At construction time, ``ready()`` registers the live instance in
##     ``_by_id`` and ``_by_key``.
##
## ``server.py`` imports this module and uses the registry for all node
## lookups.  It never needs to import individual node classes.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @par Licence: MIT

from __future__ import annotations

from typing import Any, ClassVar, Dict, Optional, Tuple

# ---------------------------------------------------------------------------
# Registries  (module-level singletons)
# ---------------------------------------------------------------------------

## slug → class  (built at class-definition time via __init_subclass__)
_types: Dict[str, type] = {}

## id(node) → node  (keeps nodes alive; used for /explore/<id> lookups)
_by_id:  Dict[int,            Any] = {}

## (cls, _key) → node  (used for /node/<type>/<key> lookups)
_by_key: Dict[Tuple[type, str], Any] = {}

## Callback fired when a node is first registered: on_register(node, parent_id) -> None
_on_register: Optional[Any] = None


def SetOnRegister(fn) -> None:
    """Register the new-node callback.  Called once at app startup."""
    global _on_register
    _on_register = fn


# ---------------------------------------------------------------------------
# Registry operations
# ---------------------------------------------------------------------------

def Register(node) -> None:
    """Add a node to both registries.  Broadcasts via SSE only on first registration."""
    is_new = id(node) not in _by_id
    _by_id[id(node)] = node
    key = node.get("_key") if callable(getattr(node, "get", None)) else None
    if key:
        _by_key[(type(node), key)] = node
    if is_new and _on_register is not None:
        _on_register(node, getattr(node, "_parent_id", None))


def LookupId(node_id: int) -> Optional[Any]:
    """Return the node for this ``id()``, or ``None``."""
    return _by_id.get(node_id)


def LookupKey(slug: str, key: str) -> Optional[Any]:
    """Return the node for *(slug, key)*, or ``None``."""
    cls = _types.get(slug)
    return _by_key.get((cls, key)) if cls else None


def NodesOfSlug(slug: str) -> list:
    """Return all registered nodes whose REST slug is *slug*."""
    cls = _types.get(slug)
    return [n for n in _by_id.values() if cls and isinstance(n, cls)]


def RootClass() -> Optional[type]:
    """Return the node class declared as the graph root, or ``None``."""
    for cls in _types.values():
        if getattr(cls, "is_root", False):
            return cls
    return None


def Clear() -> None:
    """Clear the live-instance registries.  Call on graph reset."""
    _by_id.clear()
    _by_key.clear()


# ---------------------------------------------------------------------------
# RESTEndpoint mixin
# ---------------------------------------------------------------------------

class RESTEndpoint:
    """Mixin that gives a node class a REST identity and self-registration.

    Concrete node classes declare ``rest_slug`` to opt in::

        class ArtistNode(BaseNode):
            rest_slug = "artist"

    ``data_from_key()`` reconstructs constructor kwargs from a bare REST
    key string so that ``GET /node/artist/<key>`` can resolve a node that
    is not yet in memory.

    ``get_stats()`` is an optional hook for nodes that expose richer
    tooltip data on demand (e.g. chart-run history for releases).
    """

    rest_slug: ClassVar[str]  = ""
    is_root:   ClassVar[bool] = False

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        slug = cls.__dict__.get("rest_slug", "")
        if slug:
            _types[slug] = cls

    def ready(self) -> None:
        """Register self then continue the ready() chain."""
        Register(self)
        super().ready()  # type: ignore[misc]

    @classmethod
    def data_from_key(cls, key: str) -> dict:
        """Return constructor kwargs derivable from *key* alone.

        Override in each concrete class.  Used by REST endpoints to
        resolve a node that is not yet in memory without a full HTTP fetch.
        """
        return {}

    def get_stats(self) -> Optional[dict]:
        """Return on-demand stats for the ``/stats/<id>`` endpoint.

        Returns ``None`` by default (node does not support stats).
        Override in subclasses that expose richer tooltip data.
        """
        return None
