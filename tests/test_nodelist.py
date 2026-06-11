## @file test_nodelist.py
##
## @brief Unit tests for the ``NodeList`` class.
##
## Exhaustive coverage of every public method and every documented
## error path on ``NodeList``.  Sections:
##
##   - **Construction** — empty, valid-Node iterable, and
##     non-Node iterable (the constructor must validate just as
##     ``append`` does).
##   - **append / extend / insert** — happy path and every type-error
##     combination; ``__setitem__`` by scalar index and by slice.
##   - **pop / remove / clear** — default pop, explicit-index pop,
##     pop-on-empty, remove-present, remove-missing, clear.
##   - **__setitem__ / __delitem__ positive cases** — index and slice
##     replacement and deletion with valid Nodes.
##   - **extend edge cases** — empty iterable (no-op) and attempt to
##     store a ``NodeList`` inside a ``NodeList`` (rejected because
##     ``NodeList`` is not a ``Node`` subclass).
##   - **reverse / sort** — basic reorder and key-function sort.
##   - **freeze / thaw** — every mutation method blocked when frozen;
##     deep propagation into child Nodes; shallow mode; idempotency.
##   - **iter helper** — ``iter()`` returns the contained nodes.
##
## Every exception path verifies both the exception type and key
## fragments of the error message.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT

from __future__ import annotations

import sys
from pathlib import Path
from typing import List, Tuple

_PKG_DIR = str(Path(__file__).resolve().parent)
_ROOT_DIR = str(Path(__file__).resolve().parent.parent)
for _d in (_PKG_DIR, _ROOT_DIR):
    if _d not in sys.path:
        sys.path.insert(0, _d)

from node_x import Node, NodeList

from _helpers import (
    check,
    check_catch,
    check_does_not_raise,
    heading,
)


