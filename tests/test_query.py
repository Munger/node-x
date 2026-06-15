## @file test_query.py
##
## @brief Unit tests for node_x_query — predicates, query builder, NodeIndex.
##
## Sections:
##
##   - **Predicates: basic** — Always, HasField, FieldEquals, FieldNot,
##     comparison operators, FieldIn, FieldMatches, IsType.
##   - **Predicates: composition** — &, |, ~ operators and triple chains.
##   - **Standalone functions** — where(), group_by(), unique_by(), order_by().
##   - **Query: flat operations** — where, order_by, unique_by, group_by,
##     first, count; laziness; source immutability; single-node source.
##   - **Query: combined** — chained operations; mutation independence.
##   - **Query: graph traversal via .nodes()** — enters list_fields; filters;
##     wrong-type error.
##   - **Query: graph traversal via .traverse() and .back()** — follows
##     node_fields and NodeList fields; deduplicates .back(); ordering after
##     .back().
##   - **NodeIndex** — getitem, get, get_all, contains, keys; missing nodes
##     skipped; generator source; multiple values per key.
##   - **Programmer errors** — clear messages for wrong predicate type, wrong
##     .nodes() argument.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/Munger/node-x
## @par Licence: MIT

from __future__ import annotations

import sys
from pathlib import Path
from typing import List, Tuple

_PKG_DIR  = str(Path(__file__).resolve().parent)
_ROOT_DIR = str(Path(__file__).resolve().parent.parent)
for _d in (_PKG_DIR, _ROOT_DIR):
    if _d not in sys.path:
        sys.path.insert(0, _d)

from node_x import Node, NodeList, Graph, Serialisable, SerialisableList
from node_x.node_x_query import (
    query, where, group_by, unique_by, order_by,
    union, intersect, difference,
    NodeIndex,
    Always, HasField,
    FieldEquals, FieldNot, FieldLt, FieldLte, FieldGt, FieldGte,
    FieldIn, FieldMatches, IsType,
)
from _helpers import check, catch_into, does_not_raise, heading


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

def _products() -> NodeList:
    return NodeList([
        Node({"sku": "A001", "name": "Earl Grey",             "category": "Tea",    "price": 8.50,  "stock": 120}),
        Node({"sku": "A002", "name": "Chamomile",             "category": "Tea",    "price": 6.99,  "stock": 45}),
        Node({"sku": "A003", "name": "Espresso Blend",        "category": "Coffee", "price": 12.99, "stock": 80}),
        Node({"sku": "A004", "name": "Dark Roast",            "category": "Coffee", "price": 14.99, "stock": 30}),
        Node({"sku": "A005", "name": "Cold Brew Concentrate", "category": "Coffee", "price": 18.00, "stock": 12}),
    ])


class Director(Serialisable, Node):
    restore_via_payload = True


class Film(Serialisable, Node):
    restore_via_payload = True
    node_fields = {"director": Director}


class FilmGraph(Graph):
    list_fields = {
        "directors": (SerialisableList, Director),
        "films":     (SerialisableList, Film),
    }


def _film_graph():
    db = FilmGraph({"_key": "films"})
    kubrick   = db.ensure(Director, "kubrick",   name="Stanley Kubrick")
    hitchcock = db.ensure(Director, "hitchcock", name="Alfred Hitchcock")
    shining   = db.ensure(Film, "shining",   title="The Shining",        year=1980)
    clockwork = db.ensure(Film, "clockwork", title="A Clockwork Orange",  year=1971)
    psycho    = db.ensure(Film, "psycho",    title="Psycho",              year=1960)
    shining["director"]   = kubrick
    clockwork["director"] = kubrick
    psycho["director"]    = hitchcock
    return db, kubrick, hitchcock, shining, clockwork, psycho


# ---------------------------------------------------------------------------
# run()
# ---------------------------------------------------------------------------

