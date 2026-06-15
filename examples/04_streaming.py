## @file 04_streaming.py
##
## @brief Lazy node generation from a data source using Stream.
##
## Covers: Stream.stream(), laziness (records read only on iteration),
## early exit without loading the full source.
##
## stream() is just a generator that reads from a source and yields nodes.
## The caller decides what to do with them — collect, filter, or stop early.
## Nothing is read until the caller iterates.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/Munger/node-x
## @par Licence: MIT

import csv
import io
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from node_x import Node, NodeList, Stream


# ---------------------------------------------------------------------------
# Data source — a CSV of products in a coffee shop catalogue
# ---------------------------------------------------------------------------

CSV_DATA = """\
sku,name,category,price
A001,Earl Grey,Tea,8.50
A002,Chamomile,Tea,6.99
A003,Espresso Blend,Coffee,12.99
A004,Dark Roast,Coffee,14.99
A005,Cold Brew Concentrate,Coffee,18.00
"""

_rows_read = 0


# ---------------------------------------------------------------------------
# Node subclasses
# ---------------------------------------------------------------------------

class ProductNode(Node):
    pass


class CatalogueNode(Stream, Node):
    # stream() reads the CSV row by row and yields one ProductNode per row.
    # It runs only when the caller iterates — not at construction time.
    def stream(self, data=None):
        global _rows_read
        for row in csv.DictReader(io.StringIO(CSV_DATA)):
            _rows_read += 1
            yield ProductNode({
                "sku":      row["sku"],
                "name":     row["name"],
                "category": row["category"],
                "price":    float(row["price"]),
            })


# ---------------------------------------------------------------------------
# Build the catalogue — no rows are read yet
# ---------------------------------------------------------------------------

print("Building catalogue (no rows read yet):")
catalogue = CatalogueNode({"source": "products.csv"})
print(f"  source: {catalogue['source']}")
print(f"  rows read: {_rows_read}")   # 0


# ---------------------------------------------------------------------------
# Stream all products — reads every row
# ---------------------------------------------------------------------------

print("\nLoading all products:")
products = NodeList()
for product in catalogue.stream():
    products.append(product)
    print(f"  {product['sku']}  {product['name']:<26} £{product['price']:.2f}")
print(f"  rows read: {_rows_read}")   # 5


# ---------------------------------------------------------------------------
# Early exit — stop as soon as we find what we need
# ---------------------------------------------------------------------------

# Rows after the match are never read.  The generator is just abandoned.

print("\nFirst Coffee under £15.00:")
_rows_read = 0

found = None
for product in catalogue.stream():
    if product["category"] == "Coffee" and product["price"] < 15.00:
        found = product
        break

print(f"  {found['name']} £{found['price']:.2f}")
print(f"  rows read: {_rows_read}")   # 3 — stopped after Espresso Blend
