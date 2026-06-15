## @file node_x_query.py
##
## @brief Query, filter, and traverse node_x graphs and collections.
##
## Provides composable predicates, a chainable lazy query builder, a fast
## field index, and standalone convenience functions.  Everything works on
## any iterable of nodes — NodeList, plain list, generator — and the query
## builder supports graph traversal via ``.nodes()`` and ``.traverse()``.
##
## Quick reference::
##
##     from node_x_query import query, FieldEquals, FieldLt, NodeIndex
##
##     # Flat query
##     query(products)
##         .where(FieldEquals("category", "Coffee") & FieldLt("price", 15))
##         .order_by("price")
##         .first()
##
##     # Graph traversal
##     query(film_graph)
##         .nodes(Film)
##         .traverse("director")
##         .where(FieldEquals("name", "Kubrick"))
##         .back()
##         .order_by("year")
##         .all()
##
##     # Fast lookup
##     index = NodeIndex(products, "sku")
##     node  = index["A003"]
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/Munger/node-x
## @par Licence: MIT

from __future__ import annotations

import abc
import re
from typing import Any, Callable, Dict, Hashable, Iterable, List, Optional, Tuple, Type

try:
    from .node_x import Node, NodeList
    from . import __version__  # noqa: F401
except ImportError:
    from node_x import Node, NodeList
    from node_x import __version__  # noqa: F401


# ============================================================================
# Predicates
# ============================================================================


class Predicate(abc.ABC):
    ## @brief Abstract base for node predicates.
    ##
    ## Subclass and implement ``matches()`` to create a predicate.
    ## Instances compose naturally::
    ##
    ##     p = FieldEquals("category", "Coffee") & FieldLt("price", 15)
    ##     ~HasField("director")                 # negation

    @abc.abstractmethod
    def matches(self, node: Node) -> bool:
        ## @brief Return True if *node* satisfies this predicate.
        ...

    def __and__(self, other: Predicate) -> Predicate:
        return _All(self, other)

    def __or__(self, other: Predicate) -> Predicate:
        return _Any(self, other)

    def __invert__(self) -> Predicate:
        return _Not(self)


class _All(Predicate):
    def __init__(self, *predicates: Predicate) -> None:
        self._predicates = predicates

    def matches(self, node: Node) -> bool:
        return all(p.matches(node) for p in self._predicates)


class _Any(Predicate):
    def __init__(self, *predicates: Predicate) -> None:
        self._predicates = predicates

    def matches(self, node: Node) -> bool:
        return any(p.matches(node) for p in self._predicates)


class _Not(Predicate):
    def __init__(self, predicate: Predicate) -> None:
        self._predicate = predicate

    def matches(self, node: Node) -> bool:
        return not self._predicate.matches(node)


class Always(Predicate):
    ## @brief Matches every node — useful as a no-op default.

    def matches(self, node: Node) -> bool:
        return True


class HasField(Predicate):
    ## @brief Match nodes that have *field* set to a non-None value.

    def __init__(self, field: str) -> None:
        self._field = field

    def matches(self, node: Node) -> bool:
        return node.get(self._field) is not None


class FieldEquals(Predicate):
    ## @brief Match nodes where ``node[field] == value``.

    def __init__(self, field: str, value: Any) -> None:
        self._field = field
        self._value = value

    def matches(self, node: Node) -> bool:
        return node.get(self._field) == self._value


class FieldNot(Predicate):
    ## @brief Match nodes where ``node[field] != value``.

    def __init__(self, field: str, value: Any) -> None:
        self._field = field
        self._value = value

    def matches(self, node: Node) -> bool:
        return node.get(self._field) != self._value


class FieldLt(Predicate):
    ## @brief Match nodes where ``node[field] < value``.

    def __init__(self, field: str, value: Any) -> None:
        self._field = field
        self._value = value

    def matches(self, node: Node) -> bool:
        v = node.get(self._field)
        return v is not None and v < self._value


class FieldLte(Predicate):
    ## @brief Match nodes where ``node[field] <= value``.

    def __init__(self, field: str, value: Any) -> None:
        self._field = field
        self._value = value

    def matches(self, node: Node) -> bool:
        v = node.get(self._field)
        return v is not None and v <= self._value


class FieldGt(Predicate):
    ## @brief Match nodes where ``node[field] > value``.

    def __init__(self, field: str, value: Any) -> None:
        self._field = field
        self._value = value

    def matches(self, node: Node) -> bool:
        v = node.get(self._field)
        return v is not None and v > self._value


