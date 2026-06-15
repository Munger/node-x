## @file test_graph.py
##
## @brief Unit tests for the ``Graph`` node container.
##
## Sections:
##
##   - **Graph: construction and ensure** — prefix composition, identity
##     semantics, and ``_key`` stored as the fully-qualified key.
##   - **Graph: add and type safety** — correct list routing, TypeError on
##     undeclared types.
##   - **Graph: two-pass deserialise** — cross-type ``$ref`` links between node
##     collections resolve correctly because all nodes are pre-registered before
##     any reference is resolved.
##   - **Graph: nested Graphs** — child graphs compose prefixes; two-pass
##     pre-registration recurses into nested graph snapshots.
##   - **to_plain: $ref emission** — any keyed node (``_key`` set) emits a
##     full dict on first encounter and ``{"$ref": key}`` on every subsequent
##     encounter; no ``Graph`` inheritance is required.
##   - **deserialise: $ref resolution** — cross-references in plain snapshots
##     round-trip correctly when ``deserialise()`` is called on the root node.
##   - **deserialise: $ref across SerialisableList** — the ``$ref`` / registry
##     mechanism works when shared nodes appear as list items.
##   - **deserialise: error handling** — missing ``$ref`` raises ``KeyError``
##     with the absent key; non-dict raises ``TypeError``.
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
    Graph,
    Node,
    Serialisable,
    SerialisableList,
)

from _helpers import (
    check,
    catch_into,
    does_not_raise,
    heading,
)