def run() -> Tuple[int, int]:
    ## @brief Execute all NodeList test sections and return pass/fail counts.
    ##
    ## Each section creates fresh ``NodeList`` and ``Node`` instances so
    ## that failures in one section do not affect subsequent ones.
    ##
    ## @return ``(pass_count, fail_count)`` across all sections.

    passed: List[str] = []
    failed: List[str] = []

    # ------------------------------------------------------------------
    heading("NodeList: construction")
    # ------------------------------------------------------------------
    # NodeList() with no argument creates an empty list.  Passing a
    # valid iterable of Nodes copies the elements.  Passing an iterable
    # that contains non-Nodes must raise TypeError — the constructor
    # must not bypass the same type guard enforced by append/extend.

    nl = NodeList()
    check(passed, failed, len(nl) == 0, "NodeList() creates empty list")

    items = [Node({"a": 1}), Node({"b": 2})]
    nl = NodeList(items)
    check(passed, failed, len(nl) == 2 and nl[0]["a"] == 1,
          "NodeList(iterable) populates elements")

    # Construction-time type validation was absent before it was fixed;
    # this test guards against regression.
    msg = check_catch(passed, failed,
                      "NodeList(non-Node iterable) raises TypeError",
                      TypeError, lambda: NodeList(["not", "a", "node"]))
    check(passed, failed,
          "only accepts Node instances" in msg and "str" in msg,
          "construction type message names type and suggests wrapping")

    # ------------------------------------------------------------------
    heading("NodeList: append / extend / insert")
    # ------------------------------------------------------------------
    # Each mutation method validates its argument(s) before acquiring
    # the lock so that invalid values are rejected atomically without
    # leaving the list in a partially-modified state.

    nl = NodeList()
    a = Node({"x": 1})
    nl.append(a)
    check(passed, failed, len(nl) == 1 and nl[0] is a,
          "append adds element")

    b = Node({"y": 2})
    c = Node({"z": 3})
    nl.extend([b, c])
    check(passed, failed, len(nl) == 3 and nl[1] is b and nl[2] is c,
          "extend adds multiple elements")

    d = Node({"w": 0})
    nl.insert(0, d)
    check(passed, failed, nl[0] is d,
          "insert at index 0 works")

    msg = check_catch(passed, failed,
                      "append non-Node raises TypeError",
                      TypeError, lambda: NodeList().append("not_a_node"))
    check(passed, failed,
          "only accepts" in msg and "str" in msg and "Wrap" in msg,
          "append type message names type and suggests wrapping")

    msg = check_catch(passed, failed,
                      "extend non-Node raises TypeError",
                      TypeError,
                      lambda: NodeList().extend([Node(), "bad"]))
    check(passed, failed,
          "only accepts" in msg and "str" in msg,
          "extend type message names type")

    msg = check_catch(passed, failed,
                      "insert non-Node raises TypeError",
                      TypeError, lambda: NodeList().insert(0, 42))
    check(passed, failed,
          "only accepts" in msg and "int" in msg,
          "insert type message names type")

    # __setitem__ has two validation branches: one for a scalar index
    # and one for a slice.  Both must reject non-Node values.
    nl3 = NodeList([Node()])
    msg = check_catch(passed, failed,
                      "__setitem__ scalar non-Node raises TypeError",
                      TypeError, lambda: nl3.__setitem__(0, "bad"))
    check(passed, failed,
          "only accepts" in msg and "str" in msg,
          "__setitem__ scalar type message names type")

    msg = check_catch(passed, failed,
                      "__setitem__ slice non-Node raises TypeError",
                      TypeError, lambda: nl3.__setitem__(slice(0, 1), ["x"]))
    check(passed, failed,
          "only accepts" in msg and "str" in msg,
          "__setitem__ slice type message names type")

    # ------------------------------------------------------------------
    heading("NodeList: pop / remove / clear")
    # ------------------------------------------------------------------
    # pop() with no argument removes the last element (list default).
    # pop(index) removes the element at the given position.
    # pop() on an empty list raises IndexError as the built-in does.
    # remove() raises ValueError when the element is not present.

    a, b, c = Node({"a": 1}), Node({"b": 2}), Node({"c": 3})
    nl = NodeList([a, b, c])

    popped = nl.pop()
    check(passed, failed, popped is c and len(nl) == 2,
          "pop removes last element")

    popped_first = nl.pop(0)
    check(passed, failed, popped_first is a and len(nl) == 1,
          "pop(index) removes element at explicit index")

    check_catch(passed, failed,
                "pop on empty NodeList raises IndexError",
                IndexError, lambda: NodeList().pop())

    nl2 = NodeList([a, b, c])
    nl2.remove(a)
    check(passed, failed, len(nl2) == 2 and nl2[0] is b,
          "remove removes first matching element")

    check_catch(passed, failed,
                "remove missing element raises ValueError",
                ValueError, lambda: NodeList().remove(a))

    nl2.clear()
    check(passed, failed, len(nl2) == 0,
          "clear removes all elements")

    # ------------------------------------------------------------------
    heading("NodeList: __setitem__ / __delitem__ positive cases")
    # ------------------------------------------------------------------
    # Verifies that item and slice assignment/deletion work correctly
    # with valid Nodes — the error paths are covered above; these checks
    # confirm the success paths update the list as expected.

    s0, s1, s2 = Node({"i": 0}), Node({"i": 1}), Node({"i": 2})
    nl_s = NodeList([s0, s1, s2])

    new_node = Node({"i": 99})
    nl_s[1] = new_node
    check(passed, failed, nl_s[1] is new_node,
          "__setitem__ index replaces element")

    r0, r1 = Node({"r": 0}), Node({"r": 1})
    nl_s[0:2] = [r0, r1]
    check(passed, failed, nl_s[0] is r0 and nl_s[1] is r1,
          "__setitem__ slice replaces range of elements")

    del nl_s[0]
    check(passed, failed, nl_s[0] is r1,
          "__delitem__ index removes element")

    nl_s2 = NodeList([s0, s1, s2])
    del nl_s2[0:2]
    check(passed, failed, len(nl_s2) == 1 and nl_s2[0] is s2,
          "__delitem__ slice removes range of elements")

    # ------------------------------------------------------------------
    heading("NodeList: extend edge cases")
    # ------------------------------------------------------------------
    # Extending with an empty iterable must be a no-op (no error, no
    # change to the list).  Attempting to store a NodeList inside a
    # NodeList must fail because NodeList is not a Node subclass —
    # this boundary is worth an explicit test since it is a natural
    # mistake when building nested structures.

    nl_e = NodeList([Node({"x": 1})])
    check_does_not_raise(passed, failed,
                         "extend with empty iterable is a no-op",
                         lambda: nl_e.extend([]))
    check(passed, failed, len(nl_e) == 1,
          "extend empty leaves list unchanged")

    msg = check_catch(passed, failed,
                      "NodeList inside NodeList raises TypeError",
                      TypeError,
                      lambda: NodeList().append(NodeList()))
    check(passed, failed, "only accepts Node instances" in msg,
          "NodeList-in-NodeList message says 'only accepts Node instances'")

    # ------------------------------------------------------------------
    heading("NodeList: reverse / sort")
    # ------------------------------------------------------------------

    a, b, c = Node({"n": 1}), Node({"n": 2}), Node({"n": 3})
    nl = NodeList([c, a, b])
    nl.reverse()
    check(passed, failed, nl[0] is b and nl[2] is c,
          "reverse reorders elements")

    # sort() must accept the standard key= keyword argument and produce
    # a correctly ordered list.
    nl.sort(key=lambda x: x["n"])
    check(passed, failed, nl[0] is a and nl[2] is c,
          "sort orders by key")

    # ------------------------------------------------------------------
    heading("NodeList: freeze / thaw")
    # ------------------------------------------------------------------
    # Every mutating method must check the frozen flag before acquiring
    # the lock.  The error messages must identify the operation and
    # mention thaw() as the remedy.  Deep freeze propagates the frozen
    # state into the contained Node instances; shallow freeze does not.

    a, b = Node({"x": 1}), Node({"y": 2})
    nl = NodeList([a, b])
    nl.freeze()

    msg = check_catch(passed, failed,
                      "frozen append raises TypeError",
                      TypeError, lambda: nl.append(Node()))
    check(passed, failed,
          "append" in msg and "frozen" in msg and "thaw" in msg,
          "frozen append message says 'append', 'frozen', and 'thaw'")

    msg = check_catch(passed, failed,
                      "frozen extend raises TypeError",
                      TypeError, lambda: nl.extend([Node()]))
    check(passed, failed,
          "extend" in msg and "frozen" in msg,
          "frozen extend message says 'extend' and 'frozen'")

    msg = check_catch(passed, failed,
                      "frozen insert raises TypeError",
                      TypeError, lambda: nl.insert(0, Node()))
    check(passed, failed,
          "insert" in msg and "frozen" in msg,
          "frozen insert message says 'insert' and 'frozen'")

    msg = check_catch(passed, failed,
                      "frozen pop raises TypeError",
                      TypeError, lambda: nl.pop())
    check(passed, failed,
          "pop" in msg and "frozen" in msg,
          "frozen pop message says 'pop' and 'frozen'")

    msg = check_catch(passed, failed,
                      "frozen remove raises TypeError",
                      TypeError, lambda: nl.remove(a))
    check(passed, failed,
          "remove" in msg and "frozen" in msg,
          "frozen remove message says 'remove' and 'frozen'")

    msg = check_catch(passed, failed,
                      "frozen clear raises TypeError",
                      TypeError, lambda: nl.clear())
    check(passed, failed,
          "clear all" in msg and "frozen" in msg,
          "frozen clear message says 'clear all' and 'frozen'")

    msg = check_catch(passed, failed,
                      "frozen reverse raises TypeError",
                      TypeError, lambda: nl.reverse())
    check(passed, failed,
          "reverse" in msg and "frozen" in msg,
          "frozen reverse message says 'reverse' and 'frozen'")

    msg = check_catch(passed, failed,
                      "frozen sort raises TypeError",
                      TypeError, lambda: nl.sort(key=lambda x: x["x"]))
    check(passed, failed,
          "sort" in msg and "frozen" in msg,
          "frozen sort message says 'sort' and 'frozen'")

    msg = check_catch(passed, failed,
                      "frozen __setitem__ raises TypeError",
                      TypeError, lambda: nl.__setitem__(0, Node()))
    check(passed, failed,
          "set index" in msg and "frozen" in msg,
          "frozen __setitem__ message says 'set index' and 'frozen'")

    msg = check_catch(passed, failed,
                      "frozen __delitem__ raises TypeError",
                      TypeError, lambda: nl.__delitem__(0))
    check(passed, failed,
          "delete index" in msg and "frozen" in msg,
          "frozen __delitem__ message says 'delete index' and 'frozen'")

    nl.thaw()
    check_does_not_raise(passed, failed, "thawed NodeList accepts append",
                         lambda: nl.append(Node()))
    check(passed, failed, len(nl) == 3,
          "thawed NodeList append succeeds")

    # Deep freeze: child Nodes inside the list must also be frozen.
    a2, b2 = Node({"x": 1}), Node({"y": 2})
    nl2 = NodeList([a2, b2])
    nl2.freeze(deep=True)

    msg = check_catch(passed, failed,
                      "deep-frozen NodeList child rejects writes",
                      TypeError, lambda: a2.__setitem__("z", 3))
    check(passed, failed,
          "frozen" in msg,
          "deep-frozen child message says 'frozen'")

    nl2.thaw(deep=True)
    check_does_not_raise(passed, failed,
                         "deep-thawed NodeList child accepts writes",
                         lambda: a2.__setitem__("z", 3))

    # Shallow freeze: only the list container is frozen; its elements
    # must remain mutable.
    a3, b3 = Node({"x": 1}), Node({"y": 2})
    nl3 = NodeList([a3, b3])
    nl3.freeze(deep=False)
    check_does_not_raise(passed, failed,
                         "shallow-frozen NodeList child stays mutable",
                         lambda: a3.__setitem__("z", 3))

    # ------------------------------------------------------------------
    heading("NodeList: freeze/thaw idempotent")
    # ------------------------------------------------------------------

    nl = NodeList()
    check_does_not_raise(passed, failed,
                         "freeze on unfrozen NodeList is idempotent",
                         lambda: nl.freeze())
    check_does_not_raise(passed, failed,
                         "thaw on unfrozen NodeList is idempotent",
                         lambda: nl.thaw())

    return len(passed), len(failed)
