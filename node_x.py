## @file node_x.py
##
## @brief Generic thread-safe dict-backed tree node library.
##
## Provides foundational building blocks for constructing object graphs
## with optional serialisation, streaming child discovery, and
## multi-node locking.  Designed for extractability as a standalone
## package — zero external dependencies beyond the Python standard library.
##
## Class hierarchy (dependency order):
##
##     Node                    — dict-backed, RLock, payload validation,
##                               freeze/thaw, subtree walking, merging
##     NodeList                — thread-safe Node-only collection
##     StreamMixin             — virtual stream() for lazy child discovery
##     GraphMixin              — per-class registry for true graph identity
##     Serialisable            — snapshot/restore/clone mixin
##     SerialisableNodeList    — NodeList + snapshot/restore
##     NodeTransaction         — ordered multi-node lock acquisition
##     ReadWriteMixin          — opt-in readers-writer lock for safe iteration
##
## Thread-safety contract
## ======================
##
## Protected without caller synchronisation
## -----------------------------------------
## All Node payload mutations — ``__setitem__``, ``__delitem__``,
## ``update``, ``pop``, ``clear``, ``popitem``, ``merge``, ``freeze``,
## ``thaw`` — acquire the per-instance ``RLock`` before modifying
## state.  The same applies to all ``NodeList`` mutations (``append``,
## ``extend``, ``insert``, ``pop``, ``remove``, ``clear``, ``reverse``,
## ``sort``, ``__setitem__``, ``__delitem__``, ``freeze``, ``thaw``) and
## to the ``Serialisable.to_plain()`` / ``snapshot()`` /
## ``to_pretty_json()`` family.  Subclasses inherit this protection
## automatically for any fields written via normal attribute or item
## assignment.
##
## Not protected — callers must synchronise explicitly
## ----------------------------------------------------
## **Reads** — ``node["key"]``, ``node.key``, ``node.get()``,
## ``node.items()``, and iterating a ``NodeList`` are not protected.
## In CPython the GIL makes isolated reads practically safe, but a
## read-modify-write sequence is not atomic::
##
##     # NOT safe under concurrent writes — use node.lock explicitly:
##     v = node["counter"]
##     node["counter"] = v + 1
##
## Subclasses that need safe iteration can opt into ``ReadWriteMixin``,
## which makes writers block transparently while a ``reading()`` context
## is active — no special handling required in writer code::
##
##     with node.reading():
##         for k, v in node.items():   # writers block until here exits
##             ...
##
## **Tree walks** — ``_tree_iter()`` and ``_walk_child_nodes()``
## acquire no locks.  Concurrent structural modifications (adding or
## removing children) can produce torn results.  See the ``@warning``
## on each method.
##
## **Cross-node operations** — reading from one node and writing to
## another is not atomic.  Use ``NodeTransaction`` to acquire all
## relevant locks in a stable, deadlock-free order::
##
##     with NodeTransaction(node_a, node_b):
##         val = node_a["x"]        # read
##         node_b["y"] = val        # write
##
## Read-modify-write atomicity
## ----------------------------
## Hold ``node.lock`` explicitly for any pattern of the form::
##
##     with node.lock:
##         v = node["counter"]      # read
##         node["counter"] = v + 1  # write
##
## ``NodeTransaction`` acquires locks in ``id()``-sorted order and
## therefore avoids ABBA deadlock between concurrent acquisitions on
## different node pairs.
##
## freeze() / thaw() / merge() deadlock safety
## ---------------------------------------------
## All three recursive operations use the same snapshot-then-release
## strategy: direct writes to ``self`` are performed under
## ``self._lock``; child nodes to recurse into are collected while the
## lock is held and then processed after it is released.  No two locks
## are ever held simultaneously, so ABBA deadlock cannot occur
## regardless of tree depth or concurrent access patterns.  These
## operations are safe to call from any thread without extra
## coordination.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/Munger/node-x
## @par Licence: MIT

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from importlib.metadata import PackageNotFoundError as _PackageNotFoundError
from importlib.metadata import version as _pkg_version
from typing import (
    Any,
    Callable,
    ClassVar,
    Dict,
    Generic,
    Iterable,
    Iterator,
    List,
    Optional,
    Tuple,
    Type,
    TypeVar,
)

# Read the version from the installed package metadata so that pyproject.toml
# remains the single source of truth.  The fallback fires when node_x.py is
# run directly from source without having been installed via pip — common
# during development and in CI before the package is built.
try:
    __version__: str = _pkg_version("node-x")
except _PackageNotFoundError:
    __version__ = "0.0.0.dev"

T = TypeVar("T", bound="Node")


# ============================================================================
# _RWLock  (internal)
# ============================================================================


class _RWLock:
    ## @brief Readers-writer lock used internally by ``ReadWriteMixin``.
    ##
    ## Multiple concurrent readers are allowed simultaneously.  A writer
    ## blocks until every active reader has exited.  The write side is
    ## re-entrant for the same thread so that methods which call other
    ## mutating methods internally (e.g. ``merge()`` calling
    ## ``__setitem__``) do not self-deadlock.

    def __init__(self) -> None:
        self._cond = threading.Condition(threading.Lock())
        self._readers: int = 0
        self._writing: bool = False
        self._write_thread: Optional[threading.Thread] = None
        self._write_depth: int = 0

    def acquire_read(self) -> None:
        with self._cond:
            while self._writing and self._write_thread is not threading.current_thread():
                self._cond.wait()
            self._readers += 1

    def release_read(self) -> None:
        with self._cond:
            self._readers -= 1
            if self._readers == 0:
                self._cond.notify_all()

    def acquire_write(self) -> None:
        current = threading.current_thread()
        with self._cond:
            if self._write_thread is current:
                self._write_depth += 1
                return
            while self._readers > 0 or self._writing:
                self._cond.wait()
            self._writing = True
            self._write_thread = current
            self._write_depth = 1

    def release_write(self) -> None:
        with self._cond:
            self._write_depth -= 1
            if self._write_depth == 0:
                self._writing = False
                self._write_thread = None
                self._cond.notify_all()


# ============================================================================
# Node
# ============================================================================


def _compute_reserved(cls: type) -> set[str]:
    """Compute reserved-name set for a Node/NodeList subclass.

    Walks the MRO collecting every public (non-underscore) name
    defined as a method, class attribute, or annotated field.
    Also reserves ``_frozen``, ``_reserved``, and ``_lock`` so they
    cannot be accidentally stored as payload keys.
    """
    reserved: set[str] = set()
    for base in cls.mro():
        for name in getattr(base, "__dict__", {}):
            if not name.startswith("_"):
                reserved.add(name)
        for name in getattr(base, "__annotations__", {}).keys():
            if not name.startswith("_"):
                reserved.add(name)
    reserved.update({"_frozen", "_reserved", "_lock"})
    return reserved