class FieldGte(Predicate):
    ## @brief Match nodes where ``node[field] >= value``.

    def __init__(self, field: str, value: Any) -> None:
        self._field = field
        self._value = value

    def matches(self, node: Node) -> bool:
        v = node.get(self._field)
        return v is not None and v >= self._value


class FieldIn(Predicate):
    ## @brief Match nodes where ``node[field]`` is in *values*.

    def __init__(self, field: str, values: Iterable[Any]) -> None:
        self._field = field
        self._values = set(values)

    def matches(self, node: Node) -> bool:
        return node.get(self._field) in self._values


class FieldMatches(Predicate):
    ## @brief Match nodes where *field* is a string matching *pattern*.
    ##
    ## Uses ``re.search`` so the pattern may match anywhere in the string.
    ## Anchor with ``^`` / ``$`` for full-string matching.

    def __init__(self, field: str, pattern: str) -> None:
        self._field = field
        self._re = re.compile(pattern)

    def matches(self, node: Node) -> bool:
        v = node.get(self._field)
        return isinstance(v, str) and bool(self._re.search(v))


class IsType(Predicate):
    ## @brief Match nodes that are instances of *cls*.

    def __init__(self, cls: type) -> None:
        self._cls = cls

    def matches(self, node: Node) -> bool:
        return isinstance(node, self._cls)


# ============================================================================
# Query builder
# ============================================================================

# Internally the query tracks a list of node "paths" — tuples of nodes where
# path[-1] is the current node and earlier elements are traversal ancestors.
# This makes .back() trivial (pop the last element) and supports arbitrary
# traversal depth with no extra bookkeeping.

_Path = Tuple[Node, ...]


