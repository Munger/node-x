## @file test_stream.py
##
## @brief Unit tests for the ``StreamMixin`` lazy-loading contract.
##
## ``StreamMixin`` is the engine for lazy tree construction: a caller
## obtains a root node, begins iterating it via ``stream()``, and
## children materialise on demand.  Each child may itself be streamed,
## giving a recursive, depth-first lazy population model.
##
## ``stream()`` takes an optional ``data`` argument:
##
##   - ``data=None``  -no bytes are provided; the implementation
##     obtains its source however it chooses (e.g. reads a file).
##   - ``data=bytes`` -a pre-fetched payload is passed in; the
##     implementation parses it directly without any I/O.
##
## Sections:
##
##   - **Base class** -``stream()`` with and without data both yield
##     nothing, confirming the default no-op for both calling forms.
##   - **Without data (data=None)** -override uses internal node state;
##     each call to ``stream()`` produces an independent generator.
##   - **With data (data=bytes)** -override parses the provided bytes;
##     different payloads produce different children.
##   - **Laziness** - nodes are constructed only as the iterator is
##     consumed; early exit leaves unconsumed nodes uncreated.
##   - **Re-entrancy** - two simultaneous generators from the same node
##     advance independently.
##   - **Cascading streams** -children yielded by ``stream()`` can
##     themselves be streamed, building the tree level by level.
##   - **Tree-walk + stream** -the canonical usage pattern: walk the
##     static ``_children`` skeleton with ``_tree_iter()``, then call
##     ``stream()`` on each node to populate dynamic children.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT

from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

_PKG_DIR = str(Path(__file__).resolve().parent)
_ROOT_DIR = str(Path(__file__).resolve().parent.parent)
for _d in (_PKG_DIR, _ROOT_DIR):
    if _d not in sys.path:
        sys.path.insert(0, _d)

from node_x import Node, NodeList, StreamMixin

from _helpers import check, check_does_not_raise, heading


