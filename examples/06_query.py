## @file 06_query.py
##
## @brief Querying node collections and graphs with node_x_query.
##
## Covers: composable predicates, flat queries, graph traversal
## (.nodes, .traverse, .back), NodeIndex, standalone functions,
## IsType, FieldMatches, and set operations (union, intersect, difference).
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/Munger/node-x
## @par Licence: MIT

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from node_x import Node, NodeList, Graph, Serialisable, SerialisableList
from node_x.node_x_query import (
    query, where, group_by, order_by,
    union, intersect, difference,
    NodeIndex,
    FieldEquals, FieldLt, FieldGt, FieldIn, FieldMatches, IsType,
)


# ---------------------------------------------------------------------------
# Part 1 — Flat queries over a NodeList
# ---------------------------------------------------------------------------
#
# Build a product catalogue as a plain NodeList.  node_x_query treats any
# iterable of nodes as a queryable collection.

products = NodeList([
    Node({"sku": "A001", "name": "Earl Grey",             "category": "Tea",    "price": 8.50,  "stock": 120}),
    Node({"sku": "A002", "name": "Chamomile",             "category": "Tea",    "price": 6.99,  "stock": 45}),
    Node({"sku": "A003", "name": "Espresso Blend",        "category": "Coffee", "price": 12.99, "stock": 80}),
    Node({"sku": "A004", "name": "Dark Roast",            "category": "Coffee", "price": 14.99, "stock": 30}),
    Node({"sku": "A005", "name": "Cold Brew Concentrate", "category": "Coffee", "price": 18.00, "stock": 12}),
    Node({"sku": "A006", "name": "Peppermint",            "category": "Tea",    "price": 5.49,  "stock": 200}),
])

print("=== Part 1 — Flat queries ===\n")

# Predicates are standalone objects — store and compose them freely.
is_coffee   = FieldEquals("category", "Coffee")
is_tea      = FieldEquals("category", "Tea")
under_10    = FieldLt("price", 10.00)
low_stock   = FieldLt("stock", 50)

# All coffees, cheapest first.
coffees = query(products).where(is_coffee).order_by("price").all()
print("Coffees (cheapest first):")
for p in coffees:
    print(f"  {p['name']:<26} £{p['price']:.2f}")

# Teas under £8.
cheap_teas = query(products).where(is_tea & under_10).all()
print(f"\nTeas under £10.00: {[p['name'] for p in cheap_teas]}")

# First item with low stock.
first_low = query(products).where(low_stock).order_by("stock").first()
print(f"Lowest stock item:  {first_low['name']} ({first_low['stock']} units)")

# Items matching a name pattern.
blends = query(products).where(FieldMatches("name", r"Blend|Roast")).all()
print(f"Blends and roasts:  {[p['name'] for p in blends]}")

# Count by category.
print(f"\nCoffee count: {query(products).where(is_coffee).count()}")
print(f"Tea count:    {query(products).where(is_tea).count()}")


# ---------------------------------------------------------------------------
# Part 2 — group_by and standalone functions
# ---------------------------------------------------------------------------

print("\n=== Part 2 — Group and sort ===\n")

# Group by category.
by_category = query(products).group_by(lambda n: n["category"])
for cat, items in sorted(by_category.items()):
    names = [p["name"] for p in order_by(items, "price")]
    print(f"{cat}: {', '.join(names)}")

# Standalone functions work on any iterable — no query builder needed.
teas = where(products, is_tea)
print(f"\nStandalone where() → {len(teas)} teas")
print(f"Standalone order_by() → {[p['name'] for p in order_by(teas, 'price')]}")


# ---------------------------------------------------------------------------
# Part 3 — NodeIndex for O(1) lookup
# ---------------------------------------------------------------------------

print("\n=== Part 3 — NodeIndex ===\n")

# Build the index once; look up as many times as needed.
sku_index = NodeIndex(products, "sku")

print(f"index['A003'] → {sku_index['A003']['name']}")
print(f"'A001' in index → {'A001' in sku_index}")
print(f"'ZZZZ' in index → {'ZZZZ' in sku_index}")

# Index on a non-unique field returns all matches via get_all().
cat_index = NodeIndex(products, "category")
coffee_names = [p["name"] for p in cat_index.get_all("Coffee")]
print(f"Coffees via category index: {coffee_names}")


# ---------------------------------------------------------------------------
# Part 4 — Graph traversal
# ---------------------------------------------------------------------------
#
# The same query builder works on Graph nodes.  .nodes(Type) enters a
# Graph's list_fields.  .traverse(field) follows a node_fields reference.
# .back() returns to the node before the last traversal step.

print("\n=== Part 4 — Graph traversal ===\n")


class Author(Serialisable, Node):
    restore_via_payload = True


class Book(Serialisable, Node):
    restore_via_payload = True
    node_fields = {"author": Author}


class Library(Graph):
    list_fields = {
        "authors": (SerialisableList, Author),
        "books":   (SerialisableList, Book),
    }


lib = Library({"_key": "library"})

orwell  = lib.ensure(Author, "orwell",  name="George Orwell",  born=1903)
huxley  = lib.ensure(Author, "huxley",  name="Aldous Huxley",  born=1894)
dickens = lib.ensure(Author, "dickens", name="Charles Dickens", born=1812)

b1 = lib.ensure(Book, "1984",        title="1984",                year=1949, genre="Dystopia")
b2 = lib.ensure(Book, "animal-farm", title="Animal Farm",         year=1945, genre="Satire")
b3 = lib.ensure(Book, "brave-new",   title="Brave New World",     year=1932, genre="Dystopia")
b4 = lib.ensure(Book, "great-exp",   title="Great Expectations",  year=1861, genre="Fiction")
b5 = lib.ensure(Book, "bleak-house", title="Bleak House",         year=1853, genre="Fiction")

