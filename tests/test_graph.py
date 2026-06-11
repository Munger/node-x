## @file test_graph.py
##
## @brief Unit tests for ``GraphMixin``.
##
## Sections:
##
##   - **get_or_create** — identity semantics, ``_key`` injection, defaults
##     applied only on first call, and behaviour when no defaults are given.
##   - **clear_registry** — cleared registries produce fresh instances; calling
##     on a class that has never registered a node is a no-op.
##   - **registry isolation** — each concrete subclass owns an independent
##     registry; a parent class registry must never be shared with a subclass.
##   - **graph_key / is_known / mark_known** — read-only graph state properties
##     work on nodes created via ``get_or_create()`` and on plain nodes that
##     were not.
##   - **to_plain: $ref emission** — a keyed node referenced from two places in
##     the same tree emits a full dict on first encounter and ``{"$ref": key}``
##     on every subsequent encounter; unkeyed nodes are never deduplicated.
##   - **restore: $ref resolution** — a snapshot containing ``$ref`` markers
##     round-trips correctly, restoring a true graph where both references in
##     the tree point to the *same* Python object.
##   - **restore: $ref error** — restoring a snapshot whose ``$ref`` key is
##     absent from the registry raises a ``KeyError`` with the missing key in
##     the message.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

_PKG_DIR  = str(Path(__file__).resolve().parent)
_ROOT_DIR = str(Path(__file__).resolve().parent.parent)
for _d in (_PKG_DIR, _ROOT_DIR):
    if _d not in sys.path:
        sys.path.insert(0, _d)

from node_x import (
    GraphMixin,
    Node,
    Serialisable,
    SerialisableNodeList,
)

from _helpers import (
    check,
    catch_into,
    does_not_raise,
    heading,
)