class Node(dict):
    ## @brief Thread-safe dict-backed tree node.
    ##
    ## All payload mutations (``__setitem__``, ``__delitem__``, ``update``,
    ## ``pop``, ``clear``, ``popitem``) are protected by a per-instance
    ## ``threading.RLock``.  Attribute writes to non-reserved names are
    ## routed into the dict payload under the same lock.
    ##
    ## ``__setattr__`` distinguishes two kinds of attribute:
    ##
    ##   * **Private/reserved** (name starts with ``_``, or in
    ##     ``type(self)._reserved``) — written via
    ##     ``object.__setattr__``, no lock, no freeze check.
    ##   * **Payload fields** — written into the dict under the lock
    ##     after checking the frozen flag.
    ##
    ## ``_reserved`` is populated automatically by ``__init_subclass__``
    ## from method names, properties, and annotated fields.  Subclasses
    ## that add public methods or properties do not need to maintain
    ## ``_reserved`` manually.
    ##
    ## ``_children`` is a tuple of attribute names whose values are
    ## Node or NodeList instances that form the structural tree.
    ## ``_tree_iter()`` walks these names to yield the full subtree.
    ## ``_walk_child_nodes()`` applies a callable to every descendant.
    ##
    ## Payload values are constrained by ``_validate_value()``:
    ##
    ##   * Allowed: ``Node``, ``NodeList``, ``None``, ``str``, ``int``,
    ##     ``float``, ``bool``, ``bytes``, and ``tuple`` (recursively
    ##     validated).
    ##   * Rejected: raw ``list`` (use ``NodeList``) and raw ``dict``
    ##     (wrap in a ``Node`` subclass).

    _reserved: ClassVar[set[str]] = set()
    ## @brief Attribute names treated as real object attributes rather
    ##        than payload keys.  Populated automatically.

    _children: ClassVar[Tuple[str, ...]] = ()
    ## @brief Names of payload fields that hold child Node/NodeList
    ##        instances.  Used by ``_tree_iter()`` and
    ##        ``_walk_child_nodes()``.  Immutable so that a subclass
    ##        which omits its own ``_children`` declaration cannot
    ##        accidentally mutate the base-class tuple.

    def __init_subclass__(cls, **kwargs: Any) -> None:
        ## @brief Collect reserved attribute names for each subclass.
        ##
        ## Walks the MRO and collects every public (non-underscore) name
        ## defined as a method, class attribute (except ``_children`` and
        ## ``_reserved`` themselves), or annotated field.  These are added
        ## to ``_reserved`` so that ``__setattr__`` routes them to
        ## ``object.__setattr__`` rather than the dict payload.
        ##
        ## Also ensures ``_frozen``, ``_reserved``, and ``_lock`` are
        ## always reserved so they cannot be set as payload keys.
        ##
        ## @param kwargs  Forwarded to ``super().__init_subclass__``.
        ## @return None

        super().__init_subclass__(**kwargs)
        cls._reserved = _compute_reserved(cls)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        ## @brief Construct a Node with optional initial payload.
        ##
        ## If a single positional dict argument is supplied its items are
        ## copied into the new node's payload via ``__setitem__`` (which
        ## validates values, checks frozen, and acquires the lock).
        ## All keyword arguments are then routed through ``__setattr__``.
        ##
        ## @param args    Optional single positional dict payload.
        ## @param kwargs  Optional keyword fields set via ``__setattr__``.
        ## @return None

        super().__init__()
        object.__setattr__(self, "_lock", threading.RLock())
        object.__setattr__(self, "_frozen", False)

        if args:
            if len(args) > 1:
                raise TypeError(
                    f"Node accepts at most 1 positional argument "
                    f"(a mapping), got {len(args)}."
                )
            initial = args[0]
            if isinstance(initial, dict):
                for k, v in initial.items():
                    self[k] = v
            else:
                raise TypeError(
                    f"Positional argument to Node must be a mapping "
                    f"(dict or Node), got {type(initial).__name__}."
                )

        for key, value in kwargs.items():
            setattr(self, key, value)

    def _check_frozen(self, context: str = "modify") -> None:
        ## @brief Raise if this node has been frozen.
        ## @param context  Description of the attempted operation, shown in the
        ##                 error message (e.g. ``"set key 'foo'"``).
        ## @raise TypeError  If ``_frozen`` is ``True``.
        ## @return None

        if self._frozen:
            raise TypeError(
                f"Cannot {context} on frozen {type(self).__name__}. "
                f"Call thaw() to restore mutability."
            )

    def _with_lock(self, func: Callable[[], Any]) -> Any:
        ## @brief Execute *func* under the instance RLock.
        ## @param func  Zero-argument callable.
        ## @return Whatever *func* returns.

        with self._lock:
            return func()

    @contextmanager
    def _write_guard(self) -> Iterator[None]:
        ## @brief Context manager acquired before every mutation.
        ##
        ## The base implementation is a no-op; ``ReadWriteMixin``
        ## overrides it to block writers while a ``reading()`` context
        ## is active.  All mutation methods acquire ``_write_guard``
        ## *before* ``_lock`` to preserve a consistent lock ordering and
        ## prevent deadlock.
        ##
        ## @return Context manager yielding ``None``.

        yield

    @property
    def lock(self) -> threading.RLock:
        ## @brief Expose the instance RLock for external callers.
        ##
        ## Used by ``NodeTransaction`` and any code that needs to hold
        ## locks across multiple nodes.
        ##
        ## @return The per-instance ``threading.RLock``.

        return self._lock

    def __getattr__(self, name: str) -> Any:
        ## @brief Fallback: route unknown attribute reads to dict keys.
        ##
        ## Allows ``node.foo`` to resolve as ``node["foo"]`` for schema
        ## data, while real object attributes (privates, methods,
        ## properties) continue to work normally.
        ##
        ## @param name  The attribute name to look up.
        ## @return The value of ``self[name]``.
        ## @raise AttributeError  If the key is not found in the payload.

        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)

    def __setattr__(self, name: str, value: Any) -> None:
        ## @brief Route payload attributes into the dict under lock.
        ##
        ## Names starting with underscore or in ``_reserved`` are written
        ## via ``object.__setattr__`` (direct instance attribute, no
        ## lock, no freeze check).  All other names are stored as
        ## payload keys through ``__setitem__`` after acquiring the lock
        ## and checking the frozen flag.
        ##
        ## ``_write_guard`` is acquired before ``_lock`` to maintain the
        ## consistent lock ordering required by ``ReadWriteMixin``.
        ##
        ## @param name   The attribute name.
        ## @param value  The value to store.
        ## @return None

        if name.startswith("_") or name in type(self)._reserved:
            object.__setattr__(self, name, value)
        else:
            with self._write_guard():
                with self._lock:
                    self._check_frozen(f"set attribute {name!r}")
                    self[name] = value

    def __setitem__(self, key: Any, value: Any) -> None:
        ## @brief Validate value, check frozen, then store under lock.
        ##
        ## @param key    The payload key.  Keys that are method or class
        ##               variable names (non-property ``_reserved`` entries)
        ##               are rejected; property-backed keys are allowed since
        ##               the property reads directly from the dict.
        ## @param value  The value to store (validated by
        ##               ``_validate_value``).
        ## @return None
        ## @raise KeyError  If *key* is a reserved non-property name.

        if key in type(self)._reserved and not isinstance(
            getattr(type(self), key, None), property
        ):
            raise KeyError(
                f"Key {key!r} is reserved on {type(self).__name__} -- it "
                f"conflicts with a method or property name. "
                f"Use a different key."
            )

        with self._write_guard():
            with self._lock:
                self._check_frozen(f"set key {key!r}")
                self._validate_value(value)
                super(Node, self).__setitem__(key, value)

    def __delitem__(self, key: Any) -> None:
        ## @brief Delete a payload key after checking frozen state.
        ## @param key  The key to remove.
        ## @return None

        with self._write_guard():
            with self._lock:
                self._check_frozen(f"delete key {key!r}")
                super(Node, self).__delitem__(key)

    def clear(self) -> None:
        ## @brief Remove all items from the payload under lock.
        ## @return None

        with self._write_guard():
            with self._lock:
                self._check_frozen("clear all items")
                super(Node, self).clear()

    def pop(self, key: Any, *args: Any) -> Any:
        ## @brief Remove and return a payload item under lock.
        ## @param key    The key to remove.
        ## @param args   Optional default if *key* is not found.
        ## @return The value for *key*, or the default if provided.

        with self._write_guard():
            with self._lock:
                self._check_frozen(f"pop key {key!r}")
                return super(Node, self).pop(key, *args)

    def popitem(self) -> Any:
        ## @brief Remove and return the last-inserted payload item.
        ## @return A ``(key, value)`` tuple.

        with self._write_guard():
            with self._lock:
                self._check_frozen("pop last item")
                return super(Node, self).popitem()

    def update(self, *args: Any, **kwargs: Any) -> None:
        ## @brief Merge keys into the payload, validating all keys and values.
        ##
        ## All reserved-name and value-type checks are performed before any
        ## state is mutated, so the update is all-or-nothing.
        ##
        ## @param args    Optional single mapping or iterable of pairs.
        ## @param kwargs  Additional key/value pairs.
        ## @return None

        with self._write_guard():
            with self._lock:
                self._check_frozen("update payload")
                if args:
                    if len(args) > 1:
                        raise TypeError(
                            f"Node.update() accepts at most 1 positional "
                            f"argument, got {len(args)}."
                        )
                    other = args[0]
                    pairs: list = (
                        list(other.items()) if hasattr(other, "items")
                        else list(other)
                    )
                else:
                    pairs = []

                # Validate all keys and values before writing anything.
                cls = type(self)
                for k, v in pairs:
                    if k in cls._reserved and not isinstance(
                        getattr(cls, k, None), property
                    ):
                        raise KeyError(
                            f"Key {k!r} is reserved on {cls.__name__} -- it "
                            f"conflicts with a method or property name. "
                            f"Use a different key."
                        )
                    self._validate_value(v)
                for k, v in kwargs.items():
                    if k in cls._reserved and not isinstance(
                        getattr(cls, k, None), property
                    ):
                        raise KeyError(
                            f"Key {k!r} is reserved on {cls.__name__} -- it "
                            f"conflicts with a method or property name. "
                            f"Use a different key."
                        )
                    self._validate_value(v)

                # All checks passed; write directly to the underlying dict.
                for k, v in pairs:
                    super(Node, self).__setitem__(k, v)
                for k, v in kwargs.items():
                    super(Node, self).__setitem__(k, v)

    def _validate_value(self, value: Any) -> None:
        ## @brief Ensure *value* is safe to store in a Node payload.
        ##
        ## Allowed types: Node, NodeList, None, str, int, float, bool,
        ## bytes, and tuple (recursively validated).
        ##
        ## Raw ``list`` and ``dict`` are rejected — use ``NodeList``
        ## or a ``Node`` subclass respectively.
        ##
        ## Scalars are checked first as they are the most common payload
        ## type.  Error cases (plain list, plain dict) are last.
        ##
        ## @param value  The value to check.
        ## @return None
        ## @raise TypeError  If *value* is a plain list or dict.

        if value is None or isinstance(value, (str, int, float, bool, bytes)):
            return
        if isinstance(value, (Node, NodeList)):
            return
        if isinstance(value, tuple):
            for item in value:
                self._validate_value(item)
            return
        if isinstance(value, list):
            raise TypeError(
                "Node cannot contain plain lists. Use NodeList instead."
            )
        if isinstance(value, dict):
            raise TypeError(
                "Node cannot contain raw dicts. Wrap in a Node subclass."
            )
        raise TypeError(
            f"Node cannot store {type(value).__name__}. "
            f"Allowed types: Node, NodeList, None, str, int, float, "
            f"bool, bytes, tuple."
        )

    def freeze(self, *, deep: bool = True) -> None:
        ## @brief Mark this node (and optionally its subtree) as frozen.
        ##
        ## When *deep* is ``True``, every descendant Node and NodeList is
        ## also frozen.  The implementation snapshots the immediate payload
        ## under ``self._lock``, then releases the lock before recursing
        ## into children.  Because ``self`` is frozen at snapshot time no
        ## new children can be written after the snapshot, so two locks are
        ## never held simultaneously and ABBA deadlock cannot occur.
        ##
        ## @param deep  Whether to recursively freeze children.
        ## @return None

        with self._lock:
            object.__setattr__(self, "_frozen", True)
            if not deep:
                return
            payload = list(self.values())

        def _freeze(value: Any) -> None:
            if isinstance(value, (Node, NodeList)):
                value.freeze(deep=True)
            elif isinstance(value, tuple):
                for item in value:
                    _freeze(item)

        for val in payload:
            _freeze(val)

    def thaw(self, *, deep: bool = True) -> None:
        ## @brief Restore mutability on this node (and optionally subtree).
        ##
        ## Uses the same snapshot-then-release pattern as ``freeze()``:
        ## the payload is snapshotted under ``self._lock``, the lock is
        ## released, then children are thawed without any parent lock held.
        ##
        ## @param deep  Whether to recursively thaw children.
        ## @return None

        with self._lock:
            object.__setattr__(self, "_frozen", False)
            if not deep:
                return
            payload = list(self.values())

        def _thaw(value: Any) -> None:
            if isinstance(value, (Node, NodeList)):
                value.thaw(deep=True)
            elif isinstance(value, tuple):
                for item in value:
                    _thaw(item)

        for val in payload:
            _thaw(val)

    def _walk_child_nodes(
        self,
        func: Callable[[Node], None],
        list_func: Optional[Callable[[NodeList], None]] = None,
    ) -> None:
        ## @brief Apply *func* to every descendant Node and *list_func*
        ##        to every descendant NodeList.
        ##
        ## Callers that need to propagate state onto NodeList containers
        ## (e.g. the frozen flag) should supply *list_func*.  Without it
        ## only Node instances are touched.
        ##
        ## The ``elif`` chain is deliberately ordered:
        ## ``Node → NodeList → list → dict``.  Two earlier bugs are
        ## fixed here:
        ##
        ##   1. ``list_func`` is an explicit parameter rather than
        ##      ``getattr(nodelist, func.__name__, None)``, which always
        ##      returned ``None`` for lambdas (``__name__ == '<lambda>'``).
        ##
        ##   2. ``isinstance(value, Node)`` is an ``elif`` (not ``if``)
        ##      so that Node — a dict subclass — does not also match
        ##      the ``elif isinstance(value, dict)`` branch and
        ##      re-traverse its subtree, which caused exponential
        ##      duplicate work on deep trees.
        ##
        ## @param func       Callable applied to each descendant ``Node``.
        ## @param list_func  Optional callable applied to each descendant
        ##                   ``NodeList``.
        ## @return None
        ## @warning Not thread-safe for the same reasons as
        ##          ``_tree_iter()``.  Confine calls to a single thread
        ##          or freeze the tree before invoking.

        def recurse(value: Any) -> None:
            if isinstance(value, Node):
                func(value)
            elif isinstance(value, NodeList):
                if list_func is not None:
                    list_func(value)
                for v in value:
                    recurse(v)
            elif isinstance(value, list):
                for v in value:
                    recurse(v)
            elif isinstance(value, dict):
                for v in value.values():
                    recurse(v)

        for val in self.values():
            recurse(val)

    def merge(
        self, other: Dict[str, Any] | Node
    ) -> Node:
        ## @brief Merge another mapping or Node into this one recursively.
        ##
        ## Node fields are merged recursively; scalars are overwritten.
        ## Uses the same snapshot-then-release strategy as ``freeze()``:
        ## scalar and NodeList writes are performed atomically under
        ## ``self._lock``, Node-into-Node sub-merges are collected while
        ## the lock is held and then executed after it is released, so
        ## no two locks are ever held simultaneously and ABBA deadlock
        ## cannot occur.
        ##
        ## @param other  The mapping to merge from.
        ## @return This node (for chaining).
        ## @raise TypeError  If *other* is not a mapping.

        try:
            incoming = list(other.items())
        except AttributeError as exc:
            raise TypeError(
                f"Node.merge() expected a mapping (dict or Node), "
                f"got {type(other).__name__}."
            ) from exc

        deferred: List[tuple[Node, Node]] = []

        with self._write_guard():
            with self._lock:
                self._check_frozen("merge")
                for key, value in incoming:
                    self._validate_value(value)
                    if key in self:
                        current = self[key]
                        if isinstance(current, Node) and isinstance(value, Node):
                            deferred.append((current, value))
                        else:
                            self[key] = value
                    else:
                        self[key] = value

        for current, value in deferred:
            current.merge(value)

        return self

    def _tree_iter(self) -> Iterable[Node]:
        ## @brief Depth-first generator over this node and its children.
        ##
        ## Walks all attributes named in ``_children`` recursively.
        ## Each node (including self) is yielded exactly once.
        ##
        ## @warning Not thread-safe.  No locks are acquired during
        ##          traversal.  Concurrent structural modifications
        ##          (reassigning ``_children`` tuples or the child
        ##          or reassigning child attributes) can cause nodes to
        ##          be skipped or visited twice.  Confine tree walks to
        ##          a single thread, or freeze the tree first.
        ##
        ## @yield ``Node`` instances in depth-first order.

        yield self
        for attr in type(self)._children:
            children = getattr(self, attr, None)
            if children is None:
                continue
            if isinstance(children, NodeList):
                for child in children:
                    yield from child._tree_iter()
            elif isinstance(children, Node):
                yield from children._tree_iter()