def run() -> Tuple[int, int]:
    ## @brief Execute all StreamMixin test sections and return pass/fail counts.
    ##
    ## @return ``(pass_count, fail_count)`` across all sections.

    passed: List[str] = []
    failed: List[str] = []

    # ------------------------------------------------------------------
    heading("StreamMixin: base class yields nothing in both calling forms")
    # ------------------------------------------------------------------
    # The unoverridden stream() is a no-op whether or not bytes are
    # passed.  Nodes that have nothing to discover simply do not
    # override the method.

    base = StreamMixin()
    check(passed, failed, list(base.stream()) == [],
          "base stream(data=None) yields nothing")
    check(passed, failed, list(base.stream(data=b"payload")) == [],
          "base stream(data=bytes) yields nothing regardless of content")

    # ------------------------------------------------------------------
    heading("StreamMixin: without data (data=None)")
    # ------------------------------------------------------------------
    # When no bytes are provided, the implementation derives children
    # from the node's own payload or internal state.  Each call to
    # stream() must return an independent generator so the same node
    # can be iterated more than once.

    class InternalNode(StreamMixin, Node):
        def stream(self, data: Optional[bytes] = None) -> Iterator[Node]:
            for v in self.get("vals", ()):
                yield Node({"val": v})

    sn = InternalNode({"vals": (10, 20, 30)})
    children = list(sn.stream())
    check(passed, failed, len(children) == 3,
          "stream() without data yields one child per entry")
    check(passed, failed, children[0]["val"] == 10 and children[2]["val"] == 30,
          "stream() without data yields children in order")

    # A second call must produce a fresh, independent sequence.
    children2 = list(sn.stream())
    check(passed, failed, len(children2) == 3,
          "stream() is re-callable and yields same count")
    check(passed, failed, children[0] is not children2[0],
          "each stream() call produces distinct Node instances")

    # A node with no entries must yield nothing without error.
    empty_sn = InternalNode({"vals": ()})
    check(passed, failed, list(empty_sn.stream()) == [],
          "stream() on node with no entries yields nothing")

    # ------------------------------------------------------------------
    heading("StreamMixin: with data (data=bytes)")
    # ------------------------------------------------------------------
    # When bytes are provided, the implementation parses them to produce
    # children.  Different payloads must produce different child sets,
    # and passing None vs bytes must dispatch to distinct behaviour.

    class BytesNode(StreamMixin, Node):
        def stream(self, data: Optional[bytes] = None) -> Iterator[Node]:
            if data is None:
                return
            for line in data.decode().splitlines():
                line = line.strip()
                if line:
                    yield Node({"line": line})

    bn = BytesNode()

    # No bytes: no children.
    check(passed, failed, list(bn.stream()) == [],
          "stream(data=None) yields nothing when override requires data")

    # Three lines of bytes: three children.
    payload = b"alpha\nbeta\ngamma\n"
    lines = list(bn.stream(data=payload))
    check(passed, failed, len(lines) == 3,
          "stream(data=bytes) yields one child per line")
    check(passed, failed, lines[0]["line"] == "alpha" and lines[2]["line"] == "gamma",
          "stream(data=bytes) yields children with correct content")

    # Different payload produces a different child set.
    lines2 = list(bn.stream(data=b"one\ntwo\n"))
    check(passed, failed, len(lines2) == 2,
          "stream with different data yields different children")

    # Empty bytes yields nothing.
    check(passed, failed, list(bn.stream(data=b"")) == [],
          "stream(data=b'') yields nothing")

    # ------------------------------------------------------------------
    heading("StreamMixin: laziness - nodes created only on consumption")
    # ------------------------------------------------------------------
    # stream() must be a generator: child nodes are constructed only as
    # the caller advances the iterator.  Verified by tracking
    # construction side-effects and confirming that each next() call
    # triggers exactly one construction event.

    construction_log: List[int] = []

    class TrackedNode(StreamMixin, Node):
        def stream(self, data: Optional[bytes] = None) -> Iterator[Node]:
            for i in range(5):
                construction_log.append(i)
                yield Node({"seq": i})

    tn = TrackedNode()
    gen = tn.stream()

    check(passed, failed, len(construction_log) == 0,
          "stream() generator does not execute before first next()")

    first = next(gen)
    check(passed, failed, len(construction_log) == 1 and first["seq"] == 0,
          "first next() constructs exactly one node")

    second = next(gen)
    check(passed, failed, len(construction_log) == 2 and second["seq"] == 1,
          "second next() constructs exactly one more node")

    # ------------------------------------------------------------------
    heading("StreamMixin: early exit - partial consumption is safe")
    # ------------------------------------------------------------------
    # Breaking out of the loop before exhausting stream() must not
    # cause any error, and the node must remain fully usable afterwards.

    exit_log: List[int] = []

    class ExitNode(StreamMixin, Node):
        def stream(self, data: Optional[bytes] = None) -> Iterator[Node]:
            for i in range(100):
                exit_log.append(i)
                yield Node({"seq": i})

    en = ExitNode({"status": "ok"})
    consumed = []
    for child in en.stream():
        consumed.append(child)
        if len(consumed) == 3:
            break

    check(passed, failed, len(consumed) == 3,
          "early exit consumes exactly the requested number of children")
    check(passed, failed, len(exit_log) == 3,
          "early exit does not construct unconsumed nodes")
    check(passed, failed, en["status"] == "ok",
          "node remains usable after partial stream consumption")

    # ------------------------------------------------------------------
    heading("StreamMixin: re-entrancy - two simultaneous generators")
    # ------------------------------------------------------------------
    # Two generators from the same node must advance independently.

    class CountingNode(StreamMixin, Node):
        def stream(self, data: Optional[bytes] = None) -> Iterator[Node]:
            for i in range(4):
                yield Node({"i": i})

    rn = CountingNode()
    gen_a = rn.stream()
    gen_b = rn.stream()

    a0 = next(gen_a)
    a1 = next(gen_a)
    b0 = next(gen_b)

    check(passed, failed, a0["i"] == 0 and a1["i"] == 1,
          "first generator advances to position 1 independently")
    check(passed, failed, b0["i"] == 0,
          "second generator starts at position 0 regardless of first")

    # ------------------------------------------------------------------
    heading("StreamMixin: cascading streams (recursive lazy load)")
    # ------------------------------------------------------------------
    # Children yielded by stream() can themselves be streamed, building
    # the tree level by level.  No children exist until stream() is
    # called; the caller drives population by iterating level by level.

    class LevelNode(StreamMixin, Node):
        ## @brief Node that streams a fixed number of child LevelNodes.

        def stream(self, data: Optional[bytes] = None) -> Iterator[Node]:
            n = self.get("n_children", 0)
            cc = self.get("child_children", 0)
            for i in range(n):
                yield LevelNode({"label": f"child-{i}", "n_children": cc,
                                 "child_children": 0})

    root = LevelNode({"label": "root", "n_children": 3, "child_children": 2})

    check(passed, failed, "label" in root and root["label"] == "root",
          "root node exists before streaming")

    level1 = list(root.stream())
    check(passed, failed, len(level1) == 3,
          "root.stream() yields 3 level-1 children")
    check(passed, failed, all(isinstance(c, LevelNode) for c in level1),
          "level-1 children are LevelNode instances")

    level2 = []
    for child in level1:
        level2.extend(child.stream())

    check(passed, failed, len(level2) == 6,
          "streaming all level-1 children yields 6 level-2 grandchildren")
    check(passed, failed, list(level2[0].stream()) == [],
          "level-2 nodes have no further children")

    # ------------------------------------------------------------------
    heading("StreamMixin: tree-walk + stream (canonical usage pattern)")
    # ------------------------------------------------------------------
    # Walk the static _children skeleton with _tree_iter(), call
    # stream() on each node, and collect all dynamically-discovered
    # children.  This is how a caller populates the full tree on demand.

    class ContainerNode(StreamMixin, Node):
        ## @brief Static parent node holding sub-containers.
        _children = ("subnodes",)

        def stream(self, data: Optional[bytes] = None) -> Iterator[Node]:
            for name in self.get("names", ()):
                yield LeafNode({"name": name})

    class LeafNode(StreamMixin, Node):
        ## @brief Dynamically-discovered leaf node with no further children.

        def stream(self, data: Optional[bytes] = None) -> Iterator[Node]:
            return
            yield  # make it a generator

    sub_a = ContainerNode({"names": ("item-a1", "item-a2")})
    sub_b = ContainerNode({"names": ("item-b1",)})
    root_c = ContainerNode({"names": ()})
    root_c["subnodes"] = NodeList([sub_a, sub_b])

    all_leaves: List[Node] = []
    for node in root_c._tree_iter():
        for leaf in node.stream():
            all_leaves.append(leaf)

    check(passed, failed, len(all_leaves) == 3,
          "tree-walk + stream discovers all dynamic children across skeleton")
    names = {leaf["name"] for leaf in all_leaves}
    check(passed, failed, names == {"item-a1", "item-a2", "item-b1"},
          "all expected leaf names are discovered")
    check(passed, failed, all(isinstance(lf, LeafNode) for lf in all_leaves),
          "all discovered children are the correct subclass")

    return len(passed), len(failed)
