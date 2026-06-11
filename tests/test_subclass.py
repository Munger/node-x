## @file test_subclass.py
##
## @brief Unit tests for Node subclass mechanics.
##
## Verifies the behaviours that are most important to subclass authors:
##
##   - **_reserved auto-population** — ``__init_subclass__`` must walk
##     the MRO and collect every public method, class attribute, and
##     annotated field name into ``_reserved`` so that ``__setitem__``
##     can block shadowing without manual maintenance.
##   - **Reserved-name blocking** — ``__setitem__`` must raise
##     ``KeyError`` with a message that names both the key and the
##     class when a method name is used as a payload key.
##   - **Property-backed reserved names** — a property defined on the
##     subclass *is* in ``_reserved``, but ``__setitem__`` must allow
##     it because the property getter reads from the dict payload.
##   - **Annotated fields** — class-level annotations (without default
##     values) must also be added to ``_reserved`` so they cannot be
##     shadowed in the payload.
##   - **_children tree iteration** — ``_tree_iter`` must walk
##     ``_children`` attributes that hold either a ``NodeList`` or a
##     single ``Node``.
##   - **_walk_child_nodes** — verifies the ``func`` and ``list_func``
##     callbacks are invoked correctly.
##   - **StreamMixin** — base class yields nothing; override yields
##     dynamic children.
##   - **NodeTransaction with subclasses** — subclass instances are
##     accepted without modification.
##   - **Node-typed _children** — single Node child (not in a NodeList)
##     must be descended into by ``_tree_iter``.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, List, Tuple

_PKG_DIR = str(Path(__file__).resolve().parent)
_ROOT_DIR = str(Path(__file__).resolve().parent.parent)
for _d in (_PKG_DIR, _ROOT_DIR):
    if _d not in sys.path:
        sys.path.insert(0, _d)

from node_x import Node, NodeList, NodeTransaction, StreamMixin

from _helpers import (
    check,
    check_catch,
    check_does_not_raise,
    heading,
)


