#!/usr/bin/env bash
## @file run.sh
## @brief Run the node_x unit test suite.
##
## Usage:
##     ./run.sh
##
## No venv or dependencies required beyond stdlib.
## Passes through any arguments to the test runner.

cd "$(dirname "$0")" && exec python3 __main__.py "$@"
