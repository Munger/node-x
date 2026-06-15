## @file test_concurrency.py
##
## @brief Unit tests for ``Transaction``, ``WriteMutex``,
##        and the internal ``_RWLock``.
##
## Sections:
##
##   - **Transaction** — basic cross-node atomic read/write; single
##     node; 50-node stress test (no deadlock); interaction with
##     NodeList; zero-node no-op; duplicate-node de-duplication
##     (the constructor deduplicates by id() so no double-lock
##     occurs); exception-in-body (locks must be released even when
##     the body raises); ``Node.lock`` and ``NodeList.lock`` property
##     types.
##   - **WriteMutex** — basic reading() / write round-trip;
##     writer blocked while reading() is held; multiple concurrent
##     readers allowed simultaneously; re-entrant write from the same
##     thread; NodeList integration.
##   - **_RWLock** — same-thread reader does not block while that
##     thread holds the write lock (re-entrancy contract).
##
## The timing-based tests (writer-blocks, concurrent-readers) use
## conservative sleeps and a ≥3-of-5 threshold for the reader test
## to avoid flakiness on loaded systems.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from typing import Any, List, Optional, Tuple

_PKG_DIR = str(Path(__file__).resolve().parent)
_ROOT_DIR = str(Path(__file__).resolve().parent.parent)
for _d in (_PKG_DIR, _ROOT_DIR):
    if _d not in sys.path:
        sys.path.insert(0, _d)

from node_x import Node, NodeList, Transaction, WriteMutex
from node_x import _RWLock

from _helpers import check, check_catch, check_does_not_raise, heading


