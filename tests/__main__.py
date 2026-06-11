#!/usr/bin/env python3
## @file __main__.py
## @brief Test runner for the node_x unit test suite.
##
## Imports each module's ``run()`` function, iterates all five in order,
## collects pass/fail counts, and prints a cross-module summary.
## Exits with code 1 if any test failed.
##
## Usage:
##     ./run.sh                                   # run all
##     python3 tests/unit-tests/node-x/__main__.py  # direct

from __future__ import annotations

import sys
from pathlib import Path
from typing import List, Tuple

# Ensure the project root is on sys.path for node_x imports
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Ensure the test package dir is on sys.path for sibling imports
_PKG_DIR = str(Path(__file__).parent)
if _PKG_DIR not in sys.path:
    sys.path.insert(0, _PKG_DIR)

import _helpers
import test_node
import test_nodelist
import test_serial
import test_concurrency
import test_subclass
import test_stream
import test_graph
import test_yaml


def main() -> None:
    all_passed: List[str] = []
    all_failed: List[str] = []

    modules = [
        ("Node", test_node),
        ("NodeList", test_nodelist),
        ("Serialisable", test_serial),
        ("Concurrency", test_concurrency),
        ("Subclass", test_subclass),
        ("Stream", test_stream),
        ("Graph", test_graph),
        ("YAML", test_yaml),
    ]

    for name, mod in modules:
        print(f"=== {name} ===")
        p, f = mod.run()
        all_passed.append(f"  {name}: {p} passed")
        if f:
            all_failed.append(f"  {name}: {f} failed")

    for line in all_passed:
        print(line)
    for line in all_failed:
        print(line)

    total_p = sum(int(line.split()[-2]) for line in all_passed)
    total_f = sum(int(line.split()[-2]) for line in all_failed) if all_failed else 0

    print()
    print(f"  TOTAL  Passed: {total_p}  Failed: {total_f}")
    if total_f:
        sys.exit(1)


if __name__ == "__main__":
    main()