# Initialise reserved names for Node itself.
Node._reserved = _compute_reserved(Node)


# ============================================================================
# NodeList


class NodeList(list, Generic[T]):
    ## @brief Thread-safe list-like collection of ``Node`` elements.
    ##
    ## Every mutating operation acquires the instance RLock, checks
    ## the frozen flag, and validates that each element is a ``Node``
    ## instance before delegating to the standard ``list`` implementation.
    ##
    ## Like ``Node``, this class supports ``freeze``/``thaw`` with
    ## optional deep recursion into contained Node instances.

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        ## @brief Initialise an empty list with RLock and frozen state.
        ## @param args    Forwarded to ``list.__init__``.
        ## @param kwargs  Forwarded to ``list.__init__``.
        ## @return None

        object.__setattr__(self, "_lock", threading.RLock())
        object.__setattr__(self, "_frozen", False)
        super().__init__(*args, **kwargs)
        for item in self:
            if not isinstance(item, Node):
                raise TypeError(
                    f"{type(self).__name__} only accepts Node instances, "
                    f"got {type(item).__name__}. "
                    f"Wrap plain values in a Node subclass."
                )

    def _check_frozen(self, context: str = "modify") -> None:
        ## @brief Raise if this list has been frozen.
        ## @param context  Description of the attempted operation, shown in the
        ##                 error message (e.g. ``"append element"``).
        ## @raise TypeError  If ``_frozen`` is ``True``.

        if self._frozen:
            raise TypeError(
                f"Cannot {context} on frozen {type(self).__name__}. "
                f"Call thaw() to restore mutability."
            )

    def _with_lock(self, func: Callable[[], Any]) -> Any:
        ## @brief Execute *func* under the instance RLock.
        ## @param func  Zero-argument callable.
        ## @return Whatever *func* returns.

        with self._lock:
            return func()

    @contextmanager
    def _write_guard(self) -> Iterator[None]:
        ## @brief Context manager acquired before every mutation.
        ##
        ## The base implementation is a no-op; ``ReadWriteMixin``
        ## overrides it to block writers while a ``reading()`` context
        ## is active.  All mutation methods acquire ``_write_guard``
        ## *before* ``_lock`` to preserve a consistent lock ordering.
        ##
        ## @return Context manager yielding ``None``.

        yield

    @property
    def lock(self) -> threading.RLock:
        ## @brief Expose the instance RLock for external callers.
        ##
        ## Mirrors the same property on ``Node``.  Used by
        ## ``NodeTransaction`` and callers that need to hold the lock
        ## across multiple operations.
        ##
        ## @return The per-instance ``threading.RLock``.

        return self._lock

    def freeze(self, *, deep: bool = True) -> None:
        ## @brief Mark this list (and optionally its children) as frozen.
        ##
        ## Snapshots the list contents under ``self._lock``, releases the
        ## lock, then recurses into child Nodes — matching the deadlock-free
        ## pattern used by ``Node.freeze()``.
        ##
        ## @param deep  Whether to recursively freeze child Nodes.
        ## @return None

        with self._lock:
            object.__setattr__(self, "_frozen", True)
            if not deep:
                return
            items = list(self)

        for item in items:
            if isinstance(item, Node):
                item.freeze(deep=True)

    def thaw(self, *, deep: bool = True) -> None:
        ## @brief Restore mutability on this list (and optionally children).
        ##
        ## Uses the same snapshot-then-release pattern as ``freeze()``.
        ##
        ## @param deep  Whether to recursively thaw child Nodes.
        ## @return None

        with self._lock:
            object.__setattr__(self, "_frozen", False)
            if not deep:
                return
            items = list(self)

        for item in items:
            if isinstance(item, Node):
                item.thaw(deep=True)

    def append(self, __object: T) -> None:
        ## @brief Append a Node to this list under lock.
        ## @param __object  The Node instance to append.
        ## @return None
        ## @raise TypeError  If *__object* is not a ``Node``.

        with self._write_guard():
            with self._lock:
                self._check_frozen("append element")
                if not isinstance(__object, Node):
                    raise TypeError(
                        f"{type(self).__name__} only accepts Node instances, "
                        f"got {type(__object).__name__}. "
                        f"Wrap plain values in a Node subclass."
                    )
                super(NodeList, self).append(__object)

    def extend(self, __iterable: Iterable[T]) -> None:
        ## @brief Extend this list with Nodes from an iterable.
        ## @param __iterable  Iterable of Node instances.
        ## @return None

        with self._write_guard():
            with self._lock:
                self._check_frozen("extend list")
                items = list(__iterable)
                for item in items:
                    if not isinstance(item, Node):
                        raise TypeError(
                            f"{type(self).__name__} only accepts Node instances, "
                            f"got {type(item).__name__}. "
                            f"Wrap plain values in a Node subclass."
                        )
                super(NodeList, self).extend(items)

    def insert(self, __index: int, __object: T) -> None:
        ## @brief Insert a Node at a given index under lock.
        ## @param __index   The position to insert at.
        ## @param __object  The Node instance to insert.
        ## @return None

        with self._write_guard():
            with self._lock:
                self._check_frozen("insert element")
                if not isinstance(__object, Node):
                    raise TypeError(
                        f"{type(self).__name__} only accepts Node instances, "
                        f"got {type(__object).__name__}. "
                        f"Wrap plain values in a Node subclass."
                    )
                super(NodeList, self).insert(__index, __object)

    def pop(self, __index: int = -1) -> T:
        ## @brief Remove and return the Node at *__index* under lock.
        ## @param __index  The index to pop (default -1, last element).
        ## @return The removed Node instance.

        with self._write_guard():
            with self._lock:
                self._check_frozen("pop element")
                return super(NodeList, self).pop(__index)

    def remove(self, __value: T) -> None:
        ## @brief Remove the first occurrence of *__value* under lock.
        ## @param __value  The Node instance to remove.
        ## @return None

        with self._write_guard():
            with self._lock:
                self._check_frozen("remove element")
                super(NodeList, self).remove(__value)

    def clear(self) -> None:
        ## @brief Remove all elements under lock.
        ## @return None

        with self._write_guard():
            with self._lock:
                self._check_frozen("clear all elements")
                super(NodeList, self).clear()

    def reverse(self) -> None:
        ## @brief Reverse this list in place under lock.
        ## @return None

        with self._write_guard():
            with self._lock:
                self._check_frozen("reverse list")
                super(NodeList, self).reverse()

    def sort(self, **kwargs: Any) -> None:
        ## @brief Sort this list in place under lock.
        ## @param kwargs  Keyword arguments forwarded to ``list.sort()``.
        ## @return None

        with self._write_guard():
            with self._lock:
                self._check_frozen("sort list")
                super(NodeList, self).sort(**kwargs)

    def __setitem__(
        self, __key: int | slice, __value: T | Iterable[T]
    ) -> None:
        ## @brief Set item at index or slice with type validation.
        ## @param __key    Index or slice.
        ## @param __value  Node instance (or iterable for slices).
        ## @return None

        with self._write_guard():
            with self._lock:
                self._check_frozen(f"set index {__key!r}")
                if isinstance(__key, slice):
                    items = list(__value)  # type: ignore[arg-type]
                    for item in items:
                        if not isinstance(item, Node):
                            raise TypeError(
                                f"{type(self).__name__} only accepts "
                                f"Node instances, got {type(item).__name__}. "
                                f"Wrap plain values in a Node subclass."
                            )
                    super(NodeList, self).__setitem__(__key, items)
                else:
                    if not isinstance(__value, Node):
                        raise TypeError(
                            f"{type(self).__name__} only accepts "
                            f"Node instances, got {type(__value).__name__}. "
                            f"Wrap plain values in a Node subclass."
                        )
                    super(NodeList, self).__setitem__(__key, __value)

    def __delitem__(self, __key: int | slice) -> None:
        ## @brief Delete element at index or slice under lock.
        ## @param __key  Index or slice.
        ## @return None

        with self._write_guard():
            with self._lock:
                self._check_frozen(f"delete index {__key!r}")
                super(NodeList, self).__delitem__(__key)


