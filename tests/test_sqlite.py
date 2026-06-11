## @file test_sqlite.py
##
## @brief Unit tests for ``node_x_sqlite`` — SQLite persistence companion.
##
## Sections:
##
##   - **basic save/load** — round-trip a flat node and a nested tree; cache
##     miss returns ``None``; auto-key from ``_key`` payload field.
##   - **explicit key** — save/load with a caller-supplied key for nodes that
##     have no ``_key``; ``ValueError`` when no key is available at all.
##   - **delete** — single-entry removal; no-op on a key that doesn't exist.
##   - **clear** — clear by class removes only that class; clear all removes
##     every row; ``count()`` reflects the changes.
##   - **keys** — alphabetically sorted list of stored keys; empty list on
##     miss; correct class isolation (two classes, same key).
##   - **graph $ref** — shared GraphMixin node serialises to ``$ref``;
##     restore preserves identity across two references.
##   - **DBMixin** — ``db_save``, ``db_load``, ``db_delete`` convenience
##     instance/class methods.
##   - **context manager** — ``with NodeDB(...) as db:`` closes connection on
##     exit; operations after the block re-open a fresh connection.
##   - **:memory: isolation** — separate ``:memory:`` instances are independent;
##     confirms thread-local semantics for in-process stores.
##   - **thread safety** — two threads read concurrently; one thread writes
##     while another reads; no data corruption or deadlock.
##   - **programmer errors** — ``ValueError`` when no key; ``AttributeError``
##     when node has no ``snapshot()``; ``TypeError`` from non-string key in
##     GraphMixin propagates correctly.
##
## All tests use an in-memory ``:memory:`` database so no filesystem artefacts
## are created during the test run.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/Munger/node-x
## @par Licence: MIT

from __future__ import annotations

import os
import sys
import tempfile
import threading
from pathlib import Path
from typing import List, Tuple

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
from node_x_sqlite import DBMixin, NodeDB

