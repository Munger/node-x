## @file 03_graph.py
##
## @brief A film database demonstrating Graph identity across serialisation.
##
## Covers: Graph, list_fields, ensure(), cross-type $ref, two-pass
## deserialise(), identity preserved after JSON round-trip.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/Munger/node-x
## @par Licence: MIT

import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from node_x import Graph, Node, Serialisable, SerialisableList


# ---------------------------------------------------------------------------
# Node subclasses — plain Serialisable nodes, no special base class needed
# ---------------------------------------------------------------------------

class Director(Serialisable, Node):
    restore_via_payload = True


class Film(Serialisable, Node):
    restore_via_payload = True
    # The director field is a cross-type reference to a Director node.
    node_fields = {"director": Director}


# ---------------------------------------------------------------------------
# Graph subclass — declares which node types it holds
# ---------------------------------------------------------------------------

class FilmGraph(Graph):
    # Graph.list_fields wires up two typed collections.
    # On serialise: a Film's director becomes {"$ref": "<key>"} rather than
    # a duplicated inline dict.
    # On deserialise: all nodes are pre-registered before any $ref is
    # resolved, so forward references and cross-type links both work.
    list_fields = {
        "directors": (SerialisableList, Director),
        "films":     (SerialisableList, Film),
    }


# ---------------------------------------------------------------------------
# Build the graph
# ---------------------------------------------------------------------------

db = FilmGraph({"_key": "films"})

# ensure() registers the node under its fully-qualified key
# ("films/kubrick") and returns the same instance on every subsequent call.
kubrick  = db.ensure(Director, "kubrick",  name="Stanley Kubrick")
hitchcock = db.ensure(Director, "hitchcock", name="Alfred Hitchcock")

shining    = db.ensure(Film, "the-shining",       title="The Shining",        year=1980)
clockwork  = db.ensure(Film, "clockwork-orange",  title="A Clockwork Orange",  year=1971)
psycho     = db.ensure(Film, "psycho",            title="Psycho",              year=1960)

# Link each film to its director.  Both Kubrick films point to the same object.
shining["director"]   = kubrick
clockwork["director"] = kubrick
psycho["director"]    = hitchcock

print("Films:")
for film in db["films"]:
    print(f"  {film['title']} ({film['year']}) — dir. {film['director']['name']}")

# ensure returns the same instance every time.
assert db.ensure(Director, "kubrick") is kubrick


# ---------------------------------------------------------------------------
# Serialise — shared Director becomes a $ref
# ---------------------------------------------------------------------------

snapshot = db.serialise(deep=True)

# Kubrick appears in full once (in the directors list), then as {"$ref": …}
# in each of his films so the data is never duplicated.
kubrick_refs = sum(
    1 for f in snapshot["films"]
    if isinstance(f.get("director"), dict) and "$ref" in f["director"]
)
print(f"\nKubrick serialised as $ref in {kubrick_refs} film(s) (not duplicated)")


# ---------------------------------------------------------------------------
# Deserialise — identity is restored across cross-type references
# ---------------------------------------------------------------------------

restored = FilmGraph.deserialise(snapshot)

r_films     = restored["films"]
r_directors = restored["directors"]

# Find Kubrick in the restored directors list.
r_kubrick = next(d for d in r_directors if d["name"] == "Stanley Kubrick")

# Both Kubrick films must point to the exact same Python object.
r_kubrick_films = [f for f in r_films if f.get("director") is r_kubrick]
print(f"\nAfter restore: {len(r_kubrick_films)} Kubrick film(s) share one Director object")
assert len(r_kubrick_films) == 2

# Mutating through one reference is immediately visible through the other.
r_kubrick_films[0]["director"]["name"] = "S. Kubrick"
assert r_kubrick_films[1]["director"]["name"] == "S. Kubrick"
print("Mutation through one film's director is visible from the other ✓")