# ============================================================================
# StreamMixin
# ============================================================================


class StreamMixin:
    ## @brief Mixin that adds lazy child-discovery to a ``Node``.
    ##
    ## Callers walk the static tree skeleton via ``_tree_iter()`` and
    ## call ``stream()`` on each node to discover children whose
    ## existence is not known until the node's content is examined.
    ## This allows the tree to be populated incrementally rather than
    ## all at once.
    ##
    ## The base implementation is a no-op.  Subclasses override to
    ## yield child ``Node`` instances on demand.

    def stream(self, data: Optional[bytes] = None) -> Iterable[Node]:
        ## @brief Yield dynamically-discovered child nodes.
        ##
        ## When *data* is supplied the implementation may parse those
        ## bytes to produce children.  When *data* is ``None`` the
        ## implementation is responsible for obtaining its own source
        ## (e.g. reading a file, querying a resource).
        ##
        ## The default implementation yields nothing.  Override this
        ## method to parse the node's content and yield children as
        ## they are discovered.
        ##
        ## @param data  Optional bytes for the implementation to parse.
        ##              ``None`` when no in-memory payload is provided.
        ## @yield Child ``Node`` instances discovered from content.

        yield from ()


# ============================================================================
# GraphMixin
# ============================================================================


class GraphMixin:
    ## @brief Mixin that gives a ``Node`` subclass a stable identity and a
    ##        class-level registry, enabling true graph semantics.
    ##
    ## In a pure tree each ``stream()`` call constructs a fresh node instance.
    ## When the same logical entity is reachable from multiple paths this leads
    ## to duplicate objects, duplicate work, and inconsistent state.
    ## ``GraphMixin`` resolves that: ``get_or_create()`` returns the *same*
    ## instance for a given key regardless of which traversal path discovered
    ## it, so mutations are immediately visible everywhere.
    ##
    ## Registry scope
    ## --------------
    ## Each concrete subclass gets its own registry dict, created lazily the
    ## first time ``get_or_create()`` is called on that class.  The explicit
    ## ``"_registry" not in cls.__dict__`` check prevents subclasses from
    ## inheriting — and accidentally sharing — a parent class's registry.
    ##
    ## Usage::
    ##
    ##     class DocumentNode(GraphMixin, StreamMixin, Node):
    ##         ...
    ##
    ##     a = DocumentNode.get_or_create("doc-42")
    ##     b = DocumentNode.get_or_create("doc-42")
    ##     assert a is b   # True — same instance
    ##
    ##     DocumentNode.clear_registry()   # reset between independent runs

    @classmethod
    def get_or_create(cls, key: str, defaults: Optional[Dict[str, Any]] = None) -> "Node":
        ## @brief Return the canonical node for *key*, creating it if absent.
        ##
        ## The registry lives directly on the concrete class so each subclass
        ## has an independent namespace.
        ##
        ## Keys must be strings because they are stored in the node payload as
        ## ``_key`` and are used as the values of ``$ref`` markers during
        ## serialisation.  Non-string keys (integers, tuples, etc.) would
        ## silently corrupt JSON round-trips: JSON converts tuple keys to lists,
        ## and YAML emits Python-specific tags that ``yaml.safe_load`` rejects.
        ##
        ## @param key       Unique identity string for this node.
        ## @param defaults  Initial payload dict used *only* when creating a
        ##                  new instance.  Ignored on cache hits.
        ## @return The existing or newly-created node instance.
        ## @raise TypeError  If *key* is not a ``str``.

        if not isinstance(key, str):
            raise TypeError(
                f"{cls.__name__}.get_or_create() requires a str key; "
                f"got {type(key).__name__} {key!r}. "
                f"Keys are stored as _key in the node payload and used as "
                f"$ref targets during serialisation — they must be strings "
                f"so that JSON and YAML round-trips work correctly."
            )

        if "_registry" not in cls.__dict__:
            cls._registry: Dict[str, Any] = {}

        if key not in cls._registry:
            node = cls(defaults or {})
            node["_key"] = key
            cls._registry[key] = node

        return cls._registry[key]

    @classmethod
    def clear_registry(cls) -> None:
        ## @brief Remove all entries from this class's registry.
        ##
        ## Call between independent runs to prevent stale instances from a
        ## previous run being returned by ``get_or_create()``.
        ##
        ## @return None

        if "_registry" in cls.__dict__:
            cls._registry.clear()

    @property
    def graph_key(self) -> Optional[str]:
        ## @brief The identity key this node was registered under, or ``None``
        ##        if it was not created via ``get_or_create()``.
        ##
        ## @return The key string, or ``None``.

        return self.get("_key")  # type: ignore[attr-defined]

    @property
    def is_known(self) -> bool:
        ## @brief ``True`` if this node has been marked as visited.
        ##
        ## Set via ``mark_known()``.  Callers can check this flag before
        ## calling ``stream()`` to distinguish a first visit from a
        ## cross-reference to an already-expanded node.
        ##
        ## @return Boolean.

        return bool(self.get("_known"))  # type: ignore[attr-defined]

    def mark_known(self) -> None:
        ## @brief Mark this node as visited.
        ##
        ## Call once per node, typically at the start of ``stream()`` after
        ## the cache check and before any I/O, so that concurrent arrivals
        ## at the same node observe the flag immediately.
        ##
        ## @return None

        self["_known"] = True  # type: ignore[index]