def run() -> Tuple[int, int]:
    ## @brief Execute all GraphMixin test sections and return pass/fail counts.
    ##
    ## Local class definitions are used inside each section rather than
    ## module-level fixtures so that each section starts with a clean,
    ## empty registry.  This prevents registry state from one section
    ## polluting assertions in another.
    ##
    ## @return ``(pass_count, fail_count)`` across all sections.

    passed: List[str] = []
    failed: List[str] = []

    # ------------------------------------------------------------------
    heading("GraphMixin: get_or_create")
    # ------------------------------------------------------------------
    # get_or_create is the core graph primitive: it guarantees that the
    # same key always yields the same Python object, regardless of how many
    # times or from how many call sites it is invoked.

    class GNode(GraphMixin, Node):
        pass

    GNode.clear_registry()

    a = GNode.get_or_create("k1", {"label": "first"})
    b = GNode.get_or_create("k1")

    check(passed, failed, a is b,
          "get_or_create returns same instance for same key")
    check(passed, failed, a["_key"] == "k1",
          "get_or_create stores _key in node payload")
    check(passed, failed, a["label"] == "first",
          "get_or_create applies defaults on first call")

    # Defaults supplied to a cache-hit call must be silently ignored so
    # that a second discovery of the same entity cannot mutate shared state.
    b_with_defaults = GNode.get_or_create("k1", {"label": "overwrite-attempt"})
    check(passed, failed, b_with_defaults["label"] == "first",
          "get_or_create ignores defaults on cache hit")

    # Creating with no defaults produces a node that only has _key set.
    c = GNode.get_or_create("k2")
    check(passed, failed, c["_key"] == "k2",
          "get_or_create with no defaults sets _key only")
    check(passed, failed, c is not a,
          "get_or_create returns distinct instance for different key")

    GNode.clear_registry()

    # ------------------------------------------------------------------
    heading("GraphMixin: clear_registry")
    # ------------------------------------------------------------------
    # After clear_registry() a fresh call to get_or_create must produce a
    # new instance, not the old one.  Calling clear_registry() on a class
    # that has never called get_or_create must be a safe no-op.

    class CNode(GraphMixin, Node):
        pass

    original = CNode.get_or_create("x", {"v": 1})
    CNode.clear_registry()
    reborn   = CNode.get_or_create("x", {"v": 2})

    check(passed, failed, reborn is not original,
          "clear_registry causes next get_or_create to create a new instance")
    check(passed, failed, reborn["v"] == 2,
          "new instance after clear_registry picks up fresh defaults")

    # Clearing a class that has never registered is a harmless no-op.
    class NeverUsed(GraphMixin, Node):
        pass

    does_not_raise(passed, failed,
                   "clear_registry on class with no registry is a no-op",
                   lambda: NeverUsed.clear_registry())

    CNode.clear_registry()

    # ------------------------------------------------------------------
    heading("GraphMixin: registry isolation between subclasses")
    # ------------------------------------------------------------------
    # Each concrete subclass owns an independent registry dict.  The
    # ``"_registry" not in cls.__dict__`` guard in get_or_create is what
    # prevents a subclass from inadvertently sharing its parent's dict.

    class BaseGraph(GraphMixin, Node):
        pass

    class SubGraph(BaseGraph):
        pass

    BaseGraph.clear_registry()
    SubGraph.clear_registry()

    bg = BaseGraph.get_or_create("shared-key", {"source": "base"})
    sg = SubGraph.get_or_create("shared-key", {"source": "sub"})

    check(passed, failed, bg is not sg,
          "subclass registry is independent of parent class registry")
    check(passed, failed, bg["source"] == "base",
          "parent class node holds its own defaults")
    check(passed, failed, sg["source"] == "sub",
          "subclass node holds its own defaults")

    # A key in BaseGraph must not resolve in SubGraph and vice-versa.
    BaseGraph.get_or_create("base-only", {"v": 10})
    check(passed, failed, "base-only" not in SubGraph.__dict__.get("_registry", {}),
          "key registered on parent is absent from subclass registry")

    BaseGraph.clear_registry()
    SubGraph.clear_registry()

    # ------------------------------------------------------------------
    heading("GraphMixin: graph_key / is_known / mark_known")
    # ------------------------------------------------------------------
    # graph_key exposes _key as a property.  is_known/mark_known provide a
    # lightweight visited-flag for BFS traversal without polluting the
    # public payload with implementation details.

    class FlagNode(GraphMixin, Node):
        pass

    FlagNode.clear_registry()

    keyed = FlagNode.get_or_create("fk1")
    plain = FlagNode({})  # not registered — no _key

    check(passed, failed, keyed.graph_key == "fk1",
          "graph_key returns _key for a registered node")
    check(passed, failed, plain.graph_key is None,
          "graph_key returns None for a node without _key")

    check(passed, failed, not keyed.is_known,
          "is_known is False before mark_known()")
    keyed.mark_known()
    check(passed, failed, keyed.is_known,
          "is_known is True after mark_known()")

    check(passed, failed, not plain.is_known,
          "is_known is False on a plain (unregistered) node")

    FlagNode.clear_registry()

    # ------------------------------------------------------------------
    heading("GraphMixin + Serialisable: to_plain $ref emission")
    # ------------------------------------------------------------------
    # When the same keyed (GraphMixin) node is reachable from two places in
    # the tree, to_plain() must emit the full dict exactly once (on first
    # encounter in depth-first order) and {"$ref": key} for every subsequent
    # encounter.  Unkeyed nodes — even if the same Python object appears
    # twice — must never be turned into $ref entries.

    class SNode(GraphMixin, Serialisable, Node):
        _restore_via_payload = True

    class SRoot(Serialisable, Node):
        _restore_via_payload = True

    SNode.clear_registry()

    shared = SNode.get_or_create("art1", {"name": "The Beatles"})
    root   = SRoot({})
    root["ref_a"] = shared   # first path to shared
    root["ref_b"] = shared   # second path to same object

    plain = root.to_plain()

    check(passed, failed,
          isinstance(plain["ref_a"], dict) and plain["ref_a"].get("_key") == "art1",
          "to_plain emits full dict on first encounter of keyed node")
    check(passed, failed,
          plain["ref_b"] == {"$ref": "art1"},
          "to_plain emits $ref on second encounter of same keyed node")

    # An unkeyed node that appears twice must be serialised in full both
    # times — without _key there is no handle for a $ref to name.
    unkeyed = SRoot({})
    unkeyed["x"] = 99
    root2 = SRoot({})
    root2["u1"] = unkeyed
    root2["u2"] = unkeyed  # same Python object, no _key
    plain2 = root2.to_plain()
    check(passed, failed,
          plain2["u1"] == {"x": 99} and plain2["u2"] == {"x": 99},
          "unkeyed node appearing twice is serialised in full both times")

    SNode.clear_registry()

    # ------------------------------------------------------------------
    heading("GraphMixin + Serialisable: restore $ref resolution (graph identity)")
    # ------------------------------------------------------------------
    # A snapshot produced by to_plain() contains $ref markers for shared
    # keyed nodes.  restore() must resolve those markers back to the *same*
    # Python object so that the in-memory graph preserves true identity:
    # mutating the node through one reference is immediately visible through
    # the other.

    class ArtNode(GraphMixin, Serialisable, Node):
        _restore_via_payload = True

    class WeekNode(Serialisable, Node):
        # _node_fields tells _restore_children how to rebuild the child.
        _restore_via_payload = True
        _node_fields = {"artist": ArtNode}

    class RootNode(Serialisable, Node):
        _restore_via_payload = True
        _node_fields = {"w1": WeekNode, "w2": WeekNode}

    ArtNode.clear_registry()

    artist = ArtNode.get_or_create("a1", {"name": "Beatles"})
    w1     = WeekNode({"date": "1963-01-05"})
    w2     = WeekNode({"date": "1963-01-12"})
    w1["artist"] = artist
    w2["artist"] = artist   # shared — same Python object

    root = RootNode({})
    root["w1"] = w1
    root["w2"] = w2

    # Serialise to plain dict — w2's artist should be a $ref.
    plain = root.to_plain()
    check(passed, failed,
          plain["w1"]["artist"].get("_key") == "a1",
          "to_plain: first artist reference is a full dict")
    check(passed, failed,
          plain["w2"]["artist"] == {"$ref": "a1"},
          "to_plain: second artist reference is a $ref")

    # Restore and verify that both paths resolve to the same instance.
    restored = RootNode.restore(plain)
    check(passed, failed,
          isinstance(restored["w1"]["artist"], ArtNode),
          "restored first reference is an ArtNode instance")
    check(passed, failed,
          restored["w1"]["artist"] is restored["w2"]["artist"],
          "restored graph: both references are the same Python object")
    check(passed, failed,
          restored["w1"]["artist"]["name"] == "Beatles",
          "restored shared node carries its original payload")
    check(passed, failed,
          restored["w1"]["date"] == "1963-01-05",
          "restored w1 carries its own scalar payload")
    check(passed, failed,
          restored["w2"]["date"] == "1963-01-12",
          "restored w2 carries its own scalar payload")

    # Mutating through one reference must be visible through the other,
    # which is the whole point of graph identity.
    restored["w1"]["artist"]["name"] = "The Beatles"
    check(passed, failed,
          restored["w2"]["artist"]["name"] == "The Beatles",
          "mutation through one reference is visible through the other")

    ArtNode.clear_registry()

    # ------------------------------------------------------------------
    heading("GraphMixin + Serialisable: restore $ref across SerialisableNodeList")
    # ------------------------------------------------------------------
    # The $ref / _registry mechanism must also work when shared nodes appear
    # as items in a SerialisableNodeList, since list items are restored via
    # SerialisableNodeList.restore() which now threads _registry through.

    class ItemNode(GraphMixin, Serialisable, Node):
        _restore_via_payload = True

    class ListRoot(Serialisable, Node):
        _restore_via_payload = True
        _list_fields = {"entries": (SerialisableNodeList, ItemNode)}

    ItemNode.clear_registry()

    shared_item = ItemNode.get_or_create("i1", {"tag": "shared"})
    list_root   = ListRoot({})
    # The same node appears at two positions in the list.
    lst = SerialisableNodeList([shared_item, shared_item])
    list_root["entries"] = lst

    plain3 = list_root.to_plain()
    check(passed, failed,
          isinstance(plain3["entries"], list) and len(plain3["entries"]) == 2,
          "to_plain produces two-element list")
    check(passed, failed,
          plain3["entries"][0].get("_key") == "i1",
          "to_plain: first list item is full dict")
    check(passed, failed,
          plain3["entries"][1] == {"$ref": "i1"},
          "to_plain: second list item is $ref")

    restored3 = ListRoot.restore(plain3)
    check(passed, failed,
          len(restored3["entries"]) == 2,
          "restored list has correct length")
    check(passed, failed,
          restored3["entries"][0] is restored3["entries"][1],
          "restored list: both entries are the same Python object")

    ItemNode.clear_registry()

    # ------------------------------------------------------------------
    heading("GraphMixin: programmer error — non-string key to get_or_create")
    # ------------------------------------------------------------------
    # Keys are stored as _key in the node payload and used as $ref targets
    # during serialisation.  Non-string keys corrupt JSON round-trips
    # (tuples become lists, integers are valid JSON scalars but ambiguous
    # as $ref values) so get_or_create must reject them immediately with a
    # message explaining why strings are required.

    class ErrGN(GraphMixin, Node):
        pass

    ErrGN.clear_registry()

    msg = catch_into(passed, failed,
                     "get_or_create with int key raises TypeError",
                     TypeError,
                     lambda: ErrGN.get_or_create(42))
    check(passed, failed, "int" in msg and "str" in msg,
          "TypeError names the bad type and required type")
    check(passed, failed, "$ref" in msg or "serialis" in msg,
          "TypeError explains the serialisation reason")

    msg2 = catch_into(passed, failed,
                      "get_or_create with tuple key raises TypeError",
                      TypeError,
                      lambda: ErrGN.get_or_create(("a", "b")))
    check(passed, failed, "tuple" in msg2,
          "TypeError for tuple key names the offending type")

    ErrGN.clear_registry()

    # ------------------------------------------------------------------
    heading("GraphMixin + Serialisable: restore $ref error handling")
    # ------------------------------------------------------------------
    # If a snapshot contains a $ref that was not preceded by a full node
    # definition in the same restore pass, restore() must raise a KeyError
    # that names the missing key so the caller can diagnose the problem.
    # restore() with a non-dict argument must also fail early with a clear
    # message rather than an opaque AttributeError deep in the call stack.

    class ErrNode(GraphMixin, Serialisable, Node):
        _restore_via_payload = True

    class ErrRoot(Serialisable, Node):
        _restore_via_payload = True
        _node_fields = {"child": ErrNode}

    ErrNode.clear_registry()

    # Craft a snapshot where the $ref has no matching full definition.
    bad_plain = {"child": {"$ref": "nonexistent-key"}}

    msg = catch_into(passed, failed,
                     "restore with unresolvable $ref raises KeyError",
                     KeyError,
                     lambda: ErrRoot.restore(bad_plain))
    check(passed, failed,
          "nonexistent-key" in msg,
          "KeyError message contains the missing key")
    check(passed, failed,
          "restore()" in msg or "root node" in msg,
          "KeyError message suggests calling restore() from the root")

    # Non-dict passed to restore() must give a clear TypeError rather than
    # crashing somewhere inside to_plain() or _from_payload().
    msg3 = catch_into(passed, failed,
                      "restore(42) raises TypeError",
                      TypeError,
                      lambda: ErrNode.restore(42))
    check(passed, failed,
          "mapping" in msg3 and "int" in msg3,
          "restore non-dict TypeError says 'mapping' and names the type")
    check(passed, failed,
          "snapshot()" in msg3,
          "restore non-dict TypeError mentions snapshot() as the correct source")

    ErrNode.clear_registry()

    return len(passed), len(failed)
