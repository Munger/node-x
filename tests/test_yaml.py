## @file test_yaml.py
##
## @brief Unit tests for ``node_x_yaml`` — YAML serialisation companion.
##
## Sections:
##
##   - **dump** — block vs inline output, unicode pass-through,
##     key insertion-order preservation.
##   - **load** — basic round-trip, nested nodes, non-ASCII strings.
##   - **full round-trip: simple** — dump → load cycle on a flat node and
##     a two-level tree; verifies type and field fidelity.
##   - **full round-trip: graph $ref** — dump → load cycle on a graph
##     containing a shared keyed (``Graph``) node; verifies that both
##     references restore to the *same* Python object (true graph identity).
##   - **full round-trip: SerialisableList** — list containing a
##     shared node deduplicates to ``$ref`` in YAML and restores identity.
##   - **unicode** — non-ASCII artist names survive dump → load without
##     escape sequences.
##   - **programmer errors** — ``load()`` with invalid YAML raises
##     ``yaml.YAMLError``; ``dump()`` on an object without ``to_plain()``
##     raises ``AttributeError``; ``load()`` with an orphaned ``$ref``
##     raises ``KeyError`` with the missing key in the message.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, List, Tuple

_PKG_DIR  = str(Path(__file__).resolve().parent)
_ROOT_DIR = str(Path(__file__).resolve().parent.parent)
for _d in (_PKG_DIR, _ROOT_DIR):
    if _d not in sys.path:
        sys.path.insert(0, _d)

import yaml as _yaml

from node_x import (
    Node,
    Serialisable,
    SerialisableList,
)
import node_x.node_x_yaml as node_x_yaml