# ============================================================================
# Serialisable
# ============================================================================


class Serialisable:
    ## @brief Mixin that adds snapshot/restore/clone to a ``Node``.
    ##
    ## Snapshots are plain Python dict/list/scalar trees with no
    ## ``Node`` instances — they can be serialised to JSON, YAML,
    ## msgpack, or any text/binary format and later restored into a
    ## typed object graph.
    ##
    ## Subclasses opt into structured child restoration by setting
    ## ``_node_fields`` and/or ``_list_fields``.  The default
    ## ``restore()`` passes the snapshot dict as ``__init__`` kwargs
    ## which works for simple payload-only nodes.

    _restore_via_payload: ClassVar[bool] = False
    ## @brief If ``True``, ``restore()`` bypasses ``__init__`` and
    ##        constructs via ``_from_payload()``.

    _node_fields: ClassVar[Dict[str, Type[Any]]] = {}
    ## @brief Mapping of field name → Node subclass for restoring
    ##        single-child attributes.

    _list_fields: ClassVar[Dict[str, Tuple[Type[Any], Type[Any]]]] = {}
    ## @brief Mapping of field name → (NodeList subclass, item Node
    ##        subclass) for restoring list-child attributes.

    def to_plain(self, _memo: Optional[Dict[str, bool]] = None) -> Any:
        ## @brief Recursively convert this node tree to plain Python
        ##        structures (no ``Node`` instances).
        ##
        ## Nested Nodes become dicts, lists remain lists, scalars pass
        ## through unmodified.
        ##
        ## GraphMixin nodes carry a ``_key`` that makes them addressable
        ## across multiple paths in a graph.  When such a node is
        ## encountered for the first time it is serialised in full; on
        ## every subsequent encounter a ``{"$ref": key}`` marker is emitted
        ## instead.  ``restore()`` resolves these markers back to live Node
        ## objects using a shared registry built during deserialisation.
        ## Without this mechanism, nodes reachable via more than one path
        ## would be duplicated in the output and restored as independent
        ## objects, silently breaking GraphMixin identity guarantees.
        ##
        ## @param _memo  Internal reference-tracking dict; callers should
        ##               omit this argument — it is initialised
        ##               automatically and shared across the entire
        ##               recursive walk so that all cross-references within
        ##               a single ``to_plain()`` call are detected.
        ## @return A plain dict/list/scalar tree suitable for JSON.

        if _memo is None:
            _memo = {}

        def convert(value: Any) -> Any:
            if isinstance(value, Node):
                # GraphMixin nodes carry "_key"; use it to detect shared
                # references.  The key is recorded *before* recursing so
                # that self-referential structures terminate rather than
                # looping indefinitely.
                node_key = value.get("_key")
                if node_key is not None:
                    if node_key in _memo:
                        return {"$ref": node_key}
                    _memo[node_key] = True
                with value._lock:
                    return {k: convert(v) for k, v in value.items()}
            if isinstance(value, list):
                return [convert(v) for v in value]
            if isinstance(value, dict):
                return {k: convert(v) for k, v in value.items()}
            return value

        return convert(self)

    def to_pretty_json(self, indent: int = 2) -> str:
        ## @brief Return an indented JSON string of this node tree.
        ## @param indent  Number of spaces per indent level (default 2).
        ## @return Pretty-printed JSON string.

        return json.dumps(self.to_plain(), indent=indent)

    def snapshot(self) -> Any:
        ## @brief Return a JSON-serialisable snapshot of this node tree.
        ##
        ## Scalar fields are emitted first in insertion order, followed
        ## by nested structures (dicts, lists).  This keeps snapshots
        ## readable without requiring each subclass to define ordering.
        ##
        ## @return Plain dict with scalars before nested structures.

        plain = self.to_plain()
        if not isinstance(plain, dict):
            return plain

        scalars: Dict[str, Any] = {}
        nested: Dict[str, Any] = {}

        for key, value in plain.items():
            if isinstance(value, (dict, list)):
                nested[key] = value
            else:
                scalars[key] = value

        return {**scalars, **nested}

    @classmethod
    def restore(
        cls,
        snapshot: Any,
        _registry: Optional[Dict[str, Any]] = None,
    ) -> Any:
        ## @brief Rebuild a node (or subclass) from a snapshot.
        ##
        ## If ``_restore_via_payload`` is ``True``, uses
        ## ``_from_payload()`` to bypass ``__init__`` side effects.
        ## Otherwise passes the snapshot dict as ``__init__``'s
        ## single positional argument.
        ##
        ## ``$ref`` markers produced by ``to_plain()`` are resolved using
        ## *_registry*, a shared ``key → node`` dict that is populated
        ## incrementally as nodes are restored.  Because ``to_plain()``
        ## always emits the full serialisation of a node before any
        ## ``$ref`` pointing to it (depth-first order), the registry
        ## always contains the target by the time the reference is
        ## encountered — no forward-reference resolution is required.
        ##
        ## Callers should omit *_registry*; it is created automatically
        ## on the first call and threaded through all recursive calls by
        ## ``_restore_children()`` and ``SerialisableNodeList.restore()``.
        ##
        ## @param snapshot   Plain dict from an earlier ``snapshot()``.
        ## @param _registry  Internal key→node registry; callers omit.
        ## @return A reconstructed ``Node`` subclass instance.
        ## @raise TypeError  If *snapshot* is not a dict.
        ## @raise KeyError   If a ``$ref`` key is absent from the registry.

        if _registry is None:
            _registry = {}

        if isinstance(snapshot, dict):
            # A $ref entry means this node was already restored earlier
            # in the same pass; return it directly rather than creating
            # a duplicate.
            ref = snapshot.get("$ref")
            if ref is not None:
                node = _registry.get(ref)
                if node is None:
                    raise KeyError(
                        f"$ref {ref!r} cannot be resolved — the referenced "
                        f"node has not been restored yet.  Ensure the "
                        f"snapshot was produced by to_plain() and that "
                        f"restore() is called on the root node so the full "
                        f"tree is traversed before any $ref is encountered."
                    )
                return node

            if getattr(cls, "_restore_via_payload", False):
                node = cls._from_payload(snapshot)
            else:
                try:
                    node = cls(snapshot)
                except TypeError as exc:
                    raise TypeError(
                        f"{cls.__name__}.restore() failed -- __init__ raised "
                        f"{type(exc).__name__}({exc}). "
                        f"Set _restore_via_payload = True if the snapshot "
                        f"already contains all fields, or provide a custom "
                        f"restore() implementation."
                    ) from exc

            # Register keyed nodes *before* recursing into children so
            # that any $ref pointing back to this node from a descendant
            # (e.g. a release referencing its own week via chart_weeks)
            # resolves correctly.
            key = snapshot.get("_key")
            if key is not None:
                _registry[key] = node

            # Always attempt child restoration; a no-op when _list_fields
            # and _node_fields are both empty (the common case).
            cls._restore_children(node, snapshot, _registry=_registry)

            return node

        raise TypeError(
            f"{cls.__name__}.restore() expected a mapping (dict), "
            f"got {type(snapshot).__name__}. Snapshots are produced "
            f"by calling snapshot() on a Node instance."
        )

    @classmethod
    def _from_payload(cls, payload: Dict[str, Any]) -> Any:
        ## @brief Construct a node from a plain payload dict without
        ##        invoking ``__init__``.
        ##
        ## Creates the instance via ``__new__`` then initialises the
        ## dict portion via ``dict.__init__``, bypassing validation
        ## and side effects.  Safe because *payload* already came from
        ## a ``snapshot()`` which has been validated.
        ##
        ## @param payload  Plain dict of field values.
        ## @return A new instance of ``cls``.

        instance = cls.__new__(cls)
        dict.__init__(instance, payload)
        object.__setattr__(instance, "_lock", threading.RLock())
        object.__setattr__(instance, "_frozen", False)
        if issubclass(cls, ReadWriteMixin):
            object.__setattr__(instance, "_rw_lock", _RWLock())
        return instance

    @classmethod
    def _restore_children(
        cls,
        node: Any,
        snapshot: Any,
        _registry: Optional[Dict[str, Any]] = None,
    ) -> None:
        ## @brief Rebuild declared child Nodes/NodeLists from a snapshot.
        ##
        ## Subclasses opt in by populating ``_node_fields`` and/or
        ## ``_list_fields``, then call this from their own ``restore()``
        ## after constructing *node*.
        ##
        ## *_registry* is threaded through every recursive ``restore()``
        ## and ``SerialisableNodeList.restore()`` call so that ``$ref``
        ## markers produced by ``to_plain()`` can be resolved against the
        ## partially-built tree.  Callers should pass the same registry
        ## dict that was provided to the outer ``restore()`` call.
        ##
        ## @param node       The parent node whose children to restore.
        ## @param snapshot   Plain dict snapshot containing child data.
        ## @param _registry  Shared key→node registry; pass from restore().
        ## @return None

        if not isinstance(snapshot, dict):
            return

        # Initialise to an empty registry if the caller forgot — harmless
        # but means $ref resolution won't cross the child-restore boundary.
        if _registry is None:
            _registry = {}

        for field, child_cls in getattr(cls, "_node_fields", {}).items():
            raw = snapshot.get(field)
            if raw is not None:
                setattr(node, field, child_cls.restore(raw, _registry=_registry))

        for field, (list_cls, item_cls) in getattr(
            cls, "_list_fields", {}
        ).items():
            items = snapshot.get(field, [])
            setattr(
                node,
                field,
                list_cls.restore(items, item_type=item_cls, _registry=_registry),
            )

    def clone(self) -> Any:
        ## @brief Deep-clone this node and its subtree.
        ##
        ## Preserves runtime attributes (e.g. loaders, caches) and
        ## avoids calling ``__init__`` on subclasses so that custom
        ## constructors remain valid on the clone.
        ##
        ## @return A deep copy of this node.

        return self._clone_recursive({})

    def _clone_recursive(self, memo: Dict[int, Any]) -> Any:
        ## @brief Internal deep-clone with identity-based memoisation.
        ##
        ## Uses ``object.__new__`` and ``dict.__init__`` to bypass the
        ## subclass ``__init__`` (which may have side effects) while
        ## preserving the subclass type.  Shared references (same Node
        ## reachable through multiple paths) are preserved via *memo*.
        ##
        ## @param memo  ``id(original) → clone`` dict for cycle and
        ##              shared-reference detection.
        ## @return A deep copy of this node.

        if not isinstance(self, dict):
            raise TypeError(
                f"Node subclass {type(self).__name__} is not dict-backed -- "
                f"clone() requires dict in the MRO. "
                f"Do not remove dict from the class hierarchy."
            )

        obj_id = id(self)
        if obj_id in memo:
            return memo[obj_id]

        new = type(self).__new__(type(self))
        memo[obj_id] = new
        dict.__init__(new, {})

        def clone_value(value: Any) -> Any:
            clone_func = getattr(value, "_clone_recursive", None)
            if clone_func:
                return clone_func(memo)
            if isinstance(value, NodeList):
                nl_clone = type(value).__new__(type(value))
                for attr, val in getattr(value, "__dict__", {}).items():
                    if attr == "_lock":
                        object.__setattr__(nl_clone, "_lock", threading.RLock())
                    elif attr == "_rw_lock":
                        object.__setattr__(nl_clone, "_rw_lock", _RWLock())
                    else:
                        object.__setattr__(nl_clone, attr, val)
                list.__init__(nl_clone, [clone_value(v) for v in value])
                return nl_clone
            if isinstance(value, list):
                return [clone_value(v) for v in value]
            if isinstance(value, dict):
                return {k: clone_value(v) for k, v in value.items()}
            return value

        for k, v in self.items():
            dict.__setitem__(new, k, clone_value(v))

        for attr, val in self.__dict__.items():
            if attr == "_lock":
                object.__setattr__(new, "_lock", threading.RLock())
            elif attr == "_rw_lock":
                object.__setattr__(new, "_rw_lock", _RWLock())
            else:
                object.__setattr__(new, attr, val)

        return new


