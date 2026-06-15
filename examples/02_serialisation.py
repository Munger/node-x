## @file 02_serialisation.py
##
## @brief Serialising a recipe card to JSON and restoring it.
##
## Covers: Serialisable, SerialisableList, node_fields, list_fields,
## restore_via_payload, serialise(deep=True), deserialise(), JSON round-trip.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/Munger/node-x
## @par Licence: MIT

import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from node_x import Node, Serialisable, SerialisableList


# ---------------------------------------------------------------------------
# Node subclasses
# ---------------------------------------------------------------------------

class Ingredient(Serialisable, Node):
    # restore_via_payload = True bypasses __init__ on deserialise.
    # Use this whenever __init__ has side effects or required arguments.
    restore_via_payload = True


class Step(Serialisable, Node):
    restore_via_payload = True


class Recipe(Serialisable, Node):
    restore_via_payload = True

    # node_fields  — single child nodes, restored as their declared type.
    # list_fields  — typed NodeList children, restored element by element.
    list_fields = {
        "ingredients": (SerialisableList, Ingredient),
        "steps":       (SerialisableList, Step),
    }


# ---------------------------------------------------------------------------
# Build a recipe in memory
# ---------------------------------------------------------------------------

recipe = Recipe({"title": "Scrambled Eggs", "servings": 2})

recipe["ingredients"] = SerialisableList([
    Ingredient({"item": "eggs",   "amount": 4,   "unit": "whole"}),
    Ingredient({"item": "butter", "amount": 15,  "unit": "g"}),
    Ingredient({"item": "milk",   "amount": 30,  "unit": "ml"}),
    Ingredient({"item": "salt",   "amount": 0.5, "unit": "tsp"}),
])

recipe["steps"] = SerialisableList([
    Step({"order": 1, "text": "Crack eggs into a bowl and whisk with milk."}),
    Step({"order": 2, "text": "Melt butter in a pan over low heat."}),
    Step({"order": 3, "text": "Pour in egg mixture and stir gently until just set."}),
])

print(f"Built: {recipe['title']} (serves {recipe['servings']})")
print(f"  {len(recipe['ingredients'])} ingredients, {len(recipe['steps'])} steps")


# ---------------------------------------------------------------------------
# Serialise to a plain dict, then to JSON
# ---------------------------------------------------------------------------

# deep=True walks the entire subtree; the result is JSON-safe.
# Scalars are emitted before nested structures so diffs stay readable.
snapshot = recipe.serialise(deep=True)

json_text = json.dumps(snapshot, indent=2)
print(f"\nJSON ({len(json_text)} bytes):")
print(json_text)


# ---------------------------------------------------------------------------
# Restore from the JSON
# ---------------------------------------------------------------------------

data = json.loads(json_text)

# deserialise() rebuilds the full typed graph: Recipe, Ingredient, Step
# instances are reconstructed — not plain dicts.
restored = Recipe.deserialise(data)

print(f"\nRestored: {restored['title']} (serves {restored['servings']})")

# Confirm child types came back correctly.
assert isinstance(restored["ingredients"][0], Ingredient), "Expected Ingredient"
assert isinstance(restored["steps"][0], Step), "Expected Step"

print("Ingredients:")
for ing in restored["ingredients"]:
    print(f"  {ing['amount']} {ing['unit']} {ing['item']}")

print("Steps:")
for step in restored["steps"]:
    print(f"  {step['order']}. {step['text']}")