from _helpers import (
    check,
    catch_into,
    heading,
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

class SimpleNode(Serialisable, Node):
    ## @brief Flat serialisable node; no declared child fields.
    _restore_via_payload = True


class ChildNode(Serialisable, Node):
    ## @brief Leaf node used inside tree fixtures.
    _restore_via_payload = True


class TreeNode(Serialisable, Node):
    ## @brief Two-level tree: one scalar child Node and one NodeList.
    _restore_via_payload = True
    node_fields  = {"leaf": ChildNode}
    list_fields  = {"leaves": (SerialisableList, ChildNode)}


class GNode(Serialisable, Node):
    ## @brief Keyed node — carries ``_key`` for $ref deduplication.
    _restore_via_payload = True


class Container(Serialisable, Node):
    ## @brief Holds two named references to the same GNode.
    _restore_via_payload = True
    node_fields = {"ref_a": GNode, "ref_b": GNode}


class ListContainer(Serialisable, Node):
    ## @brief Holds a NodeList that may contain duplicate GNode entries.
    _restore_via_payload = True
    list_fields = {"entries": (SerialisableList, GNode)}


# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------

def run() -> Tuple[int, int]:
    ## @brief Execute all node_x_yaml test sections and return pass/fail counts.
    ##
    ## Each section that uses Graph calls ``clear_registry()`` in a
    ## try/finally block so that registry state never leaks between sections.
    ##
    ## @return ``(pass_count, fail_count)`` across all sections.

    passed: List[str] = []
    failed: List[str] = []

    # ------------------------------------------------------------------
    heading("node_x_yaml: dump — output format")
    # ------------------------------------------------------------------
    # dump() wraps to_plain() → yaml.dump().  Block style (default) produces
    # one key per line; inline style produces a compact single-line dict.
    # Key order must match insertion order so diffs remain stable.

    n = SimpleNode({"alpha": 1, "beta": "two", "gamma": True})
    block = node_x_yaml.dump(n)

    check(passed, failed, isinstance(block, str),
          "dump returns a string")
    check(passed, failed, block.endswith("\n"),
          "dump output ends with a newline (YAML document convention)")

    # Block style puts each key on its own line; at least two newlines for
    # a three-key dict means we have block formatting.
    check(passed, failed, block.count("\n") >= 3,
          "default dump produces block YAML (one key per line)")

    inline = node_x_yaml.dump(n, default_flow_style=True)
    # Inline collapses everything onto one logical line.
    check(passed, failed, inline.strip().startswith("{"),
          "dump with default_flow_style=True produces inline YAML")

    # Key order must be insertion order so snapshots are reproducible.
    keys_in_output = [line.split(":")[0].strip()
                      for line in block.splitlines()
                      if ":" in line]
    check(passed, failed,
          keys_in_output == ["alpha", "beta", "gamma"],
          "dump preserves insertion key order (sort_keys=False)")

    # ------------------------------------------------------------------
    heading("node_x_yaml: full round-trip — flat node")
    # ------------------------------------------------------------------

    original = SimpleNode({"title": "Abbey Road", "year": 1969, "active": True})
    text = node_x_yaml.dump(original)
    restored = node_x_yaml.load(SimpleNode, text)

    check(passed, failed, isinstance(restored, SimpleNode),
          "load returns correct subclass type")
    check(passed, failed, restored["title"] == "Abbey Road",
          "string field survives round-trip")
    check(passed, failed, restored["year"] == 1969,
          "integer field survives round-trip")
    check(passed, failed, restored["active"] is True,
          "boolean field survives round-trip")
    check(passed, failed, restored is not original,
          "round-trip produces a distinct object (not the original)")

    # ------------------------------------------------------------------
    heading("node_x_yaml: full round-trip — nested tree")
    # ------------------------------------------------------------------

    leaf   = ChildNode({"id": 7, "label": "leaf-node"})
    leaves = SerialisableList([ChildNode({"id": i}) for i in range(3)])
    tree   = TreeNode({"name": "root"})
    tree["leaf"]   = leaf
    tree["leaves"] = leaves

    text2    = node_x_yaml.dump(tree)
    restored2 = node_x_yaml.load(TreeNode, text2)

    check(passed, failed, restored2["name"] == "root",
          "tree root scalar survives round-trip")
    check(passed, failed, isinstance(restored2["leaf"], ChildNode),
          "nested child node is correct type after round-trip")
    check(passed, failed, restored2["leaf"]["id"] == 7,
          "nested child field survives round-trip")
    check(passed, failed, restored2["leaf"]["label"] == "leaf-node",
          "nested child string field survives round-trip")
    check(passed, failed, isinstance(restored2["leaves"], SerialisableList),
          "NodeList type is preserved after YAML round-trip")
    check(passed, failed, len(restored2["leaves"]) == 3,
          "NodeList length preserved after round-trip")
    check(passed, failed,
          [restored2["leaves"][i]["id"] for i in range(3)] == [0, 1, 2],
          "NodeList element fields survive round-trip")

    # ------------------------------------------------------------------
    heading("node_x_yaml: full round-trip — graph $ref (identity preserved)")
    # ------------------------------------------------------------------
    # A shared GNode (same Python object referenced from two places) must
    # serialise to a $ref in YAML and restore back to a single shared
    # Python object — not two independent copies.

    shared  = GNode({"_key": "artist-1", "name": "The Beatles"})
    wrapper = Container({})
    wrapper["ref_a"] = shared
    wrapper["ref_b"] = shared

    text3 = node_x_yaml.dump(wrapper)

    # The YAML must contain a $ref entry for the second reference.
    check(passed, failed, "$ref" in text3,
          "YAML output contains a $ref marker for the shared node")
    check(passed, failed, "artist-1" in text3,
          "YAML output contains the shared node key")

    restored3 = node_x_yaml.load(Container, text3)

    check(passed, failed,
          isinstance(restored3["ref_a"], GNode),
          "first reference restores as GNode instance")
    check(passed, failed,
          restored3["ref_a"] is restored3["ref_b"],
          "both references restore to the same Python object (graph identity)")
    check(passed, failed,
          restored3["ref_a"]["name"] == "The Beatles",
          "shared node payload survives YAML round-trip")

    # Mutation through one reference must be visible via the other.
    restored3["ref_a"]["name"] = "Beatles"
    check(passed, failed,
          restored3["ref_b"]["name"] == "Beatles",
          "mutation via one reference is visible through the other after YAML restore")

    # ------------------------------------------------------------------
    heading("node_x_yaml: full round-trip — graph $ref in NodeList")
    # ------------------------------------------------------------------

    item       = GNode({"_key": "item-1", "tag": "shared"})
    list_root  = ListContainer({})
    list_root["entries"] = SerialisableList([item, item])

    text4 = node_x_yaml.dump(list_root)
    check(passed, failed, "$ref" in text4,
          "YAML list output contains a $ref for the duplicated entry")

    restored4 = node_x_yaml.load(ListContainer, text4)
    check(passed, failed, len(restored4["entries"]) == 2,
          "list length preserved after YAML round-trip")
    check(passed, failed,
          restored4["entries"][0] is restored4["entries"][1],
          "list entries that were the same object restore as the same object")

    # ------------------------------------------------------------------
    heading("node_x_yaml: unicode — non-ASCII strings survive round-trip")
    # ------------------------------------------------------------------
    # allow_unicode=True in dump() means non-ASCII chars are written as
    # literal UTF-8, not \uXXXX escapes.  yaml.safe_load reads them back.

    n_uni = SimpleNode({"artist": "Björk", "title": "Ágætis byrjun"})
    text5 = node_x_yaml.dump(n_uni)

    # The raw YAML text must contain the literal characters, not escapes.
    check(passed, failed,
          "Björk" in text5,
          "dump writes non-ASCII chars as literal UTF-8 (not escape sequences)")

    restored5 = node_x_yaml.load(SimpleNode, text5)
    check(passed, failed, restored5["artist"] == "Björk",
          "non-ASCII artist name survives YAML round-trip")
    check(passed, failed, restored5["title"] == "Ágætis byrjun",
          "non-ASCII title survives YAML round-trip")

    # ------------------------------------------------------------------
    heading("node_x_yaml: programmer errors")
    # ------------------------------------------------------------------
    # Bad YAML text must raise yaml.YAMLError immediately rather than
    # producing a silently wrong structure.  An object without to_plain()
    # must raise AttributeError at dump() time, not inside yaml.dump().
    # An orphaned $ref must raise KeyError naming the missing key.

    msg_bad = catch_into(passed, failed,
                         "load with invalid YAML raises YAMLError",
                         _yaml.YAMLError,
                         lambda: node_x_yaml.load(SimpleNode, ": bad: [yaml"))
    check(passed, failed, bool(msg_bad),
          "YAMLError message is non-empty")

    catch_into(passed, failed,
               "dump on object without to_plain() raises AttributeError",
               AttributeError,
               lambda: node_x_yaml.dump(object()))

    # A hand-crafted YAML containing an orphaned $ref (no prior full
    # definition) must raise KeyError naming the missing key.
    orphan_yaml = "child:\n  $ref: ghost-key\n"

    class OrphanRoot(Serialisable, Node):
        _restore_via_payload = True
        node_fields = {"child": GNode}

    msg_orphan = catch_into(passed, failed,
                            "load with orphaned $ref raises KeyError",
                            KeyError,
                            lambda: node_x_yaml.load(OrphanRoot, orphan_yaml))
    check(passed, failed, "ghost-key" in msg_orphan,
          "KeyError message contains the missing $ref key")

    return len(passed), len(failed)