# ============================================================================
# SerialisableNodeList
# ============================================================================


class SerialisableNodeList(NodeList[T], Generic[T]):
    ## @brief A ``NodeList`` with snapshot/restore and pretty-printing.
    ##
    ## Provides the same serialisation interface as ``Serialisable``
    ## but for list-shaped collections of ``Node`` instances.

    def snapshot(self) -> Any:
        ## @return A list of plain dicts/scalars from each element's
        ##         ``snapshot()`` method, falling back to identity for
        ##         non-Node items.

        return [
            getattr(item, "snapshot", lambda: item)() for item in self
        ]

    @classmethod
    def restore(
        cls,
        snapshots: Iterable[Any],
        item_type: type[T],
        _registry: Optional[Dict[str, Any]] = None,
    ) -> SerialisableNodeList[T]:
        ## @brief Rebuild a ``SerialisableNodeList`` from a list of
        ##        element snapshots.
        ##
        ## Each snapshot is restored via ``item_type.restore()``
        ## (if available) or by passing it to ``item_type()`` directly.
        ## Already-instantiated ``item_type`` instances pass through.
        ##
        ## *_registry* is forwarded to each ``item_type.restore()`` call
        ## so that ``$ref`` markers within list items are resolved against
        ## the same shared key→node table used by the parent restore pass.
        ##
        ## @param snapshots  Iterable of snapshot dicts/scalars.
        ## @param item_type  The ``Node`` subclass to restore each element as.
        ## @param _registry  Shared key→node registry; pass from the caller.
        ## @return A reconstructed ``SerialisableNodeList``.

        if _registry is None:
            _registry = {}

        lst: SerialisableNodeList[T] = cls()
        restore_func = getattr(item_type, "restore", None)
        for snap in snapshots:
            if isinstance(snap, item_type):
                lst.append(snap)
            elif restore_func:
                # Forward _registry so cross-list $refs resolve correctly.
                lst.append(restore_func(snap, _registry=_registry))
            else:
                lst.append(item_type(snap))
        return lst

    def to_pretty_json(self, indent: int = 2) -> str:
        ## @brief Return an indented JSON string of this list.
        ## @param indent  Number of spaces per indent level (default 2).
        ## @return Pretty-printed JSON string.

        payload = [
            getattr(item, "to_plain", lambda: item)() for item in self
        ]
        return json.dumps(payload, indent=indent)