def run() -> Tuple[int, int]:
    ## @brief Execute all subclass test sections and return pass/fail counts.
    ##
    ## Local subclasses are defined inline within each section to keep
    ## the fixture minimal and to avoid name collisions across sections.
    ##
    ## @return ``(pass_count, fail_count)`` across all sections.

    passed: List[str] = []
    failed: List[str] = []

    # ------------------------------------------------------------------
    heading("Subclass: _reserved populated automatically")
    # ------------------------------------------------------------------
    # __init_subclass__ calls _compute_reserved() which walks the MRO
    # and collects every public name.  Both methods and properties must
    # appear in _reserved so that __setitem__ can block them without
    # any manual list to maintain.

    class CustomNode(Node):
        def my_method(self) -> str:
            return "hello"

        @property
        def my_prop(self) -> str:
            return self.get("_cache", "default")

    check(passed, failed, "my_method" in CustomNode._reserved,
          "method name added to _reserved")
    check(passed, failed, "my_prop" in CustomNode._reserved,
          "property name added to _reserved")

    # ------------------------------------------------------------------
    heading("Subclass: reserved names blocked from __setitem__")
    # ------------------------------------------------------------------
    # A subclass method name used as a payload key must be rejected with
    # a KeyError whose message names the key, the class, and the word
    # "reserved" so the developer can diagnose the conflict immediately.

    cn = CustomNode()
    msg = check_catch(passed, failed,
                      "__setitem__ on reserved method name raises KeyError",
                      KeyError, lambda: cn.__setitem__("my_method", 42))
    check(passed, failed,
          "reserved" in msg and "my_method" in msg and "CustomNode" in msg,
          "reserved method message says 'reserved', names key and class")

    # ------------------------------------------------------------------
    heading("Subclass: property-backed reserved names accessible via __setitem__")
    # ------------------------------------------------------------------
    # A property is in _reserved (so __getattr__ dispatches to it), but
    # the property getter reads its value from the dict payload.
    # __setitem__ detects the property descriptor and allows the write
    # so that ``node["my_prop"] = value`` works correctly.

    check_does_not_raise(passed, failed,
        "set property-backed reserved name does not raise",
        lambda: cn.__setitem__("my_prop", 99))

    # Confirm the property getter sees the dict value just written.
    cn["_cache"] = "hello"
    check(passed, failed, cn.my_prop == "hello",
          "property getter reads value written via __setitem__")

    # ------------------------------------------------------------------
    heading("Subclass: property-backed reserved names accessible")
    # ------------------------------------------------------------------
    # Full round-trip with a property that has both getter and setter:
    # __setitem__ on the key must not raise, and reading back the key
    # must return the stored value.

    class PropNode(Node):
        @property
        def data(self) -> Any:
            return self.get("data", None)

        @data.setter
        def data(self, value: Any) -> None:
            self["data"] = value

    pn = PropNode()
    check_does_not_raise(passed, failed,
                         "__setitem__ on property-backed reserved name allowed",
                         lambda: pn.__setitem__("data", 42))
    check(passed, failed, pn["data"] == 42,
          "property-backed reserved key write succeeds")

    # ------------------------------------------------------------------
    heading("Subclass: _children tree iteration")
    # ------------------------------------------------------------------
    # _children is a list of payload field names whose values are
    # Node or NodeList instances.  _tree_iter() walks them in order,
    # yielding self first then descending into each child.

    class Branch(Node):
        _children = ("leaves",)

    class Leaf(Node):
        pass

    leaf_a = Leaf({"id": "A"})
    leaf_b = Leaf({"id": "B"})
    branch = Branch()
    branch.leaves = NodeList([leaf_a, leaf_b])

    descendants = list(branch._tree_iter())
    check(passed, failed, len(descendants) == 3,
          "_tree_iter walks subclass _children (branch + 2 leaves)")

    # ------------------------------------------------------------------
    heading("Subclass: _walk_child_nodes")
    # ------------------------------------------------------------------
    # _walk_child_nodes() applies a callable to every directly-reachable
    # Node value.  An optional list_func is invoked on each NodeList
    # container before iterating its elements.

    visited: List[str] = []

    def visitor(node: Node) -> None:
        visited.append(node.get("id", "?"))

    branch._walk_child_nodes(visitor)
    check(passed, failed, "A" in visited and "B" in visited,
          "_walk_child_nodes visits all children")

    list_visited: List[str] = []

    def list_visitor(nl: NodeList) -> None:
        list_visited.append(f"list(len={len(nl)})")

    visited2: List[str] = []
    branch._walk_child_nodes(
        lambda n: visited2.append(n.get("id", "?")),
        list_func=list_visitor,
    )
    check(passed, failed,
          len(list_visited) >= 1 and "len=2" in list_visited[0],
          "_walk_child_nodes invokes list_func on NodeList containers")

    # ------------------------------------------------------------------
    heading("Subclass: annotated fields reserved")
    # ------------------------------------------------------------------
    # Class-level type annotations (without a default value) are also
    # collected by _compute_reserved so they cannot be accidentally
    # used as payload keys.

    class TypedNode(Node):
        name: str
        count: int

    check(passed, failed, "name" in TypedNode._reserved,
          "annotated field name added to _reserved")
    check(passed, failed, "count" in TypedNode._reserved,
          "annotated field count added to _reserved")

    # ------------------------------------------------------------------
    heading("Subclass: StreamMixin override")
    # ------------------------------------------------------------------
    # The base StreamMixin.stream() is a no-op generator; subclasses
    # override it to yield dynamic children discovered from content.
    # Both the base and override behaviour are verified here.

    class StreamNode(StreamMixin, Node):
        def stream(self, data=None):
            yield Node({"child": 1})
            yield Node({"child": 2})

    sn = StreamNode({"parent": True})
    children = list(sn.stream())
    check(passed, failed, len(children) == 2,
          "StreamMixin stream() yields dynamic children")

    # The unoverridden base must yield nothing at all.
    base_stream = StreamMixin()
    check(passed, failed, list(base_stream.stream()) == [],
          "StreamMixin base stream() yields nothing")

    # ------------------------------------------------------------------
    heading("Subclass: NodeTransaction with subclasses")
    # ------------------------------------------------------------------
    # NodeTransaction only requires that each argument exposes a .lock
    # property, so any Node subclass is accepted without adaptation.

    a = CustomNode({"x": 1})
    b = CustomNode({"y": 2})
    with NodeTransaction(a, b):
        a["x"] = a["x"] + b["y"]
    check(passed, failed, a["x"] == 3,
          "NodeTransaction works with subclass instances")

    # ------------------------------------------------------------------
    heading("Subclass: _children with a single Node (not NodeList) child")
    # ------------------------------------------------------------------
    # _children may name a field whose value is a bare Node rather than
    # a NodeList.  _tree_iter() must take the isinstance(children, Node)
    # branch and recurse into it correctly.

    class ParentWithNodeChild(Node):
        _children = ("sub",)

    sub_node = Node({"v": 42})
    pwn = ParentWithNodeChild()
    pwn["sub"] = sub_node
    pwn_visited = list(pwn._tree_iter())
    check(passed, failed, len(pwn_visited) == 2 and pwn_visited[1] is sub_node,
          "_tree_iter descends into a Node-typed _children attribute")

    return len(passed), len(failed)