class Query:
    ## @brief Chainable lazy query builder over any iterable of nodes.
    ##
    ## Construct via ``query(source)`` rather than directly.  All chaining
    ## methods return a new ``Query`` — nothing is executed until a terminal
    ## method (``.all()``, ``.first()``, ``.count()``, ``.group_by()``) is
    ## called.
    ##
    ## The *source* may be a single ``Node``, a ``NodeList``, a plain list,
    ## or any iterable of nodes.

    def __init__(self, source: Any, _ops: Optional[List[tuple]] = None) -> None:
        self._source = source
        self._ops: List[tuple] = _ops or []

    # ------------------------------------------------------------------
    # Chainable — each returns a new Query
    # ------------------------------------------------------------------

    def where(self, predicate: Predicate) -> Query:
        ## @brief Keep only nodes that satisfy *predicate*.
        if not isinstance(predicate, Predicate):
            raise TypeError(
                f"Query.where() expects a Predicate; got {type(predicate).__name__}. "
                f"Pass an instance such as FieldEquals('field', value)."
            )
        return self._chain(("where", predicate))

    def order_by(self, field: str, *, reverse: bool = False) -> Query:
        ## @brief Sort current nodes by *field*.  Nodes missing *field* sort last.
        return self._chain(("order_by", field, reverse))

    def unique_by(self, fn: Callable[[Node], Hashable]) -> Query:
        ## @brief Remove duplicate nodes by the key returned by *fn*.
        ## The first occurrence of each key is kept.
        return self._chain(("unique_by", fn))

    def nodes(self, cls: type) -> Query:
        ## @brief Enter a Graph's ``list_fields``, yielding nodes of type *cls*.
        ##
        ## Scans ``list_fields`` on the current node's class for collections
        ## whose declared item type is a subclass of *cls*, then yields every
        ## matching item.
        ##
        ## @raise TypeError  If *cls* is not a type.
        if not isinstance(cls, type):
            raise TypeError(
                f"Query.nodes() expects a type; got {type(cls).__name__} {cls!r}. "
                f"Pass a Node subclass such as .nodes(Film)."
            )
        return self._chain(("nodes", cls))

    def traverse(self, field: str) -> Query:
        ## @brief Follow *field* as a node reference or node list.
        ##
        ## If the field holds a single ``Node`` it is followed directly.
        ## If it holds a ``NodeList`` every item is followed.
        ## Nodes where the field is absent or holds a non-node value are skipped.
        return self._chain(("traverse", field))

    def back(self) -> Query:
        ## @brief Return to the node before the last traversal step.
        ##
        ## Deduplicates by identity so each ancestor node appears at most once,
        ## regardless of how many descendant paths led back to it.
        return self._chain(("back",))

    # ------------------------------------------------------------------
    # Terminals — execute the pipeline and return a result
    # ------------------------------------------------------------------

    def __iter__(self):
        ## @brief Iterate matching nodes directly — a Query is itself iterable.
        return (p[-1] for p in self._run())

    def all(self) -> NodeList:
        ## @brief Execute and return all matching nodes as a ``NodeList``.
        return NodeList(self)

    def first(self) -> Optional[Node]:
        ## @brief Execute and return the first matching node, or ``None``.
        for p in self._run():
            return p[-1]
        return None

    def count(self) -> int:
        ## @brief Execute and return the number of matching nodes.
        return sum(1 for _ in self._run())

    def group_by(self, fn: Callable[[Node], Hashable]) -> Dict[Any, NodeList]:
        ## @brief Execute and partition results into a ``dict`` of ``NodeList``\s.
        result: Dict[Any, NodeList] = {}
        for p in self._run():
            k = fn(p[-1])
            result.setdefault(k, NodeList()).append(p[-1])
        return result

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _chain(self, op: tuple) -> Query:
        return Query(self._source, self._ops + [op])

    def _iter_source(self) -> List[_Path]:
        src = self._source
        if isinstance(src, Node):
            return [(src,)]
        return [(n,) for n in src]

    def _run(self) -> List[_Path]:
        paths: List[_Path] = self._iter_source()

        for op in self._ops:
            tag = op[0]

            if tag == "where":
                predicate = op[1]
                paths = [p for p in paths if predicate.matches(p[-1])]

            elif tag == "order_by":
                field, reverse = op[1], op[2]
                paths = sorted(
                    paths,
                    key=lambda p, f=field: (p[-1].get(f) is None, p[-1].get(f)),
                    reverse=reverse,
                )

            elif tag == "unique_by":
                fn = op[1]
                seen: set = set()
                deduped: List[_Path] = []
                for p in paths:
                    k = fn(p[-1])
                    if k not in seen:
                        seen.add(k)
                        deduped.append(p)
                paths = deduped

            elif tag == "nodes":
                cls = op[1]
                new_paths: List[_Path] = []
                for p in paths:
                    node = p[-1]
                    lf = getattr(type(node), "list_fields", {})
                    for field, (_, item_cls) in lf.items():
                        if issubclass(item_cls, cls):
                            lst = node.get(field)
                            if lst:
                                for item in lst:
                                    if isinstance(item, cls):
                                        new_paths.append(p + (item,))
                paths = new_paths

            elif tag == "traverse":
                field = op[1]
                new_paths = []
                for p in paths:
                    value = p[-1].get(field)
                    if value is None:
                        continue
                    if isinstance(value, NodeList):
                        for item in value:
                            if isinstance(item, Node):
                                new_paths.append(p + (item,))
                    elif isinstance(value, Node):
                        new_paths.append(p + (value,))
                paths = new_paths

            elif tag == "back":
                seen_ids: set = set()
                deduped = []
                for p in paths:
                    if len(p) < 2:
                        continue
                    prev = p[:-1]
                    nid = id(prev[-1])
                    if nid not in seen_ids:
                        seen_ids.add(nid)
                        deduped.append(prev)
                paths = deduped

        return paths


# ============================================================================
# NodeIndex
# ============================================================================


class NodeIndex:
    ## @brief Fast O(1) lookup of nodes by a field value.
    ##
    ## Build once from any iterable of nodes, then look up by value.
    ## Nodes without the indexed field are silently skipped.
    ## Multiple nodes may share a value — ``get_all()`` returns them all.
    ##
    ## Example::
    ##
    ##     index = NodeIndex(products, "sku")
    ##     node  = index["A003"]
    ##     hits  = index.get_all("Coffee")

    def __init__(self, nodes: Iterable[Node], field: str) -> None:
        self._field = field
        self._index: Dict[Any, NodeList] = {}
        for node in nodes:
            v = node.get(field)
            if v is not None:
                self._index.setdefault(v, NodeList()).append(node)

    def __getitem__(self, value: Any) -> Node:
        ## @brief Return the first node where ``field == value``.
        ## @raise KeyError  If no node has that value.
        lst = self._index[value]
        return lst[0]

    def get(self, value: Any, default: Optional[Node] = None) -> Optional[Node]:
        ## @brief Return the first node where ``field == value``, or *default*.
        lst = self._index.get(value)
        return lst[0] if lst else default

    def get_all(self, value: Any) -> NodeList:
        ## @brief Return all nodes where ``field == value`` as a ``NodeList``.
        return NodeList(self._index.get(value, NodeList()))

    def __contains__(self, value: Any) -> bool:
        return value in self._index

    def keys(self) -> Iterable[Any]:
        return self._index.keys()