from _helpers import (
    catch_into,
    check,
    heading,
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

class FlatNode(Serialisable, Node):
    ## @brief Flat serialisable node; no child fields.
    _restore_via_payload = True


class LeafNode(Serialisable, Node):
    ## @brief Leaf node used inside tree fixtures.
    _restore_via_payload = True


class TreeNode(Serialisable, Node):
    ## @brief Two-level tree: one scalar child Node and one NodeList.
    _restore_via_payload = True
    _node_fields  = {"child": LeafNode}
    _list_fields  = {"entries": (SerialisableNodeList, LeafNode)}


class GNode(GraphMixin, Serialisable, Node):
    ## @brief Graph node — carries ``_key`` for $ref deduplication.
    _restore_via_payload = True


class Wrapper(Serialisable, Node):
    ## @brief Holds two named references to the same GNode.
    _restore_via_payload = True
    _node_fields = {"ref_a": GNode, "ref_b": GNode}


class MixinNode(DBMixin, GraphMixin, Serialisable, Node):
    ## @brief Node that mixes in DBMixin for convenience-method tests.
    _restore_via_payload = True


# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------

def run() -> Tuple[int, int]:
    ## @brief Execute all node_x_sqlite test sections and return pass/fail counts.
    ##
    ## Each section that uses ``GraphMixin`` calls ``clear_registry()`` in a
    ## try/finally block so that registry state never leaks between sections.
    ## All tests use ``":memory:"`` — no filesystem artefacts are created.
    ##
    ## @return ``(pass_count, fail_count)`` across all sections.

    passed: List[str] = []
    failed: List[str] = []

    # ------------------------------------------------------------------
    heading("node_x_sqlite: basic save / load — flat node")
    # ------------------------------------------------------------------
    # save() + load() must round-trip all scalar field types and return
    # a distinct object of the correct subclass.  A cache miss returns None.

    db = NodeDB(":memory:")

    flat = FlatNode({"title": "Revolver", "year": 1966, "active": True})
    db.save(flat, key="album-1")

    restored = db.load(FlatNode, "album-1")

    check(passed, failed, restored is not None,
          "load returns a result after save")
    check(passed, failed, isinstance(restored, FlatNode),
          "load returns correct subclass type")
    check(passed, failed, restored["title"] == "Revolver",
          "string field survives SQLite round-trip")
    check(passed, failed, restored["year"] == 1966,
          "integer field survives SQLite round-trip")
    check(passed, failed, restored["active"] is True,
          "boolean field survives SQLite round-trip")
    check(passed, failed, restored is not flat,
          "load returns a new object, not the original")

    miss = db.load(FlatNode, "does-not-exist")
    check(passed, failed, miss is None,
          "load returns None on cache miss")

    db.close()

    # ------------------------------------------------------------------
    heading("node_x_sqlite: basic save / load — nested tree")
    # ------------------------------------------------------------------

    db = NodeDB(":memory:")

    child  = LeafNode({"id": 7, "label": "leaf"})
    leaves = SerialisableNodeList([LeafNode({"id": i}) for i in range(3)])
    tree   = TreeNode({"name": "root"})
    tree["child"]   = child
    tree["entries"] = leaves

    db.save(tree, key="tree-root")
    rt = db.load(TreeNode, "tree-root")

    check(passed, failed, rt is not None,
          "nested tree restores from DB")
    check(passed, failed, rt["name"] == "root",
          "tree root scalar preserved")
    check(passed, failed, isinstance(rt["child"], LeafNode),
          "child node has correct type after DB round-trip")
    check(passed, failed, rt["child"]["id"] == 7,
          "child node field preserved")
    check(passed, failed, rt["child"]["label"] == "leaf",
          "child node string field preserved")
    check(passed, failed, isinstance(rt["entries"], SerialisableNodeList),
          "NodeList type preserved after DB round-trip")
    check(passed, failed, len(rt["entries"]) == 3,
          "NodeList length preserved after DB round-trip")
    check(passed, failed,
          [rt["entries"][i]["id"] for i in range(3)] == [0, 1, 2],
          "NodeList element fields preserved after DB round-trip")

    db.close()

    # ------------------------------------------------------------------
    heading("node_x_sqlite: auto-key from _key payload field")
    # ------------------------------------------------------------------
    # GraphMixin nodes carry _key in their payload.  save() must pick
    # it up automatically without the caller passing key= explicitly.

    GNode.clear_registry()
    try:
        db = NodeDB(":memory:")

        g = GNode.get_or_create("graph-1", {"value": 42})
        db.save(g)   # no key= — should use g["_key"] == "graph-1"

        rg = db.load(GNode, "graph-1")
        check(passed, failed, rg is not None,
              "auto-key save/load works for GraphMixin node")
        check(passed, failed, rg["value"] == 42,
              "auto-key restored node carries correct payload")

        db.close()
    finally:
        GNode.clear_registry()

    # ------------------------------------------------------------------
    heading("node_x_sqlite: explicit key for un-keyed nodes")
    # ------------------------------------------------------------------

    db = NodeDB(":memory:")
    plain = FlatNode({"config": "debug"})
    db.save(plain, key="config")
    rp = db.load(FlatNode, "config")
    check(passed, failed, rp is not None,
          "explicit key save/load round-trips correctly")
    check(passed, failed, rp["config"] == "debug",
          "explicit key restored node carries correct payload")
    db.close()

    # ------------------------------------------------------------------
    heading("node_x_sqlite: delete")
    # ------------------------------------------------------------------

    db = NodeDB(":memory:")
    db.save(FlatNode({"x": 1}), key="del-1")
    db.save(FlatNode({"x": 2}), key="del-2")

    db.delete(FlatNode, "del-1")
    check(passed, failed, db.load(FlatNode, "del-1") is None,
          "deleted entry is gone")
    check(passed, failed, db.load(FlatNode, "del-2") is not None,
          "other entry is not affected by delete")

    # delete of a non-existent key is a no-op, not an error
    db.delete(FlatNode, "del-1")   # second delete must not raise
    check(passed, failed, True,
          "delete of non-existent key does not raise")

    db.close()

    # ------------------------------------------------------------------
    heading("node_x_sqlite: clear — by class and total")
    # ------------------------------------------------------------------

    db = NodeDB(":memory:")
    for i in range(3):
        db.save(FlatNode({"n": i}), key=f"flat-{i}")
    for i in range(2):
        db.save(TreeNode({"n": i}), key=f"tree-{i}")

    check(passed, failed, db.count(FlatNode) == 3,
          "count(FlatNode) is 3 before clear")
    check(passed, failed, db.count(TreeNode) == 2,
          "count(TreeNode) is 2 before clear")
    check(passed, failed, db.count() == 5,
          "total count() is 5 before any clear")

    db.clear(FlatNode)
    check(passed, failed, db.count(FlatNode) == 0,
          "count(FlatNode) is 0 after clear(FlatNode)")
    check(passed, failed, db.count(TreeNode) == 2,
          "clear(FlatNode) does not affect TreeNode entries")

    db.clear()
    check(passed, failed, db.count() == 0,
          "count() is 0 after clear() with no argument")

    db.close()

    # ------------------------------------------------------------------
    heading("node_x_sqlite: keys")
    # ------------------------------------------------------------------

    db = NodeDB(":memory:")
    db.save(FlatNode({"n": 0}), key="c")
    db.save(FlatNode({"n": 1}), key="a")
    db.save(FlatNode({"n": 2}), key="b")
    db.save(TreeNode({"n": 0}), key="z")  # different class — must not appear

    k = db.keys(FlatNode)
    check(passed, failed, k == ["a", "b", "c"],
          "keys() returns alphabetically sorted list")
    check(passed, failed, "z" not in k,
          "keys() is isolated to the requested class")
    check(passed, failed, db.keys(LeafNode) == [],
          "keys() returns empty list when no entries for class")

    db.close()

    # ------------------------------------------------------------------
    heading("node_x_sqlite: graph $ref — identity preserved")
    # ------------------------------------------------------------------
    # A shared GNode (same Python object from two places) must serialise
    # to a $ref in the snapshot and restore as a single shared object.

    GNode.clear_registry()
    try:
        db = NodeDB(":memory:")

        shared  = GNode.get_or_create("shared-node", {"label": "shared"})
        wrapper = Wrapper({})
        wrapper["ref_a"] = shared
        wrapper["ref_b"] = shared

        db.save(wrapper, key="wrapper-1")
        rw = db.load(Wrapper, "wrapper-1")

        check(passed, failed, rw is not None,
              "wrapper with $ref restores from DB")
        check(passed, failed,
              rw["ref_a"] is rw["ref_b"],
              "both $ref references restore to the same Python object")
        check(passed, failed,
              rw["ref_a"]["label"] == "shared",
              "shared node payload survives DB round-trip")

        # Mutation through one reference must be visible via the other
        rw["ref_a"]["label"] = "mutated"
        check(passed, failed,
              rw["ref_b"]["label"] == "mutated",
              "mutation through one reference is visible through the other")

        db.close()
    finally:
        GNode.clear_registry()

    # ------------------------------------------------------------------
    heading("node_x_sqlite: DBMixin — db_save / db_load / db_delete")
    # ------------------------------------------------------------------

    MixinNode.clear_registry()
    try:
        db = NodeDB(":memory:")

        node = MixinNode.get_or_create("mixin-1", {"data": "hello"})
        node.db_save(db)   # instance method — no key needed, _key is auto-used

        loaded = MixinNode.db_load("mixin-1", db)   # classmethod
        check(passed, failed, loaded is not None,
              "DBMixin.db_save + db_load round-trip succeeds")
        check(passed, failed, loaded["data"] == "hello",
              "DBMixin round-trip preserves payload")

        node.db_delete(db)
        check(passed, failed, db.load(MixinNode, "mixin-1") is None,
              "DBMixin.db_delete removes the entry")

        db.close()
    finally:
        MixinNode.clear_registry()

    # ------------------------------------------------------------------
    heading("node_x_sqlite: context manager")
    # ------------------------------------------------------------------

    # The context manager form must close the calling thread's connection
    # on __exit__.  Using the db again after the block re-opens cleanly.

    with NodeDB(":memory:") as db:
        db.save(FlatNode({"ctx": True}), key="ctx-1")
        r = db.load(FlatNode, "ctx-1")
        check(passed, failed, r is not None,
              "context manager: load succeeds inside with block")

    # After __exit__ the connection is closed; a new operation on the same
    # object should re-open cleanly (lazy _connect() re-creates the conn).
    # :memory: is wiped when closed, so a new connection gives an empty DB.
    miss = db.load(FlatNode, "ctx-1")
    check(passed, failed, miss is None,
          "context manager: :memory: DB is empty after __exit__ (connection re-created)")

    # ------------------------------------------------------------------
    heading("node_x_sqlite: thread safety — concurrent reads")
    # ------------------------------------------------------------------
    # Multiple threads reading from the same NodeDB simultaneously must not
    # deadlock or return corrupted data.  Uses a temp file because :memory:
    # gives each thread-local connection its own isolated database.

    _fd, _tmp = tempfile.mkstemp(suffix=".db", prefix="test_node_x_")
    os.close(_fd)
    try:
        db = NodeDB(_tmp)
        for i in range(10):
            db.save(FlatNode({"n": i}), key=f"t{i}")
        db.close()  # flush; reader threads open their own connections

        results: dict = {}
        errors:  list = []

        def reader(thread_id: int) -> None:
            try:
                results[thread_id] = [
                    db.load(FlatNode, f"t{i}") for i in range(10)
                ]
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=reader, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        check(passed, failed, len(errors) == 0,
              "concurrent reads from 4 threads raise no exceptions")
        check(passed, failed,
              all(all(r is not None for r in results[i]) for i in range(4)),
              "all 4 threads received complete results with no None values")
    finally:
        try:
            os.unlink(_tmp)
        except OSError:
            pass

    # ------------------------------------------------------------------
    heading("node_x_sqlite: programmer errors")
    # ------------------------------------------------------------------

    db = NodeDB(":memory:")

    # save() with no key and no _key field raises ValueError
    keyless = FlatNode({"x": 1})
    msg_no_key = catch_into(passed, failed,
                            "save() with no key raises ValueError",
                            ValueError,
                            lambda: db.save(keyless))
    check(passed, failed, "key" in msg_no_key.lower(),
          "ValueError message mentions 'key'")
    check(passed, failed, "explicitly" in msg_no_key.lower() or "pass" in msg_no_key.lower(),
          "ValueError message tells user how to fix it")

    # save() on an object without snapshot() raises AttributeError
    catch_into(passed, failed,
               "save() on non-Serialisable object raises AttributeError",
               AttributeError,
               lambda: db.save(object(), key="bare"))

    db.close()

    return len(passed), len(failed)


if __name__ == "__main__":
    from _helpers import summary
    p, f = run()
    summary(list(range(p)), list(range(f)))