def run() -> Tuple[int, int]:
    ## @brief Execute all concurrency test sections and return pass/fail counts.
    ##
    ## Concurrency tests that spin real threads join before the next
    ## section begins, so the sections are effectively sequential from
    ## the test runner's perspective.
    ##
    ## @return ``(pass_count, fail_count)`` across all sections.

    passed: List[str] = []
    failed: List[str] = []

    # ------------------------------------------------------------------
    heading("Transaction: basic cross-node read/write")
    # ------------------------------------------------------------------
    # Transaction acquires locks in id()-sorted order so that
    # concurrent transactions on the same pair of nodes always agree on
    # acquisition order, preventing ABBA deadlock.

    a = Node({"x": 1})
    b = Node({"y": 2})

    with Transaction(a, b):
        a["x"] = a["x"] + 1
        b["y"] = b["y"] + 1

    check(passed, failed, a["x"] == 2 and b["y"] == 3,
          "Transaction allows cross-node read/write")

    # ------------------------------------------------------------------
    heading("Transaction: single node")
    # ------------------------------------------------------------------

    n = Node({"a": 1})
    with Transaction(n):
        n["a"] = n["a"] + 10

    check(passed, failed, n["a"] == 11,
          "Transaction with single node works")

    # ------------------------------------------------------------------
    heading("Transaction: many nodes (no deadlock)")
    # ------------------------------------------------------------------
    # 50 nodes acquired in a single transaction: verifies that the
    # id()-sorted acquisition scales without issue.

    nodes = [Node({"i": i}) for i in range(50)]
    with Transaction(*nodes):
        total = sum(n["i"] for n in nodes)
    check(passed, failed, total == sum(range(50)),
          "Transaction acquires 50 locks without deadlock")

    # ------------------------------------------------------------------
    heading("Transaction: with NodeList")
    # ------------------------------------------------------------------
    # Transaction accepts any object with a .lock property, including
    # NodeList instances.

    items = [Node({"x": i}) for i in range(5)]
    nl = NodeList(items)
    with Transaction(nl, items[0]):
        total = sum(n["x"] for n in nl)
    check(passed, failed, total == 10,
          "Transaction works with NodeList")

    # ------------------------------------------------------------------
    heading("Transaction: zero nodes and duplicate nodes")
    # ------------------------------------------------------------------
    # An empty Transaction must be a harmless no-op.  Passing the
    # same node twice must not cause a double-lock (the constructor
    # deduplicates by object identity via id()).

    check_does_not_raise(passed, failed,
                         "Transaction() with zero nodes is a no-op",
                         lambda: Transaction().__enter__().__exit__(None, None, None))

    shared_node = Node({"v": 1})
    with Transaction(shared_node, shared_node):
        shared_node["v"] = 2
    check(passed, failed, shared_node["v"] == 2,
          "Transaction deduplicates repeated node (no double-lock)")

    # ------------------------------------------------------------------
    heading("Transaction: exception in body releases locks")
    # ------------------------------------------------------------------
    # __exit__ must release all acquired locks even when the body raises
    # an exception.  After the exception is caught the nodes must be
    # fully usable again.

    exc_a = Node({"x": 0})
    exc_b = Node({"y": 0})
    try:
        with Transaction(exc_a, exc_b):
            exc_a["x"] = 1
            raise RuntimeError("deliberate")
    except RuntimeError:
        pass
    check_does_not_raise(passed, failed,
                         "locks released after exception in Transaction body",
                         lambda: exc_a.__setitem__("x", 2))
    check(passed, failed, exc_a["x"] == 2,
          "node remains usable after exception in Transaction")

    # ------------------------------------------------------------------
    heading("Node.lock and NodeList.lock properties")
    # ------------------------------------------------------------------
    # Both Node and NodeList expose a .lock property so that external
    # callers (including Transaction) can hold the lock across
    # operations without knowledge of the internal attribute name.

    import threading as _threading
    n_lock = Node({"a": 1})
    check(passed, failed, isinstance(n_lock.lock, _threading.RLock().__class__),
          "Node.lock returns an RLock instance")

    nl_lock = NodeList([Node()])
    check(passed, failed, isinstance(nl_lock.lock, _threading.RLock().__class__),
          "NodeList.lock returns an RLock instance")

    # ------------------------------------------------------------------
    heading("Transaction: blocks concurrent readers on WriteMutex nodes")
    # ------------------------------------------------------------------
    # Transaction must enter _write_guard for each node before
    # acquiring _lock.  For WriteMutex nodes this acquires the write
    # side of the RW lock, so concurrent reading() contexts block for
    # the duration of the transaction — matching the behaviour of a
    # plain mutation on such a node.

    class RWNode(WriteMutex, Node):
        pass

    rw_tx = RWNode({"val": 0})
    reader_ran_during_tx: List[bool] = []

    def tx_reader() -> None:
        with rw_tx.reading():
            reader_ran_during_tx.append(True)

    tx_done: List[bool] = []
    reader_thread_obj = threading.Thread(target=tx_reader)

    with Transaction(rw_tx):
        reader_thread_obj.start()
        time.sleep(0.05)
        # Reader should be blocked; record whether it has already run.
        ran_inside = len(reader_ran_during_tx) > 0
        rw_tx["val"] = 99
        tx_done.append(True)

    reader_thread_obj.join()
    check(passed, failed, not ran_inside,
          "Transaction blocks concurrent reading() on WriteMutex node")
    check(passed, failed, len(reader_ran_during_tx) == 1,
          "reader proceeds after Transaction exits")
    check(passed, failed, rw_tx["val"] == 99,
          "transaction write committed correctly")

    # ------------------------------------------------------------------
    heading("WriteMutex: basic read/write")
    # ------------------------------------------------------------------
    # reading() is a context manager that marks the object as being read.
    # Writers block until all active reading() contexts have exited.
    # Normal writes outside reading() contexts proceed immediately.

    class RWNode(WriteMutex, Node):
        pass

    n = RWNode({"counter": 0})
    with n.reading():
        val = n["counter"]
    check(passed, failed, val == 0,
          "WriteMutex reading() works")

    n["counter"] = 42
    check(passed, failed, n["counter"] == 42,
          "WriteMutex write after reading() works")

    # ------------------------------------------------------------------
    heading("WriteMutex: writer blocks during reading()")
    # ------------------------------------------------------------------
    # Starts a reader that holds reading() for 0.2 s, then starts a
    # writer 0.05 s later.  Samples the writer's completion flag at
    # 0.1 s (before the reader exits) to confirm the writer is still
    # blocked, then joins both threads to confirm the write eventually
    # completes.

    rw = RWNode({"val": 0})
    writer_finished: List[bool] = []

    def reader_thread() -> None:
        with rw.reading():
            time.sleep(0.2)

    def writer_thread() -> None:
        rw["val"] = 99
        writer_finished.append(True)

    t_reader = threading.Thread(target=reader_thread)
    t_writer = threading.Thread(target=writer_thread)

    t_reader.start()
    time.sleep(0.05)
    t_writer.start()
    time.sleep(0.05)

    writer_blocked = len(writer_finished) == 0
    t_reader.join()
    t_writer.join()

    check(passed, failed, writer_blocked,
          "writer blocks while reader holds reading()")
    check(passed, failed, rw["val"] == 99,
          "writer completes after reader exits")

    # ------------------------------------------------------------------
    heading("WriteMutex: multiple concurrent readers")
    # ------------------------------------------------------------------
    # Launches 5 threads each holding reading() for 0.1 s.  Tracks the
    # peak concurrent reader count.  Expects at least 3 to overlap,
    # giving headroom for scheduling jitter on slow systems.

    rw2 = RWNode({"val": 0})
    reader_count: List[int] = [0]
    max_concurrent: List[int] = [0]
    count_lock = threading.Lock()

    def concurrent_reader() -> None:
        with rw2.reading():
            with count_lock:
                reader_count[0] += 1
                max_concurrent[0] = max(max_concurrent[0], reader_count[0])
            time.sleep(0.1)
            with count_lock:
                reader_count[0] -= 1

    threads = [threading.Thread(target=concurrent_reader) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    check(passed, failed, max_concurrent[0] >= 3,
          "multiple concurrent readers allowed (saw at least 3 of 5)")

    # ------------------------------------------------------------------
    heading("WriteMutex: re-entrant write")
    # ------------------------------------------------------------------
    # A thread that already holds the write lock must be able to
    # re-enter _write_guard() without deadlocking.  This mirrors what
    # happens internally when mutating methods call other mutating
    # methods (e.g. merge() calling __setitem__).

    rw3 = RWNode({"a": 1})

    def reentrant_write(node: RWNode) -> None:
        with node._write_guard():
            node["a"] = 2
            with node._write_guard():
                node["b"] = 3

    check_does_not_raise(passed, failed,
                         "re-entrant write does not deadlock",
                         lambda: reentrant_write(rw3))
    check(passed, failed, rw3["a"] == 2 and rw3["b"] == 3,
          "re-entrant write produces correct values")

    # ------------------------------------------------------------------
    heading("WriteMutex: with NodeList")
    # ------------------------------------------------------------------

    class RWNodeList(WriteMutex, NodeList):
        pass

    rwl = RWNodeList()
    with rwl.reading():
        check(passed, failed, len(rwl) == 0,
              "WriteMutex NodeList reading() works")

    rwl.append(Node({"x": 1}))
    check(passed, failed, len(rwl) == 1,
          "WriteMutex NodeList write after reading() works")

    # ------------------------------------------------------------------
    heading("_RWLock: same-thread reader does not block writer on same thread")
    # ------------------------------------------------------------------
    # acquire_read() allows re-entry from the thread that holds the
    # write lock.  This permits a writer to call reading() on the same
    # object without deadlocking — an important property for methods
    # that acquire the write lock and then call helpers that enter
    # reading() internally.

    rw_inner = _RWLock()
    rw_inner.acquire_write()
    check_does_not_raise(passed, failed,
                         "acquire_read on same thread as writer does not block",
                         rw_inner.acquire_read)
    rw_inner.release_read()
    rw_inner.release_write()

    return len(passed), len(failed)