# ============================================================================
# Standalone convenience functions
# ============================================================================


def query(source: Any) -> Query:
    ## @brief Wrap *source* in a ``Query`` builder and return it.
    ##
    ## *source* may be a ``Graph`` node, a ``NodeList``, a plain list, or any
    ## iterable of nodes.  Use ``.nodes(SomeType)`` to enter a Graph's typed
    ## collections.
    return Query(source)


def where(nodes: Iterable[Node], predicate: Predicate) -> NodeList:
    ## @brief Return a new ``NodeList`` containing only nodes matching *predicate*.
    return NodeList(n for n in nodes if predicate.matches(n))


def group_by(
    nodes: Iterable[Node], fn: Callable[[Node], Hashable]
) -> Dict[Any, NodeList]:
    ## @brief Partition *nodes* into a ``dict`` of ``NodeList``\s keyed by *fn(node)*.
    result: Dict[Any, NodeList] = {}
    for node in nodes:
        k = fn(node)
        result.setdefault(k, NodeList()).append(node)
    return result


def unique_by(
    nodes: Iterable[Node], fn: Callable[[Node], Hashable]
) -> NodeList:
    ## @brief Return a ``NodeList`` with duplicates removed, keyed by *fn(node)*.
    ## First occurrence of each key is kept.
    seen: set = set()
    result = NodeList()
    for node in nodes:
        k = fn(node)
        if k not in seen:
            seen.add(k)
            result.append(node)
    return result


def order_by(
    nodes: Iterable[Node], field: str, *, reverse: bool = False
) -> NodeList:
    ## @brief Return a new ``NodeList`` sorted by *field*.  Missing values sort last.
    return NodeList(
        sorted(
            nodes,
            key=lambda n, f=field: (n.get(f) is None, n.get(f)),
            reverse=reverse,
        )
    )


# ============================================================================
# Set operations
# ============================================================================
#
# Identity is determined by _key when present, falling back to object identity
# (id()) for nodes without one.  Pass a custom *key* function to override.
#
# All three functions are generators — nothing is materialised until consumed.
# intersect() and difference() must scan *b* upfront to build a key set, but
# *a* is consumed lazily so early exit still works.


def _node_id(node: Node) -> Hashable:
    k = node.get("_key") if hasattr(node, "get") else None
    return k if k is not None else id(node)


def union(
    a: Iterable[Node],
    b: Iterable[Node],
    *,
    key: Callable[[Node], Hashable] = _node_id,
) -> Iterable[Node]:
    ## @brief Yield every node from *a* and *b*, skipping duplicates.
    ##
    ## Identity is determined by *key* — defaults to ``_key`` when present,
    ## ``id()`` otherwise.  Order is: all of *a* first, then new nodes from *b*.
    ##
    ## @param a    First source — any iterable of nodes.
    ## @param b    Second source — any iterable of nodes.
    ## @param key  Function mapping a node to a hashable identity value.
    ## @return     Generator of deduplicated nodes.

    seen: set = set()
    for node in a:
        k = key(node)
        if k not in seen:
            seen.add(k)
            yield node
    for node in b:
        k = key(node)
        if k not in seen:
            seen.add(k)
            yield node


def intersect(
    a: Iterable[Node],
    b: Iterable[Node],
    *,
    key: Callable[[Node], Hashable] = _node_id,
) -> Iterable[Node]:
    ## @brief Yield nodes from *a* whose identity also appears in *b*.
    ##
    ## *b* is fully consumed upfront to build a key set; *a* is consumed lazily.
    ##
    ## @param a    Source to yield from — any iterable of nodes.
    ## @param b    Reference set — any iterable of nodes.
    ## @param key  Function mapping a node to a hashable identity value.
    ## @return     Generator of nodes in both *a* and *b*.

    b_keys = {key(node) for node in b}
    for node in a:
        if key(node) in b_keys:
            yield node


def difference(
    a: Iterable[Node],
    b: Iterable[Node],
    *,
    key: Callable[[Node], Hashable] = _node_id,
) -> Iterable[Node]:
    ## @brief Yield nodes from *a* whose identity does not appear in *b*.
    ##
    ## *b* is fully consumed upfront to build a key set; *a* is consumed lazily.
    ##
    ## @param a    Source to yield from — any iterable of nodes.
    ## @param b    Nodes to exclude — any iterable of nodes.
    ## @param key  Function mapping a node to a hashable identity value.
    ## @return     Generator of nodes in *a* but not *b*.

    b_keys = {key(node) for node in b}
    for node in a:
        if key(node) not in b_keys:
            yield node
