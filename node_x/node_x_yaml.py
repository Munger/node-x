## @file node_x_yaml.py
##
## @brief YAML serialisation companion for node_x.
##
## Provides ``dump()`` and ``load()`` that map directly onto
## ``Serialisable.to_plain()`` / ``Serialisable.deserialise()`` so callers
## get YAML in, YAML out without touching the core library.
##
## This is a standalone module — import it alongside node_x.py with no
## package structure or ``__init__.py`` required::
##
##     import node_x_yaml
##     text  = node_x_yaml.dump(root_node)
##     clone = node_x_yaml.load(DecadeNode, text)
##
## PyYAML is the only external dependency.  The module raises
## ``ImportError`` with a helpful message if PyYAML is absent so that
## users who never call this file are not affected.
##
## YAML vs JSON trade-offs
## -----------------------
## YAML is more readable for human inspection and diff review, handles
## multi-line strings cleanly, and supports inline comments (though
## ``dump()`` does not emit them).  For machine-to-machine transport or
## storage where file size matters, JSON is preferable — use
## ``Serialisable.to_pretty_json()`` or ``json.dumps(node.serialise(deep=True))``.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/Munger/node-x
## @par Licence: MIT

# ============================================================================
# Imports
# ============================================================================

from __future__ import annotations

from typing import Any, Optional, Dict, Type, TypeVar

# node_x_yaml ships as part of the same package as node_x and shares its
# version.  Importing here means there is still only one version string in
# the entire codebase: the one in pyproject.toml.
try:
    from . import __version__  # noqa: F401  # package mode
except ImportError:
    from node_x import __version__  # noqa: F401  # standalone mode

# Defer the PyYAML import so that the rest of node_x is unaffected when this
# file is never imported.  Only callers who actually use this module pay the
# import cost.
try:
    import yaml
except ImportError as _yaml_missing:
    raise ImportError(
        "node_x_yaml requires PyYAML.  Install it with:\n"
        "    pip install pyyaml\n"
        "or add 'pyyaml' to your project dependencies."
    ) from _yaml_missing

# T is the concrete Serialisable subclass returned by load().
T = TypeVar("T")

# ============================================================================
# Public API
# ============================================================================


def dump(node: Any, *, default_flow_style: bool = False, indent: int = 2) -> str:
    ## @brief Serialise a Serialisable node tree to a YAML string.
    ##
    ## Calls ``node.to_plain()`` to produce a plain Python structure (with
    ## ``$ref`` markers for Graph cross-references) and then passes
    ## that to ``yaml.dump()``.  The result is a portable YAML document that
    ## can be stored, diffed, or passed to ``load()``.
    ##
    ## ``default_flow_style=False`` (the default) produces block YAML —
    ## one key per line, easy to read and diff.  Pass ``True`` for compact
    ## inline notation, which is closer to JSON but less readable.
    ##
    ## @param node               A ``Serialisable`` node instance.
    ## @param default_flow_style Pass ``True`` for inline (compact) YAML.
    ## @param indent             Number of spaces per indent level (default 2).
    ## @return YAML document string, always ending with a newline.
    ## @raise AttributeError  If *node* does not have a ``to_plain()`` method.

    plain = node.to_plain()
    # allow_unicode=True keeps non-ASCII characters (artist names, chart titles)
    # as literal UTF-8 rather than escape sequences — far more readable.
    return yaml.dump(
        plain,
        default_flow_style=default_flow_style,
        indent=indent,
        allow_unicode=True,
        sort_keys=False,  # preserve insertion order set by to_plain()
    )


def load(cls: Type[T], text: str) -> T:
    ## @brief Reconstruct a node tree from a YAML string.
    ##
    ## Parses *text* with ``yaml.safe_load()`` (no Python-object tags, no
    ## arbitrary code execution) and passes the resulting plain structure to
    ## ``cls.deserialise()``.  ``$ref`` markers in the YAML are resolved during
    ## restore exactly as they would be from a JSON round-trip.
    ##
    ## @param cls   The ``Serialisable`` subclass to restore as (e.g. Timeline).
    ## @param text  YAML document string produced by ``dump()`` or by hand.
    ## @return A fully reconstructed instance of *cls*.
    ## @raise yaml.YAMLError  If *text* is not valid YAML.
    ## @raise TypeError       If *cls* does not have a ``deserialise()`` classmethod.
    ## @raise KeyError        If a ``$ref`` in the YAML cannot be resolved.

    plain = yaml.safe_load(text)
    # safe_load is mandatory — never use yaml.load() with untrusted input.
    # It produces only Python dicts, lists, strings, ints, floats, bools,
    # and None, which is exactly what deserialise() expects.
    return cls.deserialise(plain)
