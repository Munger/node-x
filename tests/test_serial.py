## @file test_serial.py
##
## @brief Unit tests for the ``Serialisable`` mixin and
##        ``SerialisableList``.
##
## Sections:
##
##   - **to_plain** — flat dict, nested Nodes, and pass-through of
##     opaque scalar types (tuple, bytes) that ``json.dumps`` cannot
##     handle but that ``to_plain`` must not alter.
##   - **snapshot** — scalar fields, nested Nodes, and the documented
##     ordering guarantee (scalars emitted before nested structures).
##   - **to_pretty_json** — valid JSON output and indentation.
##   - **restore** — round-trip reconstruction, subclass-type
##     preservation, non-dict argument rejection, and the wrapped
##     ``TypeError`` produced when ``__init__`` itself raises.
##   - **clone** — scalar copy, deep Node copy, distinct references,
##     shared-reference memo preservation, and ``SerialisableList``
##     type preservation (regression for the bug where clone() silently
##     demoted NodeList to a plain list).
##   - **_restore_via_payload** — bypass-``__init__`` path.
##   - **_from_payload + WriteMutex** — regression for the bug
##     where _from_payload omitted _rw_lock initialisation.
##   - **_restore_children** — None snapshot and unknown-field
##     edge cases.
##   - **SerialisableList** — snapshot, restore (including the
##     fallback path when item_type has no restore()), to_pretty_json,
##     and empty-list edge cases.
##   - **disk round-trip** — full JSON serialise-to-file and
##     restore-from-file cycle covering scalars, booleans, a child Node,
##     and a SerialisableList.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Tuple

_PKG_DIR = str(Path(__file__).resolve().parent)
_ROOT_DIR = str(Path(__file__).resolve().parent.parent)
for _d in (_PKG_DIR, _ROOT_DIR):
    if _d not in sys.path:
        sys.path.insert(0, _d)

from node_x import (
    Node,
    NodeList,
    WriteMutex,
    Serialisable,
    SerialisableList,
)

from _helpers import (
    check,
    check_catch,
    check_does_not_raise,
    heading,
)


class SerNode(Serialisable, Node):
    ## @brief Concrete serialisable node subclass used throughout the tests.
    ##
    ## Declares ``child_nodes`` as a child field so ``to_plain`` and
    ## ``snapshot`` recurse into it.  See ``DiskNode`` below for the
    ## full restore-of-children path.

    _children = ("child_nodes",)


