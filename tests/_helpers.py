## @file _helpers.py
## @brief Shared test utilities for the node_x test suite.
##
## Provides lightweight assertion helpers that record results into
## caller-supplied ``passed`` / ``failed`` lists rather than using
## ``unittest`` or ``pytest``.  No external dependencies.

from __future__ import annotations

import sys
from typing import Any, Callable, Optional, Tuple, Type


def catch(
    exc_type: Type[BaseException],
    func: Callable[[], Any],
) -> Tuple[BaseException, str]:
    ## @brief Assert that *func* raises *exc_type*.
    ##
    ## Returns the ``(exception, message)`` tuple so the caller can
    ## inspect the message text.  Raises ``AssertionError`` (not caught)
    ## if the expected exception is not raised.
    ##
    ## @param exc_type  The expected exception class.
    ## @param func      Zero-argument callable to invoke.
    ## @return ``(exception_instance, str(exception))``.
    ## @raise AssertionError  If no exception or wrong type raised.

    try:
        func()
    except exc_type as e:
        msg = str(e)
        return e, msg
    except BaseException as e:
        raise AssertionError(
            f"Expected {exc_type.__name__}, got {type(e).__name__}({e})"
        ) from e
    else:
        raise AssertionError(
            f"Expected {exc_type.__name__}, no exception raised"
        )


def check(
    passed: list[str],
    failed: list[str],
    ok: bool,
    msg: str,
) -> None:
    ## @brief Record a boolean test result and print it immediately.
    ##
    ## Appends *msg* to ``passed`` on success or ``failed`` on failure.
    ## Every test prints its outcome so the user always sees what was tested.
    ##
    ## @param passed  Mutable list of passing-test descriptions.
    ## @param failed  Mutable list of failing-test descriptions.
    ## @param ok      ``True`` if the test passed.
    ## @param msg     Human-readable description of the test.

    if ok:
        passed.append(msg)
        print(f"  PASS  {msg}")
    else:
        failed.append(msg)
        print(f"  FAIL  {msg}")


def catch_into(
    passed: list[str],
    failed: list[str],
    label: str,
    exc_type: Type[BaseException],
    func: Callable[[], Any],
) -> str:
    ## @brief Assert *func* raises *exc_type* and record the outcome.
    ##
    ## Like ``catch()`` but records the result into the pass/fail lists
    ## and returns the exception message for further inspection.  An
    ## empty message is itself a failure.
    ##
    ## @param passed    Mutable list of passing-test descriptions.
    ## @param failed    Mutable list of failing-test descriptions.
    ## @param label     Short label for this test (shown in output).
    ## @param exc_type  Expected exception class.
    ## @param func      Zero-argument callable to invoke.
    ## @return The exception message text, or ``""`` on failure.

    try:
        exc, msg = catch(exc_type, func)
    except AssertionError as e:
        failed.append(f"{label}: {e}")
        print(f"  FAIL  {label}: {e}")
        return ""

    if not msg:
        failed.append(f"{label}: empty exception message")
        print(f"  FAIL  {label}: empty exception message")
        return ""

    passed.append(
        f"{label}: {exc_type.__name__}: {msg.split(chr(10))[0]}"
    )
    print(f"  PASS  {label}: {exc_type.__name__}: {msg.split(chr(10))[0]}")
    return msg


def does_not_raise(
    passed: list[str],
    failed: list[str],
    label: str,
    func: Callable[[], Any],
) -> None:
    ## @brief Assert *func* completes without any exception.
    ##
    ## @param passed  Mutable list of passing-test descriptions.
    ## @param failed  Mutable list of failing-test descriptions.
    ## @param label   Short label for this test.
    ## @param func    Zero-argument callable to invoke.

    try:
        func()
    except BaseException as e:
        failed.append(f"{label}: unexpected {type(e).__name__}({e})")
        print(f"  FAIL  {label}: unexpected {type(e).__name__}({e})")
    else:
        passed.append(f"{label}")
        print(f"  PASS  {label}")


def heading(title: str) -> None:
    ## @brief Print a section heading to stdout.
    ## @param title  The heading text.

    print()
    print(f"--- {title}")


def summary(passed: list[str], failed: list[str]) -> Tuple[int, int]:
    ## @brief Print pass/fail summary and optionally exit with code 1.
    ##
    ## Designed for standalone module use (not via the multi-module
    ## runner) so each test module can be run directly.
    ##
    ## @param passed  List of passing-test descriptions.
    ## @param failed  List of failing-test descriptions.
    ## @return ``(len(passed), len(failed))``.

    total = len(passed) + len(failed)
    print()
    print("-" * 72)
    print(f"  Passed: {len(passed)}  Failed: {len(failed)}  Total: {total}")
    if failed:
        print()
        print("  FAILURES:")
        for f in failed:
            print(f"    - {f}")
        sys.exit(1)
    else:
        print("  All tests passed.")
    return len(passed), len(failed)


# Backward-compatible aliases for existing test modules.
check_catch = catch_into
check_does_not_raise = does_not_raise