# ============================================================================
# NodeTransaction
# ============================================================================


class NodeTransaction:
    ## @brief Ordered multi-node lock acquisition.
    ##
    ## Acquires one or more ``Node`` or ``NodeList`` locks in a stable
    ## order (sorted by ``id()``) to prevent deadlock when performing
    ## cross-node operations.  Any object that exposes a ``lock``
    ## property returning a ``threading.RLock`` is accepted.
    ## Locks are released in reverse order.
    ##
    ## ``ReadWriteMixin`` nodes are fully supported: ``_write_guard``
    ## is entered before ``_lock`` for each node (matching the documented
    ## lock ordering), so concurrent ``reading()`` contexts are blocked
    ## for the duration of the transaction.
    ##
    ## Usage::
    ##
    ##     with NodeTransaction(node_a, node_b, node_c):
    ##         # safe: locks held in id order
    ##         node_a["ref"] = node_b["id"]
    ##
    ##     with NodeTransaction(parent_node, some_nodelist):
    ##         for item in some_nodelist:   # safe: lock held
    ##             ...

    def __init__(self, *nodes: Any) -> None:
        ## @brief Accept one or more lockable instances to manage.
        ##
        ## Nodes are sorted by ``id()`` for deterministic ordering.
        ## Any object with a ``.lock`` property is accepted
        ## (``Node``, ``NodeList``, or ``ReadWriteMixin`` subclasses).
        ##
        ## @param nodes  One or more lockable instances.

        seen: set[int] = set()
        unique: list[Any] = []
        for n in nodes:
            nid = id(n)
            if nid not in seen:
                seen.add(nid)
                unique.append(n)
        self._nodes = sorted(unique, key=id)
        self._guards: List[Any] = []

    def __enter__(self) -> NodeTransaction:
        ## @brief Acquire all write guards and locks in sorted order.
        ##
        ## For each node, ``_write_guard`` is entered before ``_lock``
        ## to maintain the correct lock ordering.  For plain ``Node``
        ## and ``NodeList`` instances ``_write_guard`` is a no-op.
        ## For ``ReadWriteMixin`` nodes it acquires the write side of
        ## the readers-writer lock, blocking any concurrent readers.
        ##
        ## If any acquisition raises, all previously-acquired guards
        ## and locks are released before the exception propagates.
        ##
        ## @return This transaction instance.

        acquired_guards: List[Any] = []
        acquired_locks: List[Any] = []
        try:
            for n in self._nodes:
                guard = n._write_guard()
                guard.__enter__()
                acquired_guards.append(guard)
            for n in self._nodes:
                n.lock.acquire()
                acquired_locks.append(n)
        except BaseException:
            for n in reversed(acquired_locks):
                n.lock.release()
            for guard in reversed(acquired_guards):
                guard.__exit__(None, None, None)
            raise
        self._guards = acquired_guards
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        ## @brief Release all locks then write guards in reverse order.
        ## @param exc_type  Exception type (unused).
        ## @param exc       Exception value (unused).
        ## @param tb        Traceback (unused).
        ## @return ``False`` — exceptions are not suppressed.

        for n in reversed(self._nodes):
            n.lock.release()
        for guard in reversed(self._guards):
            guard.__exit__(exc_type, exc, tb)
        return False