def run() -> Tuple[int, int]:
    ## @brief Execute all Serialisable test sections and return pass/fail counts.
    ##
    ## Each section creates fresh instances so failures do not cascade.
    ## The disk round-trip section writes to ``/tmp`` and cleans up via
    ## a ``finally`` block regardless of test outcome.
    ##
    ## @return ``(pass_count, fail_count)`` across all sections.

    passed: List[str] = []
    failed: List[str] = []

    # ------------------------------------------------------------------
    heading("Serialisable: to_plain")
    # ------------------------------------------------------------------
    # to_plain() converts the node tree to plain Python structures with
    # no Node instances, making it safe to pass to json.dumps or any
    # other serialiser.  Opaque scalar types (tuple, bytes) that Node
    # allows in its payload must be returned unchanged — to_plain() is
    # not responsible for JSON encoding, only for unwrapping Node types.

    n = SerNode({"a": 1, "b": "two"})
    plain = n.to_plain()
    check(passed, failed, plain == {"a": 1, "b": "two"},
          "to_plain returns flat dict")

    child = SerNode({"x": 10})
    parent = SerNode({"label": "root"})
    parent["child"] = child
    plain = parent.to_plain()
    check(passed, failed,
          plain["child"] == {"x": 10},
          "to_plain recurses into child Nodes")

    # tuple and bytes are valid Node payload types; to_plain() must
    # pass them through untouched rather than trying to convert them.
    n_tup = SerNode({"t": (1, "two", None), "b": b"raw"})
    plain_tup = n_tup.to_plain()
    check(passed, failed,
          plain_tup["t"] == (1, "two", None) and plain_tup["b"] == b"raw",
          "to_plain passes through tuple and bytes values unchanged")

    # ------------------------------------------------------------------
    heading("Serialisable: snapshot")
    # ------------------------------------------------------------------
    # snapshot() calls to_plain() and then reorders the result so that
    # scalar fields appear before nested structures, making snapshots
    # easier to read in logs and diffs without requiring each subclass
    # to define its own field ordering.

    n = SerNode({"a": 1, "b": "two"})
    snap = n.serialise(deep=True)
    check(passed, failed, snap["a"] == 1 and snap["b"] == "two",
          "snapshot contains all scalar fields")

    child = SerNode({"x": 10})
    parent = SerNode({"label": "root"})
    parent["child"] = child
    snap = parent.serialise(deep=True)
    check(passed, failed,
          snap["child"] == {"x": 10},
          "snapshot recurses into child Nodes")

    # Build a node where a nested field comes first in insertion order
    # so we can verify that snapshot() moves it after the scalars.
    mixed = SerNode({"z_nested": SerNode({"deep": 1}), "a_scalar": 42, "b_scalar": "hi"})
    snap_mixed = mixed.serialise(deep=True)
    keys = list(snap_mixed.keys())
    scalar_idx = max(keys.index("a_scalar"), keys.index("b_scalar"))
    nested_idx = keys.index("z_nested")
    check(passed, failed, scalar_idx < nested_idx,
          "snapshot places scalars before nested structures")

    # ------------------------------------------------------------------
    heading("Serialisable: to_pretty_json")
    # ------------------------------------------------------------------

    n = SerNode({"a": 1})
    js = n.to_pretty_json()
    parsed = json.loads(js)
    check(passed, failed, parsed["a"] == 1,
          "to_pretty_json produces valid JSON")
    check(passed, failed, "\n" in js,
          "to_pretty_json is indented")

    # ------------------------------------------------------------------
    heading("Serialisable: restore")
    # ------------------------------------------------------------------
    # restore() reconstructs a node from a snapshot dict.  The default
    # path passes the snapshot as the single positional argument to
    # __init__.  When __init__ raises TypeError (e.g. because it
    # requires arguments the snapshot cannot supply), restore() wraps
    # the error with a hint about _restore_via_payload.

    n = SerNode({"a": 1, "b": "two"})
    snap = n.serialise(deep=True)
    restored = SerNode.deserialise(snap)
    check(passed, failed, restored["a"] == 1 and restored["b"] == "two",
          "restore reconstructs node from snapshot")
    check(passed, failed, isinstance(restored, SerNode),
          "restore preserves subclass type")

    msg = check_catch(passed, failed,
                      "restore(42) raises TypeError",
                      TypeError, lambda: SerNode.deserialise(42))
    check(passed, failed,
          "expected a mapping" in msg and "int" in msg and "serialise" in msg,
          "restore type message says 'mapping', names type, mentions serialise()")

    # When __init__ raises TypeError the error must be re-raised with a
    # message that points the developer to _restore_via_payload.
    class BadInitNode(Serialisable, Node):
        def __init__(self, *args, **kwargs):
            raise TypeError("intentional init failure")

    msg = check_catch(passed, failed,
                      "restore() wraps __init__ TypeError",
                      TypeError,
                      lambda: BadInitNode.deserialise({"a": 1}))
    check(passed, failed,
          "deserialise()" in msg and "restore_via_payload" in msg,
          "wrapped restore error mentions deserialise() and restore_via_payload")

    # ------------------------------------------------------------------
    heading("Serialisable: clone")
    # ------------------------------------------------------------------
    # clone() produces a deep copy of the node tree using __new__ +
    # dict.__init__ to avoid calling __init__ (which may have side
    # effects).  Shared Node references within the tree must map to
    # the same clone via the memo dict.  SerialisableList values
    # must be cloned as SerialisableList, not demoted to plain list
    # (regression guard for the clone() NodeList bug).

    child = SerNode({"x": (1, 2, 3)})
    parent = SerNode({"label": "root"})
    parent["child"] = child
    cloned = parent.clone()

    check(passed, failed, cloned["label"] == "root",
          "clone copies scalar fields")
    check(passed, failed, cloned["child"]["x"] == (1, 2, 3),
          "clone deep-copies nested Nodes")
    check(passed, failed, cloned["child"] is not child,
          "clone produces distinct child reference")

    # Two payload keys pointing to the same Node instance must both
    # point to the same (single) clone in the result.
    shared = SerNode({"v": 1})
    parent_shared = SerNode({"a": shared, "b": shared})
    cloned_shared = parent_shared.clone()
    check(passed, failed, cloned_shared["a"] is cloned_shared["b"],
          "clone preserves shared Node references via memo")

    # SerialisableList must survive clone() with its type intact.
    class RWSerNode(WriteMutex, Serialisable, Node):
        pass

    rw_src = RWSerNode({"v": 1})
    rw_clone = rw_src.clone()
    check(passed, failed, rw_src._rw_lock is not rw_clone._rw_lock,
          "clone gives WriteMutex node a fresh _rw_lock (not shared)")

    nl_orig = SerialisableList([SerNode({"i": 0}), SerNode({"i": 1})])
    parent_nl = SerNode({"label": "with-list"})
    parent_nl["kids"] = nl_orig
    cloned_nl = parent_nl.clone()
    check(passed, failed, isinstance(cloned_nl["kids"], SerialisableList),
          "clone preserves SerialisableList type (not plain list)")
    check(passed, failed, len(cloned_nl["kids"]) == 2,
          "cloned SerialisableList has correct length")
    check(passed, failed, cloned_nl["kids"] is not nl_orig,
          "cloned SerialisableList is a distinct object")
    check(passed, failed, cloned_nl["kids"][0] is not nl_orig[0],
          "cloned SerialisableList elements are distinct objects")

    # ------------------------------------------------------------------
    heading("Serialisable: _restore_via_payload")
    # ------------------------------------------------------------------
    # When _restore_via_payload is True, restore() uses _from_payload()
    # instead of __init__, bypassing validation and side effects.
    # This is the intended pattern for nodes whose __init__ requires
    # runtime arguments that are not stored in the snapshot.

    class PayloadNode(Serialisable, Node):
        _restore_via_payload = True

    original = PayloadNode({"a": 1})
    snap = original.serialise(deep=True)
    restored = PayloadNode.deserialise(snap)
    check(passed, failed, restored["a"] == 1,
          "_restore_via_payload restore works")

    # ------------------------------------------------------------------
    heading("Serialisable: _from_payload initialises _rw_lock for WriteMutex")
    # ------------------------------------------------------------------
    # _from_payload() bypasses __init__, which means WriteMutex.__init__
    # is never called.  The fix initialises _rw_lock explicitly when the
    # class includes WriteMutex in its MRO.  This test guards against
    # regression: without the fix the first mutation on the restored node
    # raises AttributeError on _rw_lock.

    class RWPayloadNode(WriteMutex, Serialisable, Node):
        _restore_via_payload = True

    rw_orig = RWPayloadNode({"x": 10})
    rw_snap = rw_orig.serialise(deep=True)
    rw_restored = RWPayloadNode.deserialise(rw_snap)
    check_does_not_raise(passed, failed,
                         "_from_payload + WriteMutex: mutation does not raise",
                         lambda: rw_restored.__setitem__("y", 20))
    check(passed, failed, rw_restored["y"] == 20,
          "_from_payload + WriteMutex: write succeeds after restore")

    # ------------------------------------------------------------------
    heading("Serialisable: _restore_children edge cases")
    # ------------------------------------------------------------------
    # _restore_children() is a helper for custom restore() implementations.
    # It must silently skip when the snapshot is None and silently ignore
    # keys that are not declared in node_fields or list_fields.

    class EmptyNode(Serialisable, Node):
        node_fields: Dict[str, Any] = {}
        list_fields: Dict[str, Any] = {}

    empty = EmptyNode()
    EmptyNode._restore_children(empty, None)
    check(passed, failed, True,
          "_restore_children with None snapshot is safe")

    EmptyNode._restore_children(empty, {"unexpected": 1})
    check(passed, failed, True,
          "_restore_children with unknown fields is safe")

    # ------------------------------------------------------------------
    heading("SerialisableList: snapshot / restore")
    # ------------------------------------------------------------------
    # snapshot() produces a list of plain dicts by calling snapshot()
    # on each element.  restore() reconstructs elements via item_type.deserialise()
    # if available, otherwise falls back to item_type(snap) directly.
    # Already-instantiated item_type instances must pass through unchanged.

    items = [SerNode({"i": 1}), SerNode({"i": 2})]
    snl = SerialisableList(items)
    snapshots = snl.serialise(deep=True)
    check(passed, failed, len(snapshots) == 2,
          "SerialisableList.snapshot returns list of snapshots")

    restored_list = SerialisableList.deserialise(snapshots, SerNode)
    check(passed, failed, len(restored_list) == 2,
          "SerialisableList.restore reconstructs elements")

    # Already-instantiated instances must pass through the isinstance
    # check and not be re-constructed.
    raw_list = SerialisableList.deserialise([SerNode({"x": 1})], SerNode)
    check(passed, failed, len(raw_list) == 1,
          "SerialisableList.restore passes through Node instances")

    # ------------------------------------------------------------------
    heading("SerialisableList: to_pretty_json")
    # ------------------------------------------------------------------

    snl = SerialisableList([SerNode({"a": 1})])
    js = snl.to_pretty_json()
    parsed = json.loads(js)
    check(passed, failed, isinstance(parsed, list) and parsed[0]["a"] == 1,
          "SerialisableList.to_pretty_json works")

    # Empty list edge cases: snapshot and JSON serialisation must both
    # produce valid empty-list representations.
    empty_snl = SerialisableList()
    check(passed, failed, empty_snl.serialise(deep=True) == [],
          "SerialisableList.snapshot on empty list returns []")
    check(passed, failed, json.loads(empty_snl.to_pretty_json()) == [],
          "SerialisableList.to_pretty_json on empty list returns '[]'")

    # When item_type has no restore() method, restore() falls back to
    # calling item_type(snap) directly (the plain Node constructor path).
    class NoRestoreNode(Node):
        pass

    snl_nr = SerialisableList.deserialise([{"x": 1}], NoRestoreNode)
    check(passed, failed,
          len(snl_nr) == 1 and isinstance(snl_nr[0], NoRestoreNode),
          "SerialisableList.restore falls back to item_type(snap) when no restore()")

    # ------------------------------------------------------------------
    heading("Serialisable: clone on Serialisable subclass")
    # ------------------------------------------------------------------

    class ClonableNode(Serialisable, Node):
        pass

    wn = ClonableNode({"x": 1})
    cloned = wn.clone()
    check(passed, failed, cloned["x"] == 1,
          "clone works on Serialisable subclass")

    # ------------------------------------------------------------------
    heading("Serialisable: disk round-trip")
    # ------------------------------------------------------------------
    # DiskNode demonstrates the canonical pattern for a Serialisable
    # subclass with typed children: use _restore_via_payload to load
    # scalar fields via _from_payload, then call _restore_children to
    # rebuild the child Node and SerialisableList.

    class DiskNode(Serialisable, Node):
        ## @brief Node with custom restore that reconstructs children from a snapshot.
        ##
        ## Declares ``node_fields`` and ``list_fields`` so ``_restore_children``
        ## can rebuild child Nodes and NodeLists from plain dicts.  Sets
        ## ``_restore_via_payload = True`` to bypass ``__setitem__`` validation
        ## during construction (the raw snapshot dict is loaded directly).
        ##
        ## The custom ``restore()`` is the intended pattern for any Serialisable
        ## subclass with children: construct from payload, then restore children.

        node_fields = {"sub": SerNode}
        list_fields = {"child_nodes": (SerialisableList, SerNode)}
        _restore_via_payload = True

        @classmethod
        def restore(cls, snapshot: Any) -> Any:
            ## @brief Reconstruct a DiskNode tree from a snapshot dict.
            ##
            ## Two-phase restore: load scalar fields via ``_from_payload``
            ## (which uses ``dict.__init__`` to bypass validation), then
            ## rebuild child Nodes/NodeLists via ``_restore_children``.
            ##
            ## @param snapshot  Plain dict from ``snapshot()``.
            ## @return A new ``DiskNode`` with fully reanimated children.

            node = cls._from_payload(snapshot)
            cls._restore_children(node, snapshot)
            return node

    # Build a two-level tree: root → scalar fields + child Node + NodeList.
    original = DiskNode({
        "name": "root",
        "count": 42,
        "active": True,
    })
    child = SerNode({"id": 1, "label": "leaf"})
    original["sub"] = child
    items = SerialisableList([
        SerNode({"idx": 0}),
        SerNode({"idx": 1}),
    ])
    original["child_nodes"] = items

    # Phase 1 — snapshot tree to JSON and write to /tmp.
    snap = original.serialise(deep=True)
    js = json.dumps(snap, indent=2)

    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, dir="/tmp",
    )
    try:
        tmp.write(js)
        tmp.close()

        # Phase 2 — read JSON back from disk, restore tree from plain dict.
        with open(tmp.name) as f:
            restored_data = json.load(f)

        restored = DiskNode.deserialise(restored_data)

        # Phase 3 — verify every field, type, and identity constraint.
        check(passed, failed, isinstance(restored, DiskNode),
              "restored tree has correct type")
        check(passed, failed, restored["name"] == "root",
              "restored scalar 'name' matches")
        check(passed, failed, restored["count"] == 42,
              "restored scalar 'count' matches")
        check(passed, failed, restored["active"] is True,
              "restored bool 'active' matches")
        check(passed, failed, isinstance(restored["sub"], SerNode),
              "restored child is a Node instance")
        check(passed, failed, restored["sub"]["id"] == 1,
              "restored child node field matches")
        check(passed, failed, restored["sub"]["label"] == "leaf",
              "restored child node label matches")
        check(passed, failed, isinstance(restored["child_nodes"],
                                         SerialisableList),
              "restored node list is NodeList instance")
        check(passed, failed, len(restored["child_nodes"]) == 2,
              "restored node list has correct length")
        check(passed, failed,
              restored["child_nodes"][0]["idx"] == 0,
              "restored node list element 0 matches")
        check(passed, failed,
              restored["child_nodes"][1]["idx"] == 1,
              "restored node list element 1 matches")
        # Identity checks: restore must produce new objects, not aliases
        # of the originals.
        check(passed, failed, restored is not original,
              "restored tree is distinct from original")
        check(passed, failed, restored["sub"] is not child,
              "restored child is distinct from original child")
        check(passed, failed, restored["child_nodes"] is not items,
              "restored node list is distinct from original list")

    finally:
        os.unlink(tmp.name)

    return len(passed), len(failed)
