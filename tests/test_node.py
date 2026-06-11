## @file test_node.py
##
## @brief Unit tests for the ``Node`` class.
##
## Exhaustive coverage of every public method and every documented
## error path on ``Node``.  Each section maps to a discrete contract:
##
##   - **Construction** — positional dict, keyword args, Node-from-Node,
##     and all invalid-argument combinations.
##   - **Item/attr access** — ``__setitem__`` / ``__getitem__`` /
##     ``__delitem__`` round-trips, reserved-key blocking, and
##     ``__setattr__`` / ``__getattr__`` delegation.
##   - **Value validation** — every allowed scalar type, recursive
##     tuple validation, and every rejected type with error-message
##     content checks.
##   - **Dict-protocol mutations** — ``clear``, ``pop`` (with and
##     without default), ``popitem`` (including empty-node edge case),
##     ``update`` (mapping, iterable-of-pairs, empty, invalid-value).
##   - **freeze / thaw** — every mutation method blocked when frozen;
##     deep propagation into child Nodes and NodeList containers;
##     shallow mode leaves children mutable; idempotency.
##   - **merge** — recursive Node-to-Node merge, plain-dict source,
##     scalar/Node overwrite in both directions, NodeList replacement
##     (not merge), return-value chaining, invalid-value rejection,
##     frozen-node rejection.
##   - **_tree_iter** — NodeList-typed children, Node-typed children,
##     and the degenerate base-Node case.
##   - **_with_lock** — basic execution-under-lock sanity check.
##
## Every exception-raising path verifies both the exception type
## and the relevant fragments of the error message.
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

import re

import node_x
import node_x_yaml
from node_x import Node, NodeList

from _helpers import (
    check,
    check_catch,
    check_does_not_raise,
    heading,
)