# ============================================================================
# ReadWriteMixin
# ============================================================================


class ReadWriteMixin:
    ## @brief Opt-in mixin that adds readers-writer lock semantics to a
    ##        ``Node`` or ``NodeList``.
    ##
    ## By default, ``Node`` and ``NodeList`` protect mutations but not
    ## reads.  This mixin adds a ``reading()`` context manager that
    ## callers wrap iteration in.  Any mutation that arrives while one
    ## or more readers are active blocks transparently until every
    ## reader has exited — no special handling is required in writer
    ## code.
    ##
    ## Usage::
    ##
    ##     class LiveList(ReadWriteMixin, NodeList[MyNode]):
    ##         pass
    ##
    ##     lst = LiveList()
    ##
    ##     # reader (e.g. coordinator iterating):
    ##     with lst.reading():
    ##         for item in lst:          # writers block until here exits
    ##             process(item)
    ##
    ##     # writer (e.g. worker appending) — no changes needed:
    ##     lst.append(new_node)          # blocks if a reader is active
    ##
    ## The mixin is compatible with both ``Node`` and ``NodeList``.
    ## Multiple concurrent readers are allowed; writers are serialised
    ## and block until all readers finish.  The write lock is re-entrant
    ## for the same thread so that methods which call other mutating
    ## methods internally (e.g. ``merge()`` calling ``__setitem__``)
    ## do not self-deadlock.
    ##
    ## Lock ordering: ``_write_guard`` (readers-writer layer) is always
    ## acquired *before* the per-instance ``_lock`` (RLock layer).
    ## ``reading()`` does not acquire ``_lock``, so there is no
    ## circular dependency between the two layers.

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        ## @brief Initialise the readers-writer lock alongside the base
        ##        ``Node`` or ``NodeList`` state.
        ## @param args    Forwarded to the base class ``__init__``.
        ## @param kwargs  Forwarded to the base class ``__init__``.
        ## @return None

        object.__setattr__(self, "_rw_lock", _RWLock())
        super().__init__(*args, **kwargs)

    @contextmanager
    def reading(self) -> Iterator[None]:
        ## @brief Context manager that marks this object as being read.
        ##
        ## Mutations (``__setitem__``, ``append``, etc.) block until
        ## every active ``reading()`` context has exited.  Multiple
        ## concurrent readers are allowed simultaneously.
        ##
        ## Usage::
        ##
        ##     with node.reading():
        ##         for k, v in node.items():   # safe
        ##             ...
        ##
        ##     with nodelist.reading():
        ##         for item in nodelist:       # safe
        ##             ...
        ##
        ## @yield Control to the caller while the read lock is held.

        rw: _RWLock = object.__getattribute__(self, "_rw_lock")
        rw.acquire_read()
        try:
            yield
        finally:
            rw.release_read()

    @contextmanager
    def _write_guard(self) -> Iterator[None]:
        ## @brief Override of the base no-op: acquires the write lock.
        ##
        ## Blocks until all active ``reading()`` contexts have exited,
        ## then yields.  Re-entrant: the same thread may acquire the
        ## write lock multiple times without deadlocking.
        ##
        ## @return Context manager yielding ``None``.

        rw: _RWLock = object.__getattribute__(self, "_rw_lock")
        rw.acquire_write()
        try:
            yield
        finally:
            rw.release_write()


# ============================================================================
# Public API
# ============================================================================

__all__ = [
    "Node",
    "NodeList",
    "StreamMixin",
    "GraphMixin",
    "Serialisable",
    "SerialisableNodeList",
    "NodeTransaction",
    "ReadWriteMixin",
]