def run() -> Tuple[int, int]:
    ## @brief Execute all query test sections and return pass/fail counts.

    passed: List[str] = []
    failed: List[str] = []

    # ======================================================================
    heading("Predicates: basic")
    # ======================================================================

    n_tea    = Node({"category": "Tea",    "price": 8.50})
    n_coffee = Node({"category": "Coffee", "price": 12.99})
    n_empty  = Node({})

    check(passed, failed, Always().matches(n_tea),    "Always matches any node")
    check(passed, failed, Always().matches(n_empty),  "Always matches empty node")

    check(passed, failed, HasField("price").matches(n_tea),      "HasField matches present field")
    check(passed, failed, not HasField("price").matches(n_empty), "HasField misses absent field")
    check(passed, failed, not HasField("x").matches(n_tea),       "HasField misses wrong field")

    check(passed, failed, FieldEquals("category", "Tea").matches(n_tea),       "FieldEquals matches")
    check(passed, failed, not FieldEquals("category", "Tea").matches(n_coffee), "FieldEquals no match")
    check(passed, failed, not FieldEquals("category", "Tea").matches(n_empty),  "FieldEquals absent field")
    check(passed, failed, FieldEquals("count", 0).matches(Node({"count": 0})),  "FieldEquals zero value")

    check(passed, failed, FieldNot("category", "Tea").matches(n_coffee),       "FieldNot matches different value")
    check(passed, failed, not FieldNot("category", "Tea").matches(n_tea),       "FieldNot rejects equal value")
    check(passed, failed, FieldNot("category", "Tea").matches(n_empty),         "FieldNot absent field counts as different")

    check(passed, failed, FieldLt("price", 10).matches(n_tea),          "FieldLt below threshold")
    check(passed, failed, not FieldLt("price", 10).matches(n_coffee),    "FieldLt above threshold")
    check(passed, failed, not FieldLt("price", 8.50).matches(n_tea),     "FieldLt equal is not less")
    check(passed, failed, not FieldLt("price", 10).matches(n_empty),     "FieldLt absent field no match")

    check(passed, failed, FieldLte("price", 8.50).matches(n_tea),        "FieldLte at boundary")
    check(passed, failed, FieldLte("price", 9.00).matches(n_tea),        "FieldLte below boundary")
    check(passed, failed, not FieldLte("price", 8.00).matches(n_tea),    "FieldLte above boundary")

    check(passed, failed, FieldGt("price", 10).matches(n_coffee),        "FieldGt above threshold")
    check(passed, failed, not FieldGt("price", 10).matches(n_tea),       "FieldGt below threshold")
    check(passed, failed, not FieldGt("price", 12.99).matches(n_coffee), "FieldGt equal is not greater")
    check(passed, failed, not FieldGt("price", 10).matches(n_empty),     "FieldGt absent field no match")

    check(passed, failed, FieldGte("price", 12.99).matches(n_coffee),    "FieldGte at boundary")
    check(passed, failed, FieldGte("price", 12.00).matches(n_coffee),    "FieldGte below boundary")
    check(passed, failed, not FieldGte("price", 13.00).matches(n_coffee), "FieldGte above boundary")

    check(passed, failed,
          FieldIn("category", ["Tea", "Coffee"]).matches(n_tea),    "FieldIn value present")
    check(passed, failed,
          not FieldIn("category", ["Coffee"]).matches(n_tea),        "FieldIn value absent from set")
    check(passed, failed,
          not FieldIn("category", ["Tea"]).matches(n_empty),         "FieldIn absent field")

    check(passed, failed,
          FieldMatches("name", r"^Earl").matches(Node({"name": "Earl Grey"})),   "FieldMatches anchored start")
    check(passed, failed,
          FieldMatches("name", r"Grey").matches(Node({"name": "Earl Grey"})),    "FieldMatches search (not fullmatch)")
    check(passed, failed,
          not FieldMatches("name", r"^Earl").matches(Node({"name": "Chamomile"})), "FieldMatches no match")
    check(passed, failed,
          not FieldMatches("price", r"\d").matches(Node({"price": 8.50})),       "FieldMatches non-string field")
    check(passed, failed,
          not FieldMatches("name", r".*").matches(n_empty),                      "FieldMatches absent field")

    class FooNode(Node): pass
    class BarNode(Node): pass
    class BazNode(FooNode): pass
    check(passed, failed, IsType(FooNode).matches(FooNode({})),    "IsType exact match")
    check(passed, failed, IsType(FooNode).matches(BazNode({})),    "IsType matches subclass")
    check(passed, failed, not IsType(FooNode).matches(BarNode({})), "IsType rejects wrong type")

    # ======================================================================
    heading("Predicates: composition")
    # ======================================================================

    both = FieldEquals("category", "Coffee") & FieldLt("price", 15)
    check(passed, failed, both.matches(n_coffee),           "& both true")
    check(passed, failed, not both.matches(n_tea),           "& left false")
    check(passed, failed, not both.matches(Node({"category": "Coffee", "price": 20})), "& right false")

    either = FieldEquals("category", "Tea") | FieldLt("price", 7)
    check(passed, failed, either.matches(n_tea),             "| left true")
    check(passed, failed, either.matches(Node({"category": "Coffee", "price": 5})), "| right true")
    check(passed, failed, not either.matches(n_coffee),      "| both false")

    neg = ~FieldEquals("category", "Tea")
    check(passed, failed, neg.matches(n_coffee),             "~ negation matches")
    check(passed, failed, not neg.matches(n_tea),            "~ negation rejects")

    triple = FieldEquals("category", "Coffee") & FieldGt("price", 10) & FieldLt("price", 15)
    check(passed, failed, triple.matches(n_coffee),          "triple & all true")
    check(passed, failed, not triple.matches(Node({"category": "Coffee", "price": 20})), "triple & last false")

    check(passed, failed,
          (~(FieldEquals("category", "Coffee") & FieldLt("price", 10))).matches(n_coffee),
          "~(a & b) matches when inner is false")

    # ======================================================================
    heading("Standalone functions")
    # ======================================================================

    products = _products()

    result = where(products, FieldEquals("category", "Coffee"))
    check(passed, failed, len(result) == 3,                          "where() count")
    check(passed, failed, isinstance(result, NodeList),              "where() returns NodeList")
    check(passed, failed, all(n["category"] == "Coffee" for n in result), "where() values correct")

    groups = group_by(products, lambda n: n["category"])
    check(passed, failed, set(groups.keys()) == {"Tea", "Coffee"},   "group_by() keys")
    check(passed, failed, len(groups["Tea"]) == 2,                   "group_by() Tea count")
    check(passed, failed, len(groups["Coffee"]) == 3,                "group_by() Coffee count")
    check(passed, failed, isinstance(groups["Tea"], NodeList),       "group_by() values are NodeLists")

    unique = unique_by(products, lambda n: n["category"])
    check(passed, failed, len(unique) == 2,                          "unique_by() count")
    check(passed, failed, unique[0]["category"] == "Tea",            "unique_by() first occurrence kept (Tea)")
    check(passed, failed, unique[1]["category"] == "Coffee",         "unique_by() second occurrence kept (Coffee)")
    check(passed, failed, isinstance(unique, NodeList),              "unique_by() returns NodeList")

    sorted_asc = order_by(products, "price")
    prices = [n["price"] for n in sorted_asc]
    check(passed, failed, prices == sorted(prices),                  "order_by() ascending")

    sorted_desc = order_by(products, "price", reverse=True)
    prices_desc = [n["price"] for n in sorted_desc]
    check(passed, failed, prices_desc == sorted(prices_desc, reverse=True), "order_by() descending")

    nodes_missing = NodeList([
        Node({"name": "b", "price": 5}),
        Node({"name": "a"}),
        Node({"name": "c", "price": 3}),
    ])
    sorted_missing = order_by(nodes_missing, "price")
    check(passed, failed, sorted_missing[0]["name"] == "c",          "order_by() missing field sorts last (first item)")
    check(passed, failed, sorted_missing[-1]["name"] == "a",         "order_by() missing field sorts last (last item)")

    # ======================================================================
    heading("Query: flat operations")
    # ======================================================================

    products = _products()

    check(passed, failed, len(query(products).all()) == 5,           "all() returns full list")
    check(passed, failed, isinstance(query(products).all(), NodeList), "all() returns NodeList")

    teas = query(products).where(FieldEquals("category", "Tea")).all()
    check(passed, failed, len(teas) == 2,                            "where() filters correctly")

    chained = (query(products)
               .where(FieldEquals("category", "Coffee"))
               .where(FieldLt("price", 13))
               .all())
    check(passed, failed, len(chained) == 1,                         "chained where() narrows result")
    check(passed, failed, chained[0]["name"] == "Espresso Blend",    "chained where() correct item")

    first = query(products).where(FieldEquals("category", "Tea")).first()
    check(passed, failed, first is not None,                         "first() returns a node")
    check(passed, failed, first["category"] == "Tea",                "first() returns matching node")

    check(passed, failed,
          query(products).where(FieldEquals("category", "Juice")).first() is None,
          "first() returns None when no match")

    check(passed, failed,
          query(products).where(FieldEquals("category", "Coffee")).count() == 3,
          "count() correct")

    asc = query(products).order_by("price").all()
    asc_prices = [n["price"] for n in asc]
    check(passed, failed, asc_prices == sorted(asc_prices),          "order_by() ascending in query")

    desc = query(products).order_by("price", reverse=True).all()
    desc_prices = [n["price"] for n in desc]
    check(passed, failed, desc_prices == sorted(desc_prices, reverse=True), "order_by() descending in query")

    u = query(products).unique_by(lambda n: n["category"]).all()
    check(passed, failed, len(u) == 2,                               "unique_by() in query")

    by_cat = query(products).group_by(lambda n: n["category"])
    check(passed, failed, set(by_cat.keys()) == {"Tea", "Coffee"},   "group_by() terminal")

    # Laziness — building a query does not execute it
    original_len = len(products)
    q = query(products).where(FieldEquals("category", "Tea")).order_by("price")
    check(passed, failed, len(products) == original_len,             "query does not mutate source")

    # Single-node source
    single = query(Node({"category": "Tea", "price": 8.50})).where(FieldEquals("category", "Tea")).all()
    check(passed, failed, len(single) == 1,                          "single-node source works")

    # Empty source
    check(passed, failed, query(NodeList()).all() == [],              "empty source: all() returns []")
    check(passed, failed, query(NodeList()).first() is None,          "empty source: first() returns None")
    check(passed, failed, query(NodeList()).count() == 0,             "empty source: count() is 0")

    # ======================================================================
    heading("Query: combined operations")
    # ======================================================================

    products = _products()

    ordered_coffees = (query(products)
                       .where(FieldEquals("category", "Coffee"))
                       .order_by("price")
                       .all())
    names = [n["name"] for n in ordered_coffees]
    check(passed, failed,
          names == ["Espresso Blend", "Dark Roast", "Cold Brew Concentrate"],
          "where then order_by produces correct ordered result")

    cheapest_per_cat = (query(products)
                        .order_by("price")
                        .unique_by(lambda n: n["category"])
                        .all())
    check(passed, failed, len(cheapest_per_cat) == 2,                "order then unique_by count")
    by_cat_map = {n["category"]: n["price"] for n in cheapest_per_cat}
    check(passed, failed, by_cat_map["Tea"] == 6.99,                 "cheapest tea via order+unique")
    check(passed, failed, by_cat_map["Coffee"] == 12.99,             "cheapest coffee via order+unique")

    # Chaining is immutable — base query is not affected by derived query
    base = query(products).where(FieldEquals("category", "Coffee"))
    extended = base.where(FieldLt("price", 13))
    check(passed, failed, base.count() == 3,                         "base query unchanged after chaining")
    check(passed, failed, extended.count() == 1,                     "derived query applies additional filter")

    negated = query(products).where(~FieldEquals("category", "Tea")).all()
    check(passed, failed, len(negated) == 3,                         "negated predicate in where()")
    check(passed, failed, all(n["category"] == "Coffee" for n in negated), "negated predicate correct values")

    # ======================================================================
    heading("Query: graph traversal via .nodes()")
    # ======================================================================

    db, kubrick, hitchcock, shining, clockwork, psycho = _film_graph()

    films = query(db).nodes(Film).all()
    check(passed, failed, len(films) == 3,                           "nodes() enters list_fields, count")
    check(passed, failed, all(isinstance(f, Film) for f in films),   "nodes() returns correct type")

    directors = query(db).nodes(Director).all()
    check(passed, failed, len(directors) == 2,                       "nodes() for Director type")

    early = query(db).nodes(Film).where(FieldLt("year", 1970)).all()
    check(passed, failed, len(early) == 1,                           "nodes().where() count")
    check(passed, failed, early[0]["title"] == "Psycho",             "nodes().where() correct film")

    class OtherNode(Node): pass
    check(passed, failed,
          query(db).nodes(OtherNode).all() == [],
          "nodes() with no matching list_field returns empty")

    # ======================================================================
    heading("Query: graph traversal via .traverse() and .back()")
    # ======================================================================

    db, kubrick, hitchcock, shining, clockwork, psycho = _film_graph()

    traversed_directors = query(db).nodes(Film).traverse("director").all()
    check(passed, failed, len(traversed_directors) == 3,             "traverse() one director per film")
    check(passed, failed,
          all(isinstance(d, Director) for d in traversed_directors), "traverse() returns Director nodes")

    kubrick_paths = (query(db)
                     .nodes(Film)
                     .traverse("director")
                     .where(FieldEquals("name", "Stanley Kubrick"))
                     .all())
    check(passed, failed, len(kubrick_paths) == 2,                   "traverse().where() one per Kubrick film")
    check(passed, failed, all(d is kubrick for d in kubrick_paths),  "traverse() yields same object")

    kubrick_films = (query(db)
                     .nodes(Film)
                     .traverse("director")
                     .where(FieldEquals("name", "Stanley Kubrick"))
                     .back()
                     .all())
    check(passed, failed, len(kubrick_films) == 2,                   "back() returns Film nodes")
    check(passed, failed,
          all(isinstance(f, Film) for f in kubrick_films),           "back() nodes are Films")
    titles = {f["title"] for f in kubrick_films}
    check(passed, failed,
          titles == {"The Shining", "A Clockwork Orange"},           "back() correct film titles")

    all_films_back = (query(db)
                      .nodes(Film)
                      .traverse("director")
                      .back()
                      .all())
    check(passed, failed, len(all_films_back) == 3,                  "back() deduplicates film nodes")

    ordered_back = (query(db)
                    .nodes(Film)
                    .traverse("director")
                    .where(FieldEquals("name", "Stanley Kubrick"))
                    .back()
                    .order_by("year")
                    .all())
    check(passed, failed,
          ordered_back[0]["year"] < ordered_back[1]["year"],         "order_by() after back() works")

    check(passed, failed,
          query(_products()).back().all() == [],
          "back() on non-traversal path returns empty")

    # Traverse a NodeList field (not a single node ref)
    class TagNode(Node): pass
    film_with_tags = Node({
        "title": "Test",
        "tags": NodeList([TagNode({"name": "thriller"}), TagNode({"name": "classic"})]),
    })
    tag_result = query(NodeList([film_with_tags])).traverse("tags").all()
    check(passed, failed, len(tag_result) == 2,                      "traverse() follows NodeList field")
    check(passed, failed, isinstance(tag_result[0], TagNode),        "traverse() NodeList items have correct type")

    # Film with no director is skipped by traverse
    db2 = FilmGraph({"_key": "test"})
    db2.ensure(Film, "no-dir", title="Mystery", year=2000)
    check(passed, failed,
          query(db2).nodes(Film).traverse("director").all() == [],
          "traverse() skips nodes with absent field")

    # ======================================================================
    heading("NodeIndex")
    # ======================================================================

    products = _products()
    sku_index = NodeIndex(products, "sku")

    check(passed, failed, sku_index["A003"]["name"] == "Espresso Blend", "NodeIndex getitem")
    check(passed, failed, sku_index.get("A001")["name"] == "Earl Grey",  "NodeIndex get() found")
    check(passed, failed, sku_index.get("ZZZZ") is None,                 "NodeIndex get() missing returns None")

    sentinel = Node({"name": "default"})
    check(passed, failed, sku_index.get("ZZZZ", sentinel) is sentinel,   "NodeIndex get() with default")

    check(passed, failed, len(sku_index.get_all("A001")) == 1,            "NodeIndex get_all() single match")
    check(passed, failed, isinstance(sku_index.get_all("A001"), NodeList), "NodeIndex get_all() returns NodeList")
    check(passed, failed, sku_index.get_all("ZZZZ") == [],                "NodeIndex get_all() no match returns empty")

    cat_index = NodeIndex(products, "category")
    coffees = cat_index.get_all("Coffee")
    check(passed, failed, len(coffees) == 3,                              "NodeIndex get_all() multiple matches")

    check(passed, failed, "A001" in sku_index,                            "NodeIndex contains found")
    check(passed, failed, "ZZZZ" not in sku_index,                        "NodeIndex contains missing")
    check(passed, failed, set(sku_index.keys()) == {"A001","A002","A003","A004","A005"}, "NodeIndex keys()")

    nodes_partial = NodeList([
        Node({"sku": "X1", "name": "has sku"}),
        Node({"name": "no sku"}),
    ])
    partial_index = NodeIndex(nodes_partial, "sku")
    check(passed, failed, "X1" in partial_index,                          "NodeIndex skips nodes without field")
    check(passed, failed, len(list(partial_index.keys())) == 1,           "NodeIndex only indexes present fields")

    gen = (Node({"id": i, "v": i * 2}) for i in range(3))
    gen_index = NodeIndex(gen, "id")
    check(passed, failed, gen_index[2]["v"] == 4,                         "NodeIndex from generator")

    # ======================================================================
    heading("Programmer errors")
    # ======================================================================

    db, *_ = _film_graph()

    msg = catch_into(passed, failed,
                     "where() with non-Predicate raises TypeError",
                     TypeError,
                     lambda: query(_products()).where(lambda n: True))
    check(passed, failed, "Predicate" in msg,    "TypeError names Predicate")
    check(passed, failed, "FieldEquals" in msg,  "TypeError gives a concrete example")

    msg2 = catch_into(passed, failed,
                      "nodes() with string raises TypeError",
                      TypeError,
                      lambda: query(db).nodes("Film"))
    check(passed, failed, "type" in msg2,        "TypeError for nodes() string arg mentions 'type'")

    catch_into(passed, failed,
               "nodes() with node instance raises TypeError",
               TypeError,
               lambda: query(db).nodes(Film({})))

    catch_into(passed, failed,
               "NodeIndex getitem missing raises KeyError",
               KeyError,
               lambda: NodeIndex(_products(), "sku")["ZZZZ"])

    # ======================================================================
    heading("Set operations: union, intersect, difference")
    # ======================================================================
    # Identity defaults to _key when present, id() otherwise.
    # All three are generators — results are lazy until consumed.

    db_a, kubrick, hitchcock, shining, clockwork, psycho = _film_graph()

    # Second graph uses the same prefix so _key values align for set operations.
    # shining appears in both (same _key); jaws is unique to db_b.
    db_b = FilmGraph({"_key": "films"})
    kubrick_b  = db_b.ensure(Director, "kubrick",   name="Stanley Kubrick")
    spielberg  = db_b.ensure(Director, "spielberg", name="Steven Spielberg")
    shining_b  = db_b.ensure(Film, "shining",   title="The Shining",       year=1980)
    jaws       = db_b.ensure(Film, "jaws",       title="Jaws",              year=1975)
    shining_b["director"] = kubrick_b
    jaws["director"]      = spielberg

    films_a = list(query(db_a).nodes(Film))   # shining, clockwork, psycho
    films_b = list(query(db_b).nodes(Film))   # shining_b, jaws

    # union — all unique films from both graphs
    u = NodeList(union(films_a, films_b))
    u_keys = {n.get("_key") for n in u}
    check(passed, failed, len(u) == 4,                                   "union() count")
    check(passed, failed, "films/shining" in u_keys,                     "union() contains shining (from a, not duplicated)")
    check(passed, failed, "films/jaws" in u_keys,                        "union() contains jaws (from b only)")
    check(passed, failed, "films/clockwork" in u_keys,                   "union() contains clockwork (from a only)")

    # union with custom key — by title field
    u_custom = NodeList(union(films_a, films_b, key=lambda n: n.get("title")))
    check(passed, failed, len(u_custom) == 4,                            "union() with custom key")

    # intersect — only films in both (shining appears in both by _key)
    i = NodeList(intersect(films_a, films_b))
    check(passed, failed, len(i) == 1,                                   "intersect() count")
    check(passed, failed, i[0].get("_key") == "films/shining",           "intersect() correct film")
    check(passed, failed, i[0] is shining,                               "intersect() yields node from a, not b")

    # intersect — empty when no overlap
    check(passed, failed,
          NodeList(intersect(films_b, [psycho])) == [],                   "intersect() empty when no overlap")

    # difference — films in a but not b
    d = NodeList(difference(films_a, films_b))
    d_keys = {n.get("_key") for n in d}
    check(passed, failed, len(d) == 2,                                   "difference() count")
    check(passed, failed, "films/clockwork" in d_keys,                   "difference() contains clockwork")
    check(passed, failed, "films/psycho" in d_keys,                      "difference() contains psycho")
    check(passed, failed, "films/shining" not in d_keys,                 "difference() excludes shining")

    # difference reversed — films in b but not a
    d_rev = NodeList(difference(films_b, films_a))
    check(passed, failed, len(d_rev) == 1,                               "difference() reversed count")
    check(passed, failed, d_rev[0].get("_key") == "films/jaws",          "difference() reversed correct film")

    # generators are lazy — union over two queries, neither materialised
    lazy_union = union(
        query(db_a).nodes(Film),
        query(db_b).nodes(Film),
    )
    check(passed, failed, hasattr(lazy_union, "__next__") or hasattr(lazy_union, "__iter__"),
          "union() result is iterable (lazy generator)")

    # set operations feed into Graph.from_nodes()
    result_graph = FilmGraph.from_nodes(
        intersect(query(db_a).nodes(Film), query(db_b).nodes(Film))
    )
    check(passed, failed, isinstance(result_graph, FilmGraph),            "from_nodes(intersect()) returns FilmGraph")
    check(passed, failed, len(result_graph["films"]) == 1,                "from_nodes(intersect()) correct film count")
    check(passed, failed, result_graph["films"][0].get("_key") == "films/shining",
          "from_nodes(intersect()) correct film in result graph")

    union_graph = FilmGraph.from_nodes(
        union(query(db_a).nodes(Film), query(db_b).nodes(Film))
    )
    check(passed, failed, len(union_graph["films"]) == 4,                 "from_nodes(union()) correct film count")

    return len(passed), len(failed)


if __name__ == "__main__":
    from _helpers import summary
    p, f = run()
    summary([" " * p], [" " * f])
