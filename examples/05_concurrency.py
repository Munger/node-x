## @file 05_concurrency.py
##
## @brief Thread safety with WriteMutex and Transaction.
##
## Covers: WriteMutex (safe iteration under concurrent writes),
## Transaction (atomic cross-node operations, deadlock-free locking).
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/Munger/node-x
## @par Licence: MIT

import sys
import threading
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from node_x import Node, NodeList, WriteMutex, Transaction


# ---------------------------------------------------------------------------
# Part 1 — WriteMutex: safe iteration while another thread is writing
# ---------------------------------------------------------------------------
#
# By default a NodeList offers no protection against concurrent reads and
# writes.  Mix in WriteMutex and wrap iteration in a reading() context to
# block writers for the duration of the read — no changes needed in the
# writer code.

class LiveQueue(WriteMutex, NodeList):
    """A job queue that can be iterated safely under concurrent writes."""
    pass


class Job(Node):
    pass


queue = LiveQueue([
    Job({"id": i, "task": f"job-{i}", "done": False})
    for i in range(5)
])

results = []
reading_active = threading.Event()   # set by reader once inside reading()


def reader():
    """Iterate the queue inside a reading() context."""
    with queue.reading():
        reading_active.set()               # signal: read lock is now held
        time.sleep(0.05)                   # hold the lock so the writer queues up
        snapshot = [job["task"] for job in queue]
    results.extend(snapshot)


def writer():
    """Append a new job — blocks until the reader releases reading()."""
    reading_active.wait()                  # wait until reader holds the lock
    queue.append(Job({"id": 99, "task": "late-job", "done": False}))


# Start reader first, then writer.  The writer blocks on reading_active until
# the reader is inside queue.reading(), then tries to append and is held until
# the reader exits the context.
t_reader = threading.Thread(target=reader)
t_writer = threading.Thread(target=writer, daemon=True)

t_reader.start()
t_writer.start()

t_reader.join()
t_writer.join()

print("Part 1 — WriteMutex")
print(f"  reader saw {len(results)} jobs: {results}")
print(f"  queue now has {len(queue)} jobs (writer appended after read finished)")


# ---------------------------------------------------------------------------
# Part 2 — Transaction: atomic cross-node operations
# ---------------------------------------------------------------------------
#
# Transaction acquires locks on multiple nodes in id()-sorted order,
# eliminating ABBA deadlock regardless of how many concurrent transfers
# are in flight simultaneously.

class Account(Node):
    pass


alice   = Account({"owner": "Alice",   "balance": 1000})
bob     = Account({"owner": "Bob",     "balance": 500})
charlie = Account({"owner": "Charlie", "balance": 750})

transfer_log = []
log_lock = threading.Lock()


def transfer(src: Account, dst: Account, amount: int) -> None:
    """Move amount from src to dst atomically."""
    with Transaction(src, dst):
        if src["balance"] >= amount:
            src["balance"] -= amount
            dst["balance"] += amount
            with log_lock:
                transfer_log.append(
                    f"{src['owner']} → {dst['owner']} £{amount}"
                )


# Launch many concurrent transfers in both directions.
threads = []
for _ in range(20):
    threads.append(threading.Thread(target=transfer, args=(alice, bob,     50)))
    threads.append(threading.Thread(target=transfer, args=(bob,   charlie, 30)))
    threads.append(threading.Thread(target=transfer, args=(charlie, alice, 20)))

for t in threads:
    t.start()
for t in threads:
    t.join()

total = alice["balance"] + bob["balance"] + charlie["balance"]

print("\nPart 2 — Transaction")
print(f"  {len(transfer_log)} transfers completed")
print(f"  Alice:   £{alice['balance']}")
print(f"  Bob:     £{bob['balance']}")
print(f"  Charlie: £{charlie['balance']}")
print(f"  Total:   £{total}  (must equal £2250 — no money created or lost)")
assert total == 2250, f"Invariant broken: total = {total}"
print("  Invariant holds ✓")