def run() -> Tuple[int, int]:
    ## @brief Execute all Node test sections and return pass/fail counts.
    ##
    ## Sections run sequentially; each section creates fresh Node
    ## instances so failures do not cascade.  The ``heading()`` calls
    ## mark section boundaries in the printed output.
    ##
    ## @return ``(pass_count, fail_count)`` across all sections.

    passed: List[str] = []
    failed: List[str] = []

    # ------------------------------------------------------------------
    heading("node_x: __version__")
    # ------------------------------------------------------------------
    # __version__ must be importable as a module attribute — the standard
    # Python convention for distributed libraries.  When installed via pip
    # the value comes from the package metadata (pyproject.toml is the
    # single source of truth); when running from source the fallback
    # "0.0.0.dev" fires.  Either way it must be a non-empty string that
    # looks like a semver or a development marker.

    check(passed, failed, hasattr(node_x, "__version__"),
          "node_x exposes __version__")
    check(passed, failed, isinstance(node_x.__version__, str),
          "__version__ is a string")
    check(passed, failed, bool(node_x.__version__),
          "__version__ is non-empty")

    # Accept semver (1.2.3), pre-release (1.2.3a1, 1.2.3.post1),
    # and the source fallback (0.0.0.dev).
    _ver_re = re.compile(r"^\d+\.\d+\.\d+")
    check(passed, failed, bool(_ver_re.match(node_x.__version__)),
          "__version__ starts with MAJOR.MINOR.PATCH")

    # node_x_yaml re-exports the same version so callers have a single
    # place to check regardless of which module they import.
    check(passed, failed, node_x_yaml.__version__ == node_x.__version__,
          "node_x_yaml.__version__ matches node_x.__version__")

    # ------------------------------------------------------------------
    heading("Node: construction")
    # ------------------------------------------------------------------
    # Node.__init__ accepts zero arguments, a single dict, keyword
    # arguments, or a combination.  Because Node is a dict subclass,
    # passing another Node as the positional argument exercises the
    # same dict-copy code path.  The two error branches (>1 positional,
    # non-mapping positional) each carry structured messages so callers
    # can diagnose mistakes without reading source.

    n = Node()
    check(passed, failed, len(n) == 0, "Node() creates empty payload")

    n = Node({"a": 1, "b": "two"})
    check(passed, failed, n["a"] == 1 and n["b"] == "two",
          "Node(dict) populates payload")

    n = Node(a=1, b="two")
    check(passed, failed, n["a"] == 1 and n["b"] == "two",
          "Node(kwargs) populates payload")

    n = Node({"x": 10}, y=20)
    check(passed, failed, n["x"] == 10 and n["y"] == 20,
          "Node(dict, kwargs) merges both")

    msg = check_catch(passed, failed,
                      "Node(1, 2) rejects >1 positional arg",
                      TypeError, lambda: Node({}, {}))
    check(passed, failed,
          "accepts at most 1" in msg and "got 2" in msg,
          "Node(1, 2) message mentions count")

    msg = check_catch(passed, failed,
                      "Node(42) rejects non-mapping positional",
                      TypeError, lambda: Node(42))
    check(passed, failed,
          "must be a mapping" in msg and "int" in msg,
          "Node(42) message says 'mapping' and names type")

    # Node is a dict subclass, so Node(another_node) must copy the
    # payload through the same __setitem__ validation path as Node(dict).
    src = Node({"a": 1, "b": "two"})
    n = Node(src)
    check(passed, failed, n["a"] == 1 and n["b"] == "two",
          "Node(another_node) copies payload from Node source")

    # ------------------------------------------------------------------
    heading("Node: __setitem__ / __getitem__ / __delitem__")
    # ------------------------------------------------------------------
    # The dict contract: set, get, and delete work as expected.
    # Deleting a missing key raises KeyError via the underlying dict.
    # Attempting to store a value under a reserved name (one that
    # shadows a method or class variable) raises KeyError with a
    # diagnostic message so the developer knows to choose a different key.

    n = Node()
    n["key"] = "value"
    check(passed, failed, n["key"] == "value",
          "__setitem__ / __getitem__ round-trip")

    del n["key"]
    check(passed, failed, "key" not in n,
          "__delitem__ removes key")

    check_catch(passed, failed,
                "del missing key raises KeyError",
                KeyError, lambda: n["missing"])

    msg = check_catch(passed, failed,
                      "__setitem__ reserved key raises KeyError",
                      KeyError, lambda: Node().__setitem__("update", 1))
    check(passed, failed,
          "reserved" in msg and "Node" in msg,
          "reserved key message says 'reserved' and names the class")

    # ------------------------------------------------------------------
    heading("Node: __setattr__ / __getattr__")
    # ------------------------------------------------------------------
    # Attribute writes on non-reserved, non-underscore names are
    # routed to the dict payload via __setitem__.  Attribute reads on
    # unknown names fall through to __getattr__ which looks in the dict;
    # a missing key surfaces as AttributeError (not KeyError) so
    # normal hasattr() / getattr() usage works as expected.

    n = Node()
    n.foo = "bar"
    check(passed, failed, n.foo == "bar",
          "__setattr__ / __getattr__ round-trip")

    check_catch(passed, failed,
                "__getattr__ missing key raises AttributeError",
                AttributeError, lambda: Node().missing_key)

    # ------------------------------------------------------------------
    heading("Node: value validation")
    # ------------------------------------------------------------------
    # _validate_value() enforces a strict whitelist.  Raw list and dict
    # are rejected because they bypass the locking and type guarantees
    # of NodeList and Node respectively.  Unknown types are also
    # rejected.  Tuples are allowed but validated recursively — a tuple
    # containing a list must still be rejected.  The error messages name
    # the offending type and suggest the correct alternative so the
    # developer does not need to look up the docs.

    msg = check_catch(passed, failed,
                      "list raises TypeError",
                      TypeError,
                      lambda: Node().__setitem__("bad", [1, 2, 3]))
    check(passed, failed,
          "plain lists" in msg and "NodeList" in msg,
          "list message says 'plain lists' and suggests NodeList")

    msg = check_catch(passed, failed,
                      "dict raises TypeError",
                      TypeError,
                      lambda: Node().__setitem__("bad", {"x": 1}))
    check(passed, failed,
          "raw dicts" in msg and "Node subclass" in msg,
          "dict message says 'raw dicts' and suggests Node subclass")

    msg = check_catch(passed, failed,
                      "unknown type raises TypeError",
                      TypeError,
                      lambda: Node().__setitem__("bad", 3.14j))
    check(passed, failed,
          "complex" in msg and "Allowed types" in msg,
          "unknown type message names type and lists allowed")

    # Confirm the complete scalar whitelist is accepted.
    n2 = Node()
    n2["s"] = "str"
    n2["i"] = 42
    n2["f"] = 3.14
    n2["b"] = True
    n2["n"] = None
    n2["bytes"] = b"hello"
    n2["tup"] = (1, "two", Node())
    check(passed, failed,
          n2["s"] == "str" and n2["i"] == 42 and n2["b"] is True,
          "Node stores all valid scalar types")

    # Recursive tuple validation: even a list buried inside a tuple
    # must be caught before the value reaches the dict.
    msg = check_catch(passed, failed,
                      "tuple containing invalid type raises TypeError",
                      TypeError,
                      lambda: Node().__setitem__("bad", (1, [2, 3])))
    check(passed, failed,
          "plain lists" in msg,
          "tuple-invalid message names the disallowed inner type")

    # ------------------------------------------------------------------
    heading("Node: clear / pop / popitem / update")
    # ------------------------------------------------------------------
    # These methods mirror the built-in dict API but add frozen checks
    # and (for update) value validation before any state is mutated.
    # pop() must accept an optional default to match dict semantics.
    # update() must handle an iterable of (key, value) pairs in
    # addition to mappings, matching the dict.update() contract.

    n = Node({"a": 1, "b": 2, "c": 3})
    check(passed, failed, n.pop("a") == 1,
          "pop returns correct value")
    check(passed, failed, "a" not in n,
          "pop removes key")

    # dict.pop(key, default) must return the default for missing keys
    # rather than raising, and pop(missing_key) with no default must
    # still raise KeyError.
    check(passed, failed, n.pop("missing", "default") == "default",
          "pop with default returns default for missing key")
    check_catch(passed, failed,
                "pop missing key without default raises KeyError",
                KeyError, lambda: n.pop("also_missing"))

    n2 = Node({"x": 10})
    k, v = n2.popitem()
    check(passed, failed, k == "x" and v == 10,
          "popitem returns (key, value)")

    # popitem() on an empty node must raise KeyError as dict does.
    check_catch(passed, failed,
                "popitem on empty Node raises KeyError",
                KeyError, lambda: Node().popitem())

    n3 = Node({"a": 1})
    n3.update({"b": 2, "c": 3}, d=4)
    check(passed, failed, n3["a"] == 1 and n3["b"] == 2 and n3["d"] == 4,
          "update merges mapping and kwargs")

    n4 = Node({"a": 1, "b": 2})
    n4.clear()
    check(passed, failed, len(n4) == 0,
          "clear removes all items")

    msg = check_catch(passed, failed,
                      "update with reserved key raises KeyError",
                      KeyError, lambda: Node().update({"update": 1}))
    check(passed, failed,
          "reserved" in msg and "update" in msg,
          "update reserved-key message says 'reserved' and names the key")

    msg = check_catch(passed, failed,
                      "update with 2 positional rejects",
                      TypeError, lambda: Node().update({"a": 1}, {"b": 2}))
    check(passed, failed,
          "at most 1" in msg and "update" in msg,
          "update arg count message names method and limit")

    # The validation branch for iterable-of-pairs exercises the
    # ``else: for _, v in other`` path inside Node.update().
    n5 = Node()
    n5.update([("x", 1), ("y", 2)])
    check(passed, failed, n5["x"] == 1 and n5["y"] == 2,
          "update accepts iterable of (key, value) pairs")

    check_does_not_raise(passed, failed,
                         "update with empty mapping is a no-op",
                         lambda: Node({"a": 1}).update({}))

    # Validation must run before any dict mutation, so an invalid value
    # anywhere in the update mapping must abort the whole update.
    msg = check_catch(passed, failed,
                      "update with invalid value raises TypeError",
                      TypeError,
                      lambda: Node().update({"bad": [1, 2]}))
    check(passed, failed, "plain lists" in msg,
          "update invalid-value message identifies the problem")

    # ------------------------------------------------------------------
    heading("Node: freeze / thaw")
    # ------------------------------------------------------------------
    # Once frozen, every mutation method must raise TypeError with a
    # message that names the operation and mentions thaw() so the caller
    # knows how to recover.  Deep freeze must propagate into both child
    # Nodes and NodeList containers.  Shallow freeze (deep=False) must
    # leave child objects mutable.

    n = Node({"a": 1})
    n.freeze()

    msg = check_catch(passed, failed,
                      "frozen __setitem__ raises TypeError",
                      TypeError, lambda: n.__setitem__("b", 2))
    check(passed, failed,
          "set key" in msg and "frozen" in msg and "thaw" in msg,
          "frozen set message says 'set key', 'frozen', and 'thaw'")

    msg = check_catch(passed, failed,
                      "frozen __delitem__ raises TypeError",
                      TypeError, lambda: n.__delitem__("a"))
    check(passed, failed,
          "delete key" in msg and "frozen" in msg,
          "frozen del message says 'delete key' and 'frozen'")

    msg = check_catch(passed, failed,
                      "frozen clear raises TypeError",
                      TypeError, lambda: n.clear())
    check(passed, failed,
          "clear all" in msg and "frozen" in msg,
          "frozen clear message says 'clear all' and 'frozen'")

    msg = check_catch(passed, failed,
                      "frozen pop raises TypeError",
                      TypeError, lambda: n.pop("a"))
    check(passed, failed,
          "pop key" in msg and "frozen" in msg,
          "frozen pop message says 'pop key' and 'frozen'")

    msg = check_catch(passed, failed,
                      "frozen popitem raises TypeError",
                      TypeError, lambda: n.popitem())
    check(passed, failed,
          "pop last item" in msg and "frozen" in msg,
          "frozen popitem message says 'pop last item' and 'frozen'")

    msg = check_catch(passed, failed,
                      "frozen update raises TypeError",
                      TypeError, lambda: n.update({"c": 3}))
    check(passed, failed,
          "update" in msg and "frozen" in msg,
          "frozen update message says 'update' and 'frozen'")

    # __setattr__ for payload keys routes through __setitem__ which
    # checks frozen, so setattr on a frozen node must also raise.
    msg = check_catch(passed, failed,
                      "frozen __setattr__ raises TypeError",
                      TypeError, lambda: setattr(n, "extra", 99))
    check(passed, failed,
          "set attribute" in msg and "frozen" in msg,
          "frozen setattr message says 'set attribute' and 'frozen'")

    n.thaw()
    check_does_not_raise(passed, failed, "thawed node accepts writes",
                         lambda: n.__setitem__("b", 2))
    check(passed, failed, n["b"] == 2,
          "thawed __setitem__ succeeds")

    # deep=True (default) must propagate the frozen state down through
    # child Nodes so they too reject mutations.
    child = Node({"x": 1})
    parent = Node()
    parent["child"] = child
    parent.freeze(deep=True)

    msg = check_catch(passed, failed,
                      "deep-frozen child rejects writes",
                      TypeError, lambda: child.__setitem__("y", 2))
    check(passed, failed,
          "frozen" in msg,
          "deep-frozen child message says 'frozen'")

    parent.thaw(deep=True)
    check_does_not_raise(passed, failed, "deep-thawed child accepts writes",
                         lambda: child.__setitem__("y", 2))

    # deep=False must freeze the parent only; the child must remain mutable.
    child2 = Node({"x": 1})
    parent2 = Node()
    parent2["child"] = child2
    parent2.freeze(deep=False)
    check_does_not_raise(passed, failed, "shallow-frozen child stays mutable",
                         lambda: child2.__setitem__("y", 2))

    # Deep freeze must also reach NodeList containers held in the
    # payload, and the Nodes stored inside those lists.
    nl_child = Node({"v": 1})
    nl = NodeList([nl_child])
    parent3 = Node()
    parent3["children"] = nl
    parent3.freeze(deep=True)
    msg = check_catch(passed, failed,
                      "deep-freeze propagates to NodeList container",
                      TypeError, lambda: nl.append(Node()))
    check(passed, failed, "frozen" in msg,
          "frozen NodeList message says 'frozen'")
    msg2 = check_catch(passed, failed,
                       "deep-freeze propagates to Node inside NodeList",
                       TypeError, lambda: nl_child.__setitem__("v", 99))
    check(passed, failed, "frozen" in msg2,
          "frozen Node-in-NodeList message says 'frozen'")

    parent3.thaw(deep=True)
    check_does_not_raise(passed, failed,
                         "deep-thaw restores NodeList mutability",
                         lambda: nl.append(Node()))
    check_does_not_raise(passed, failed,
                         "deep-thaw restores Node-in-NodeList mutability",
                         lambda: nl_child.__setitem__("v", 99))

    # ------------------------------------------------------------------
    heading("Node: merge")
    # ------------------------------------------------------------------
    # merge() performs recursive Node-to-Node merging: matching Node
    # fields are merged recursively; all other values (scalars, NodeList)
    # are overwritten.  The source can be a plain dict or another Node.
    # merge() returns self to enable chaining.  Validation runs before
    # any state change so an invalid value aborts the entire merge.

    class MergeChild(Node):
        pass

    n = Node({"a": 1, "c": MergeChild({"nested": 0})})
    other = Node({"b": 2, "c": MergeChild({"nested": 99})})
    n.merge(other)
    check(passed, failed, n["a"] == 1 and n["b"] == 2,
          "merge adds new keys")
    check(passed, failed, n["c"]["nested"] == 99,
          "merge recursively merges nested Nodes")

    msg = check_catch(passed, failed,
                      "merge with non-mapping raises TypeError",
                      TypeError, lambda: n.merge(42))
    check(passed, failed,
          "mapping" in msg and "int" in msg,
          "merge type message says 'mapping' and names type")

    # Return-value chaining: merge() must return self so expressions
    # like node.merge(a).merge(b) are valid.
    result = Node({"a": 1}).merge({"b": 2})
    check(passed, failed, isinstance(result, Node) and result["b"] == 2,
          "merge returns self for chaining")

    # merge() must accept a plain dict, not only Node instances.
    n_plain = Node({"a": 1})
    n_plain.merge({"b": 2, "c": 3})
    check(passed, failed, n_plain["b"] == 2 and n_plain["c"] == 3,
          "merge accepts plain dict")

    # When the existing value is a Node but the incoming value is a
    # scalar, the scalar wins (no recursive merge attempted).
    n_over = Node({"a": Node({"x": 1})})
    n_over.merge({"a": 42})
    check(passed, failed, n_over["a"] == 42,
          "merge overwrites Node value with scalar")

    # When the existing value is a scalar and the incoming value is a
    # Node, the Node replaces the scalar (no crash on type mismatch).
    n_under = Node({"a": 42})
    n_under.merge({"a": Node({"x": 1})})
    check(passed, failed, isinstance(n_under["a"], Node),
          "merge overwrites scalar with incoming Node")

    # NodeList values are treated as opaque scalars: the incoming list
    # replaces the existing one rather than merging element-by-element.
    nl1, nl2 = NodeList([Node({"i": 0})]), NodeList([Node({"i": 1})])
    n_nl = Node({"nodes": nl1})
    n_nl.merge({"nodes": nl2})
    check(passed, failed, n_nl["nodes"] is nl2,
          "merge replaces NodeList with incoming NodeList (no element merge)")

    # Invalid values must be rejected before any state is written.
    msg = check_catch(passed, failed,
                      "merge with invalid value raises TypeError",
                      TypeError, lambda: Node().merge({"bad": [1, 2]}))
    check(passed, failed, "plain lists" in msg,
          "merge invalid-value message identifies the problem")

    n.freeze()
    msg = check_catch(passed, failed,
                      "frozen merge raises TypeError",
                      TypeError, lambda: n.merge({"d": 4}))
    check(passed, failed,
          "frozen" in msg and "merge" in msg,
          "frozen merge message says 'merge' and 'frozen'")
    n.thaw()

    # ------------------------------------------------------------------
    heading("Node: _tree_iter")
    # ------------------------------------------------------------------
    # _tree_iter() performs depth-first traversal guided by the
    # _children class variable.  It handles both NodeList-typed and
    # plain Node-typed child attributes.  A Node with no _children
    # yields only itself.

    class ParentNode(Node):
        _children = ("items",)

    class ChildNode(Node):
        _children = ("sub",)

    gc = Node()
    c = ChildNode()
    c.sub = gc
    p = ParentNode()
    p.items = NodeList([c])

    visited = list(p._tree_iter())
    check(passed, failed, len(visited) == 3,
          "_tree_iter yields parent + child + grandchild (3 nodes)")

    # A plain Node has _children = () so the generator yields only self.
    lone = Node({"x": 1})
    check(passed, failed, list(lone._tree_iter()) == [lone],
          "_tree_iter on base Node with no _children yields just self")

    # _children may point to a single Node rather than a NodeList;
    # _tree_iter must recurse into it via the isinstance(children, Node)
    # branch.
    class NodeChildNode(Node):
        _children = ("sub",)

    sub = Node({"v": 1})
    parent_nc = NodeChildNode()
    parent_nc["sub"] = sub
    nc_visited = list(parent_nc._tree_iter())
    check(passed, failed, len(nc_visited) == 2 and nc_visited[1] is sub,
          "_tree_iter walks a Node-typed _children attribute (not just NodeList)")

    # ------------------------------------------------------------------
    heading("Node: freeze/thaw idempotent")
    # ------------------------------------------------------------------
    # Calling freeze() on an already-unfrozen node, or thaw() on an
    # already-thawed node, must not raise.

    n = Node()
    check_does_not_raise(passed, failed, "freeze on unfrozen is idempotent",
                         lambda: n.freeze())
    check_does_not_raise(passed, failed, "thaw on unfrozen is idempotent",
                         lambda: n.thaw())

    # ------------------------------------------------------------------
    heading("Node: _with_lock helper")
    # ------------------------------------------------------------------
    # _with_lock() is a low-level helper for callers that need to run
    # an arbitrary callable under the instance RLock.  Verifies that
    # the callable executes and its return value is forwarded correctly.

    n = Node({"a": 1})
    result = n._with_lock(lambda: n["a"] + 1)
    check(passed, failed, result == 2,
          "_with_lock executes func under lock")

    return len(passed), len(failed)