b1["author"] = orwell
b2["author"] = orwell
b3["author"] = huxley
b4["author"] = dickens
b5["author"] = dickens

# All books, alphabetically.
all_books = query(lib).nodes(Book).order_by("title").all()
print("All books:")
for b in all_books:
    print(f"  {b['title']:<28} ({b['year']}) — {b['author']['name']}")

# Dystopias only.
dystopias = query(lib).nodes(Book).where(FieldEquals("genre", "Dystopia")).all()
print(f"\nDystopias: {[b['title'] for b in dystopias]}")

# Books by authors born before 1900 — traverse director → filter → back to books.
old_authors = (query(lib)
               .nodes(Book)
               .traverse("author")
               .where(FieldLt("born", 1900))
               .back()
               .order_by("year")
               .all())
print(f"\nBooks by authors born before 1900:")
for b in old_authors:
    print(f"  {b['title']} ({b['author']['name']}, b. {b['author']['born']})")

# Find the author of "Animal Farm" directly.
author = (query(lib)
          .nodes(Book)
          .where(FieldEquals("title", "Animal Farm"))
          .traverse("author")
          .first())
print(f"\nAuthor of 'Animal Farm': {author['name']}")

# All books with a FieldIn predicate.
selected = (query(lib)
            .nodes(Book)
            .where(FieldIn("genre", ["Dystopia", "Satire"]))
            .order_by("year")
            .all())
print(f"\nDystopias and Satires: {[b['title'] for b in selected]}")


# ---------------------------------------------------------------------------
# Part 5 — IsType and FieldMatches
# ---------------------------------------------------------------------------
#
# IsType matches nodes that are instances of a given class — useful when a
# collection holds mixed node types.  FieldMatches filters by regex.

print("\n=== Part 5 — IsType and FieldMatches ===\n")

# Mix authors and books into one flat list to show IsType filtering.
mixed = NodeList([orwell, huxley, dickens, b1, b2, b3, b4, b5])

authors_only = query(mixed).where(IsType(Author)).all()
books_only   = query(mixed).where(IsType(Book)).all()
print(f"IsType(Author) → {[n['name'] for n in authors_only]}")
print(f"IsType(Book)   → {[n['title'] for n in books_only]}")

# Combine IsType with other predicates.
old_authors_only = query(mixed).where(IsType(Author) & FieldLt("born", 1900)).all()
print(f"Authors born before 1900: {[n['name'] for n in old_authors_only]}")

# FieldMatches — regex search on a field value.
# Titles starting with a digit.
digit_titles = query(lib).nodes(Book).where(FieldMatches("title", r"^\d")).all()
print(f"\nTitles starting with a digit: {[b['title'] for b in digit_titles]}")

# Case-insensitive search for a word anywhere in the title.
world_books = query(lib).nodes(Book).where(FieldMatches("title", r"(?i)world")).all()
print(f"Titles containing 'world':    {[b['title'] for b in world_books]}")

# Combine IsType and FieldMatches — authors whose name starts with a vowel.
vowel_authors = query(mixed).where(IsType(Author) & FieldMatches("name", r"^[AEIOU]")).all()
print(f"Authors starting with a vowel: {[n['name'] for n in vowel_authors]}")


# ---------------------------------------------------------------------------
# Part 6 — Set operations: union, intersect, difference
# ---------------------------------------------------------------------------
#
# union, intersect, and difference work on any iterables of nodes — lists,
# queries, streams.  Identity defaults to _key; pass key= to override.
# All three are generators: nothing runs until consumed.

print("\n=== Part 6 — Set operations ===\n")

# Two library collections with some overlap.
# lib_a: the library above (Orwell, Huxley, Dickens)
# lib_b: a second library that shares some books but has extras.
lib_b = Library({"_key": "library"})
huxley_b  = lib_b.ensure(Author, "huxley",   name="Aldous Huxley",   born=1894)
twain     = lib_b.ensure(Author, "twain",     name="Mark Twain",       born=1835)
b3_b      = lib_b.ensure(Book, "brave-new",  title="Brave New World",  year=1932, genre="Dystopia")
b6        = lib_b.ensure(Book, "huck-finn",  title="Huckleberry Finn", year=1884, genre="Fiction")
b3_b["author"] = huxley_b
b6["author"]   = twain

books_a = query(lib).nodes(Book)    # 1984, Animal Farm, Brave New World, Great Exp, Bleak House
books_b = query(lib_b).nodes(Book)  # Brave New World, Huckleberry Finn

# Union — all books from either library, no duplicates.
all_books = NodeList(union(books_a, books_b))
print(f"Union ({len(all_books)} books):")
for b in sorted(all_books, key=lambda n: n["title"]):
    print(f"  {b['title']}")

# Intersect — books in both libraries (shared by _key).
shared = NodeList(intersect(books_a, books_b))
print(f"\nIntersect — in both libraries: {[b['title'] for b in shared]}")

# Difference — books in lib but not lib_b.
lib_only = NodeList(difference(books_a, books_b))
print(f"Difference — in lib only: {[b['title'] for b in lib_only]}")

# Set ops feed straight into Graph.from_nodes() to produce a typed result.
shared_lib = Library.from_nodes(intersect(books_a, books_b))
print(f"\nfrom_nodes(intersect): {len(shared_lib['books'])} book(s) — {shared_lib['books'][0]['title']}")

# Custom key — compare by title rather than _key (useful across namespaces).
lib_only_by_title = NodeList(
    difference(books_a, books_b, key=lambda n: n["title"])
)
print(f"Difference by title:  {[b['title'] for b in lib_only_by_title]}")
