## @file app.py
##
## @brief Central event dispatcher and thread pool for the UK Charts Explorer.
##
## Provides a global event stack so any module can post events without
## importing the module that handles them.  Anything that deals with events
## inherits EventHandler and overrides on_event().  The app's main loop
## calls HandleEvents() which dispatches each event to its target handler,
## plus any registered periodic tasks.
##
## Import chain: app ← model, server   (app imports nothing from either)
##
## @copyright Copyright (c) 2026 Tim Hosking
## @par Licence: MIT

from __future__ import annotations

import queue
import threading
import time
from typing import Callable, Dict, List

# ── Thread pool ───────────────────────────────────────────────────────────────
#
# Plain FIFO queue — the event stack (LIFO) already ensures the most recently
# posted event is dispatched first.  Whichever event is dispatched first simply
# gets the next free worker.  No separate priority numbering needed.

_NWORKERS = 10
_tl       = threading.local()
_work_q: queue.LifoQueue = queue.LifoQueue()


def _pool_worker() -> None:
    while True:
        _work_q.get()()


for _i in range(_NWORKERS):
    threading.Thread(target=_pool_worker, daemon=True, name=f"async-{_i}").start()


def current_gen() -> int:
    """Return the current app generation.  Incremented on every reset."""
    return _gen


def worker_gen() -> int:
    """Return the generation captured when this pool thread was dispatched."""
    return getattr(_tl, 'gen', _gen)


def _async_submit(fn, *args, on_done: "EventHandler | None" = None, **kwargs) -> None:
    gen = _gen

    def _run():
        _tl.gen = gen
        result = fn(*args, **kwargs)
        if on_done is not None:
            on_done.post_event({"type": "async_done", "target": on_done, "value": result})

    _work_q.put(_run)


# ── Event handler base ────────────────────────────────────────────────────────

class EventHandler:
    """Mixin for anything that sends or receives app events."""

    def post_event(self, event: dict) -> None:
        """Push an event onto the stack, stamped with this thread's gen."""
        QueueEvent(event, worker_gen())

    def handle_event(self, event: dict) -> None:
        self.on_event(event)

    def on_event(self, event: dict) -> None: ...

    def Async(self, fn, *args, on_done: "EventHandler | None" = None, **kwargs) -> None:
        """Submit fn to the pool; the event stack ordering determines dispatch order."""
        _async_submit(fn, *args, on_done=on_done, **kwargs)


# ── Event bus ─────────────────────────────────────────────────────────────────
#
# Events are held on a stack (LIFO).  post_event() pushes; HandleEvents() pops.
# The most recently posted event is always dispatched first, so a user click
# pushed on top of a deep cascade backlog is processed on the very next tick.

_event_stack: list             = []
_stack_lock:  threading.Lock   = threading.Lock()
_gen:         int              = 0
_handlers:    Dict[str, Callable] = {}
_tasks:       List[Callable]      = []


def QueueEvent(event: dict, gen: int | None = None) -> None:
    """Push an event onto the stack."""
    with _stack_lock:
        _event_stack.append((_gen if gen is None else gen, event))


def RegisterHandler(event_type: str, fn: Callable) -> None:
    """Register a fallback handler for events without a specific target."""
    _handlers[event_type] = fn


def RegisterTask(fn: Callable) -> None:
    """Register a periodic task called every loop iteration."""
    _tasks.append(fn)


def reset() -> None:
    """Increment generation and drain both the event stack and work queue."""
    global _gen
    _gen += 1
    with _stack_lock:
        _event_stack.clear()
    while True:
        try:
            _work_q.get_nowait()
        except queue.Empty:
            break


def HandleEvents() -> None:
    """Pop and dispatch events from the stack until it is empty."""
    while True:
        with _stack_lock:
            if not _event_stack:
                break
            gen, event = _event_stack.pop()
        if gen != _gen:
            continue
        target = event.get("target")
        if isinstance(target, EventHandler):
            target.handle_event(event)
        else:
            fn = _handlers.get(event.get("type", ""))
            if fn:
                fn(event)


def run() -> None:
    """Main loop — runs forever.  Call once from main() after all setup."""
    while True:
        HandleEvents()
        for task in _tasks:
            task()
        time.sleep(0.01)