def run() -> Tuple[int, int]:
    ## @brief Execute all Graph test sections and return pass/fail counts.
    ##
    ## @return ``(pass_count, fail_count)`` across all sections.

    passed: List[str] = []
    failed: List[str] = []

    # ------------------------------------------------------------------
    heading("Graph: construction and ensure")
    # ------------------------------------------------------------------
    # A Graph subclass declares list_fields to type its node collections.
    # ensure() guarantees identity: the same key always returns the
    # same Python object.  The node's _key is stored as the fully-qualified
    # (prefixed) form.

    class ArtistNode(Serialisable, Node):
        restore_via_payload = True

    class TestGraph(Graph):
        list_fields = {"artists": (SerialisableList, ArtistNode)}

    g = TestGraph({"_key": "uk"})

    beatles = g.ensure(ArtistNode, "beatles", name="The Beatles")

    check(passed, failed, isinstance(beatles, ArtistNode),
          "ensure returns an ArtistNode")
    check(passed, failed, beatles["_key"] == "uk/beatles",
          "ensure stores fully-qualified _key in node")
    check(passed, failed, beatles["name"] == "The Beatles",
          "ensure applies kwargs as initial payload")

    beatles2 = g.ensure(ArtistNode, "beatles")
    check(passed, failed, beatles is beatles2,
          "ensure returns same instance for same key")

    stones = g.ensure(ArtistNode, "stones")
    check(passed, failed, stones is not beatles,
          "ensure returns distinct instance for different key")
    check(passed, failed, stones["_key"] == "uk/stones",
          "second node also carries prefixed _key")

    # Graph with no _key: local keys used unchanged.
    class UnprefixedGraph(Graph):
        list_fields = {"artists": (SerialisableList, ArtistNode)}

    ug = UnprefixedGraph()
    ua = ug.ensure(ArtistNode, "beatles")
    check(passed, failed, ua["_key"] == "beatles",
          "graph with no _key stores local key unchanged")

    # ------------------------------------------------------------------
    heading("Graph: prefix and full_key")
    # ------------------------------------------------------------------

    check(passed, failed, g.prefix == "uk",
          "prefix property returns _key value")
    check(passed, failed, g.full_key("beatles") == "uk/beatles",
          "full_key prepends prefix")
    check(passed, failed, ug.prefix == "",
          "prefix is empty string when _key absent")
    check(passed, failed, ug.full_key("beatles") == "beatles",
          "full_key with no prefix returns key unchanged")

    # ------------------------------------------------------------------
    heading("Graph: add and type safety")
    # ------------------------------------------------------------------
    # add() routes a node to its matching list by isinstance; an undeclared
    # type raises TypeError.

    class TagNode(Serialisable, Node):
        restore_via_payload = True

    class TypedGraph(Graph):
        list_fields = {"artists": (SerialisableList, ArtistNode)}

    tg = TypedGraph({"_key": "tg"})
    artist = ArtistNode({"_key": "tg/solo", "name": "Solo"})
    tg.add(artist)

    check(passed, failed, len(tg["artists"]) == 1,
          "add() appends node to the matching list")
    check(passed, failed, tg["artists"][0] is artist,
          "add() stores the exact same instance")

    msg = catch_into(passed, failed,
                     "add() with undeclared type raises TypeError",
                     TypeError,
                     lambda: tg.add(TagNode({"_key": "tg/t1"})))
    check(passed, failed, "list_fields" in msg and "TagNode" in msg,
          "TypeError names the undeclared type and list_fields")

    # ------------------------------------------------------------------
    heading("Graph: two-pass deserialise — cross-type $ref resolution")
    # ------------------------------------------------------------------
    # The two-pass approach registers all nodes from all list_fields before
    # resolving any $ref.  This allows a release to $ref an artist that
    # appears later in the snapshot, and an artist to $ref releases.

    class ReleaseNode(Serialisable, Node):
        restore_via_payload = True
        node_fields = {"artist": ArtistNode}

    class ReleaseList(SerialisableList["ReleaseNode"]): pass

    class MusicGraph(Graph):
        list_fields = {
            "artists":  (SerialisableList, ArtistNode),
            "releases": (ReleaseList,      ReleaseNode),
        }

    # Build the graph in-memory.
    mg = MusicGraph({"_key": "music"})
    beatles = mg.ensure(ArtistNode, "beatles", name="The Beatles")
    ppm     = mg.ensure(ReleaseNode, "ppm", title="Please Please Me")
    ppm["artist"] = beatles   # forward reference that becomes $ref

    # Serialise to a plain snapshot.
    snap = mg.serialise(deep=True)

    check(passed, failed, snap.get("_key") == "music",
          "serialised graph carries its _key")
    check(passed, failed, isinstance(snap.get("artists"), list),
          "serialised graph has artists list")
    check(passed, failed, isinstance(snap.get("releases"), list),
          "serialised graph has releases list")

    # The release's artist field should be a $ref (already emitted in artists).
    release_snap = snap["releases"][0]
    check(passed, failed, release_snap.get("artist") == {"$ref": "music/beatles"},
          "release artist is serialised as $ref to beatles")

    # Restore and verify cross-type $ref resolution.
    restored = MusicGraph.deserialise(snap)

    check(passed, failed, isinstance(restored, MusicGraph),
          "deserialised result is a MusicGraph")
    check(passed, failed, len(restored["artists"]) == 1,
          "restored graph has one artist")
    check(passed, failed, len(restored["releases"]) == 1,
          "restored graph has one release")

    r_artist  = restored["artists"][0]
    r_release = restored["releases"][0]

    check(passed, failed, r_artist["name"] == "The Beatles",
          "restored artist carries original payload")
    check(passed, failed, r_release["title"] == "Please Please Me",
          "restored release carries original payload")
    check(passed, failed, r_release["artist"] is r_artist,
          "restored release.artist is the same object as the restored artist")

    # Mutation through one reference must be visible through the other.
    r_artist["name"] = "Beatles"
    check(passed, failed, r_release["artist"]["name"] == "Beatles",
          "mutation through artist reference visible via release.artist")

    # ------------------------------------------------------------------
    heading("Graph: two-pass — forward $ref (release listed before artist)")
    # ------------------------------------------------------------------
    # The pre-pass must handle the case where the $ref appears before the
    # full node definition in the serialised order.

    forward_snap = {
        "_key": "music",
        # release appears FIRST in the snapshot
        "releases": [
            {"_key": "music/ppm", "title": "PPM",
             "artist": {"$ref": "music/beatles"}},  # forward ref
        ],
        # artist appears SECOND
        "artists": [
            {"_key": "music/beatles", "name": "The Beatles"},
        ],
    }

    restored2 = MusicGraph.deserialise(forward_snap)
    check(passed, failed,
          restored2["releases"][0]["artist"] is restored2["artists"][0],
          "forward $ref resolves: release listed before artist in snapshot")

    # ------------------------------------------------------------------
    heading("Graph: nested Graphs compose prefixes")
    # ------------------------------------------------------------------

    class SubGraph(Graph):
        list_fields = {"artists": (SerialisableList, ArtistNode)}

    class SubGraphList(SerialisableList["SubGraph"]): pass

    class RootGraph(Graph):
        list_fields = {"regions": (SubGraphList, SubGraph)}

    rg = RootGraph({"_key": "root"})
    uk = rg.ensure(SubGraph, "uk")
    check(passed, failed, uk["_key"] == "root/uk",
          "nested graph _key is prefixed by parent")
    check(passed, failed, uk.prefix == "root/uk",
          "nested graph prefix reflects full composite key")

    uk_beatles = uk.ensure(ArtistNode, "beatles")
    check(passed, failed, uk_beatles["_key"] == "root/uk/beatles",
          "node in nested graph carries doubly-prefixed _key")

    # ------------------------------------------------------------------
    heading("Graph + Serialisable: to_plain $ref emission")
    # ------------------------------------------------------------------
    # Any node carrying _key gets $ref deduplication in to_plain() — no
    # Graph inheritance required.  The first encounter is serialised in
    # full; every subsequent encounter emits {"$ref": key}.

    class KNode(Serialisable, Node):
        restore_via_payload = True

    class KRoot(Serialisable, Node):
        restore_via_payload = True

    shared = KNode({"_key": "art1", "name": "The Beatles"})
    root   = KRoot({})
    root["ref_a"] = shared
    root["ref_b"] = shared

    plain = root.to_plain()

    check(passed, failed,
          isinstance(plain["ref_a"], dict) and plain["ref_a"].get("_key") == "art1",
          "to_plain emits full dict on first encounter of keyed node")
    check(passed, failed,
          plain["ref_b"] == {"$ref": "art1"},
          "to_plain emits $ref on second encounter of same keyed node")

    # Unkeyed nodes are never deduplicated.
    unkeyed = KRoot({"x": 99})
    root2   = KRoot({})
    root2["u1"] = unkeyed
    root2["u2"] = unkeyed
    plain2 = root2.to_plain()
    check(passed, failed,
          plain2["u1"] == {"x": 99} and plain2["u2"] == {"x": 99},
          "unkeyed node appearing twice is serialised in full both times")

    # ------------------------------------------------------------------
    heading("Serialisable: deserialise $ref resolution (graph identity)")
    # ------------------------------------------------------------------
    # $ref markers produced by to_plain() must round-trip back to the
    # *same* Python object.

    class ArtNode(Serialisable, Node):
        restore_via_payload = True

    class WeekNode(Serialisable, Node):
        restore_via_payload = True
        node_fields = {"artist": ArtNode}

    class RootNode(Serialisable, Node):
        restore_via_payload = True
        node_fields = {"w1": WeekNode, "w2": WeekNode}

    artist = ArtNode({"_key": "a1", "name": "Beatles"})
    w1 = WeekNode({"date": "1963-01-05"})
    w2 = WeekNode({"date": "1963-01-12"})
    w1["artist"] = artist
    w2["artist"] = artist

    rn = RootNode({})
    rn["w1"] = w1
    rn["w2"] = w2

    plain = rn.to_plain()
    check(passed, failed,
          plain["w1"]["artist"].get("_key") == "a1",
          "to_plain: first artist reference is a full dict")
    check(passed, failed,
          plain["w2"]["artist"] == {"$ref": "a1"},
          "to_plain: second artist reference is a $ref")

    res = RootNode.deserialise(plain)
    check(passed, failed,
          isinstance(res["w1"]["artist"], ArtNode),
          "restored first reference is an ArtNode instance")
    check(passed, failed,
          res["w1"]["artist"] is res["w2"]["artist"],
          "restored graph: both references are the same Python object")
    check(passed, failed,
          res["w1"]["artist"]["name"] == "Beatles",
          "restored shared node carries its original payload")

    res["w1"]["artist"]["name"] = "The Beatles"
    check(passed, failed,
          res["w2"]["artist"]["name"] == "The Beatles",
          "mutation through one reference is visible through the other")

    # ------------------------------------------------------------------
    heading("Serialisable: deserialise $ref across SerialisableList")
    # ------------------------------------------------------------------

    class ItemNode(Serialisable, Node):
        restore_via_payload = True

    class ListRoot(Serialisable, Node):
        restore_via_payload = True
        list_fields = {"entries": (SerialisableList, ItemNode)}

    shared_item = ItemNode({"_key": "i1", "tag": "shared"})
    lr = ListRoot({})
    lr["entries"] = SerialisableList([shared_item, shared_item])

    plain3 = lr.to_plain()
    check(passed, failed,
          isinstance(plain3["entries"], list) and len(plain3["entries"]) == 2,
          "to_plain produces two-element list")
    check(passed, failed,
          plain3["entries"][0].get("_key") == "i1",
          "to_plain: first list item is full dict")
    check(passed, failed,
          plain3["entries"][1] == {"$ref": "i1"},
          "to_plain: second list item is $ref")

    res3 = ListRoot.deserialise(plain3)
    check(passed, failed, len(res3["entries"]) == 2,
          "restored list has correct length")
    check(passed, failed,
          res3["entries"][0] is res3["entries"][1],
          "restored list: both entries are the same Python object")

    # ------------------------------------------------------------------
    heading("Serialisable: deserialise $ref error handling")
    # ------------------------------------------------------------------

    class ErrNode(Serialisable, Node):
        restore_via_payload = True

    class ErrRoot(Serialisable, Node):
        restore_via_payload = True
        node_fields = {"child": ErrNode}

    bad = {"child": {"$ref": "nonexistent-key"}}
    msg = catch_into(passed, failed,
                     "deserialise with unresolvable $ref raises KeyError",
                     KeyError,
                     lambda: ErrRoot.deserialise(bad))
    check(passed, failed, "nonexistent-key" in msg,
          "KeyError message contains the missing key")
    check(passed, failed,
          "deserialise()" in msg or "root node" in msg,
          "KeyError message suggests calling deserialise() from the root")

    msg3 = catch_into(passed, failed,
                      "deserialise(42) raises TypeError",
                      TypeError,
                      lambda: ErrNode.deserialise(42))
    check(passed, failed,
          "mapping" in msg3 and "int" in msg3,
          "non-dict TypeError says 'mapping' and names the type")
    check(passed, failed,
          "serialise(deep=True)" in msg3,
          "non-dict TypeError mentions serialise(deep=True) as the correct source")

    # ------------------------------------------------------------------
    heading("Graph: programmer errors — ensure and add")
    # ------------------------------------------------------------------
    # ensure() must reject non-string keys immediately with a message
    # that explains the serialisation reason.  Passing a class that is not
    # declared in list_fields must raise TypeError that names both the class
    # and list_fields so the fix is obvious.

    class ErrArtist(Serialisable, Node):
        restore_via_payload = True

    class ErrTag(Serialisable, Node):
        restore_via_payload = True

    class ErrGraph(Graph):
        list_fields = {"artists": (SerialisableList, ErrArtist)}

    eg = ErrGraph({"_key": "err"})

    msg_int = catch_into(passed, failed,
                         "ensure with int key raises TypeError",
                         TypeError,
                         lambda: eg.ensure(ErrArtist, 42))
    check(passed, failed, "int" in msg_int and "str" in msg_int,
          "TypeError names the bad type and required type")
    check(passed, failed, "$ref" in msg_int or "serialis" in msg_int,
          "TypeError explains the serialisation reason")

    msg_tuple = catch_into(passed, failed,
                           "ensure with tuple key raises TypeError",
                           TypeError,
                           lambda: eg.ensure(ErrArtist, ("a", "b")))
    check(passed, failed, "tuple" in msg_tuple,
          "TypeError for tuple key names the offending type")

    # Class not in list_fields: TypeError should name the class and list_fields.
    msg_cls = catch_into(passed, failed,
                         "ensure with undeclared class raises TypeError",
                         TypeError,
                         lambda: eg.ensure(ErrTag, "t1"))
    check(passed, failed, "ErrTag" in msg_cls and "list_fields" in msg_cls,
          "TypeError names the undeclared class and list_fields")
    check(passed, failed, type(eg).__name__ in msg_cls,
          "TypeError names the graph class so the caller knows which list_fields to update")

    # ------------------------------------------------------------------
    heading("Graph: from_nodes()")
    # ------------------------------------------------------------------
    # from_nodes() constructs a new Graph subclass instance from any
    # iterable of nodes, routing each via add().  It is inherited by every
    # Graph subclass automatically.

    class FNArtist(Serialisable, Node):
        restore_via_payload = True

    class FNRelease(Serialisable, Node):
        restore_via_payload = True

    class FNGraph(Graph):
        list_fields = {
            "artists":  (SerialisableList, FNArtist),
            "releases": (SerialisableList, FNRelease),
        }

    a1 = FNArtist({"_key": "fn/a1", "name": "Artist One"})
    a2 = FNArtist({"_key": "fn/a2", "name": "Artist Two"})
    r1 = FNRelease({"_key": "fn/r1", "title": "Release One"})
    r2 = FNRelease({"_key": "fn/r2", "title": "Release Two"})

    result = FNGraph.from_nodes([a1, a2, r1, r2])

    check(passed, failed, isinstance(result, FNGraph),
          "from_nodes() returns an instance of the subclass")
    check(passed, failed, len(result["artists"]) == 2,
          "from_nodes() routes artists to correct list")
    check(passed, failed, len(result["releases"]) == 2,
          "from_nodes() routes releases to correct list")
    check(passed, failed, result["artists"][0] is a1,
          "from_nodes() preserves node identity — not copies")
    check(passed, failed, result["releases"][1] is r2,
          "from_nodes() preserves order")

    # from_nodes() with a generator — consumed lazily
    gen_result = FNGraph.from_nodes(n for n in [a1, r1])
    check(passed, failed, len(gen_result["artists"]) == 1,
          "from_nodes() accepts a generator")

    # Node type not in list_fields raises TypeError
    class FNOther(Node): pass
    catch_into(passed, failed,
               "from_nodes() with undeclared type raises TypeError",
               TypeError,
               lambda: FNGraph.from_nodes([FNOther({})]))

    # from_nodes() on empty iterable returns empty graph
    empty = FNGraph.from_nodes([])
    check(passed, failed, empty.get("artists") is None and empty.get("releases") is None,
          "from_nodes() on empty iterable returns empty graph")

    return len(passed), len(failed)
