## @file 01_nodes_and_lists.py
##
## @brief Building a shop inventory with Node and NodeList.
##
## Covers: node construction, attribute vs item access, NodeList,
## tree walking with _tree_iter(), reserved-name protection.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/Munger/node-x
## @par Licence: MIT

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from node_x import Node, NodeList


# ---------------------------------------------------------------------------
# Node subclasses
# ---------------------------------------------------------------------------

class Product(Node):
    _children = ()

    def label(self) -> str:
        return f"{self['name']} — £{self['price']:.2f} ({self['stock']} in stock)"


class Category(Node):
    _children = ("products",)


class Inventory(Node):
    _children = ("categories",)


# ---------------------------------------------------------------------------
# Build the tree
# ---------------------------------------------------------------------------

inventory = Inventory({"store": "Corner Shop"})

fruit = Category({"name": "Fruit"})
fruit["products"] = NodeList([
    Product({"name": "Apple",  "price": 0.30, "stock": 120}),
    Product({"name": "Banana", "price": 0.20, "stock": 80}),
    Product({"name": "Mango",  "price": 1.10, "stock": 15}),
])

dairy = Category({"name": "Dairy"})
dairy["products"] = NodeList([
    Product({"name": "Milk",   "price": 1.05, "stock": 40}),
    Product({"name": "Butter", "price": 1.60, "stock": 22}),
])

inventory["categories"] = NodeList([fruit, dairy])


# ---------------------------------------------------------------------------
# Access — dict-style and attribute-style are identical
# ---------------------------------------------------------------------------

print(f"Store: {inventory['store']}")
print(f"Store: {inventory.store}")          # same thing

print(f"\nFirst category: {fruit['name']}")
print(f"First product:  {fruit['products'][0]['name']}")


# ---------------------------------------------------------------------------
# Iterate a NodeList like a plain list
# ---------------------------------------------------------------------------

print("\nFruit products:")
for p in fruit["products"]:
    print(f"  {p.label()}")

cheapest = min(fruit["products"], key=lambda p: p["price"])
print(f"Cheapest fruit: {cheapest['name']}")


# ---------------------------------------------------------------------------
# Walk the entire tree depth-first with _tree_iter()
# ---------------------------------------------------------------------------

print("\nAll nodes (depth-first):")
for node in inventory._tree_iter():
    if "name" in node:
        indent = "  " if isinstance(node, Category) else "    "
        print(f"{indent}{node['name']}")


# ---------------------------------------------------------------------------
# Reserved-name protection
# ---------------------------------------------------------------------------

# Node automatically reserves all method names (update, pop, clear, …).
# Attempting to use one as a payload key raises KeyError immediately,
# so incoming data can never shadow class behaviour.

try:
    Product({"name": "Widget", "price": 9.99, "update": "oops"})
except KeyError as exc:
    print(f"\nReserved-name protection caught: {exc}")
