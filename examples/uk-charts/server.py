#!/usr/bin/env python3
"""
UK Charts Explorer — server.py

HTTP + SSE back-end for the UK Charts Explorer demo.

High-level flow
───────────────
1.  Browser loads ui.html, opens a long-lived SSE connection on GET /stream.
2.  User clicks a node (or the graph auto-starts) → browser POSTs /explore/<id>.
3.  BFSManager.explore() pushes the node onto an internal queue.Queue.
4.  A daemon worker thread picks it up, calls _expand_node() to iterate the
    node and serialise its children, then broadcasts the result to every
    connected SSE client.
5.  The browser receives the "expand" event, calls addChild() for each child,
    and the D3 force graph updates immediately.
6.  The worker cascades children whose auto_expand flag is True back onto the
    BFS queue so the graph grows automatically without client involvement.
    Children with auto_expand=False wait for a user click.

Pause / Resume
──────────────
Workers block on a threading.Event that is cleared when paused.  The current
fetch always completes; only the *next* pick-up is blocked.  The queue retains
all pending items so Resume carries on from exactly the same frontier.

Stop
────
Drains the queue and sets a _stop_flag.  BFSManager.push() is a no-op while
stopped.  A user POST to /explore/<id> clears the flag and re-opens the queue.

SSE event types
───────────────
  expand    — { node_id, label, children:[...], status, error }
  status    — { msg, queued }  (after each expansion; queued = queue depth)
  done      — { queued:0 }     (queue empty and all workers idle)
  heartbeat — {}               (every 15 s when idle; keeps proxies alive)
"""

from __future__ import annotations

import argparse
import json
import queue
import sys
import time
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List

_HERE = Path(__file__).resolve().parent
for _p in (_HERE, _HERE.parent, _HERE.parent.parent):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import model
from model import (
    ArtistNode, ChartNode, DecadeNode, MonthNode,
    ReleaseNode, Timeline, YearNode,
)

_UI_FILE  = _HERE / "ui.html"
_CSS_FILE = _HERE / "ui.css"

# Concurrent BFS worker threads.
_N_WORKERS = 6

# Map URL type-slug → node class.  Drives all key-based REST endpoints.
_TYPE_MAP = {
    "timeline": Timeline,
    "decade":   DecadeNode,
    "year":     YearNode,
    "month":    MonthNode,
    "chart":    ChartNode,
    "artist":   ArtistNode,
    "release":  ReleaseNode,
}

# Per-worker generation tag — lets _chain_notify discard stale parent-chain broadcasts.
_worker_tl = threading.local()


# ─────────────────────────────────────────────────────────────────────────────
# Node registry  (id(node) → node)
#
# Python's id() is address-based and reusable after GC, so we hold a strong
# reference here to keep each node alive for as long as the session lasts.
# ─────────────────────────────────────────────────────────────────────────────

_node_registry: Dict[int, object] = {}
_registry_lock = threading.Lock()


def _register(node) -> None:
    """Add node to the registry so the HTTP handler can look it up by id()."""
    with _registry_lock:
        _node_registry[id(node)] = node


def _lookup(node_id: int):
    """Return the node for this id(), or None."""
    with _registry_lock:
        return _node_registry.get(node_id)


def _resolve_node(type_name: str, key: str):
    """Get or create a node by REST type name and key.

    Checks the registry first (fast path), then falls back to get_or_create
    with data derived from the key alone.  Returns (node, error_str) — error
    is None on success.
    """
    cls = _TYPE_MAP.get(type_name)
    if cls is None:
        return None, f"Unknown type: {type_name!r}"
    key  = urllib.parse.unquote(key)   # decode %2F etc. in artist/release paths
    node = cls.get_or_create(key, cls._data_from_key(key))
    _register(node)
    return node, None


# ─────────────────────────────────────────────────────────────────────────────
# Node metadata helpers
# ─────────────────────────────────────────────────────────────────────────────

def _label(node) -> str:
    return node.get_rendering().label


def _node_type(node) -> str:
    return type(node).__name__.replace("Root", "").replace("Node", "").lower()


def _node_info(node) -> dict:
    """
    Serialise a node to the JSON dict the browser stores in its gNodes array.

    All fields used by the D3 renderer, tooltip, and physics are included.
    ReleaseNode chart stats are included if already fetched; otherwise they
    are zeroed and fetched on demand via GET /stats/<id>.

    Mixin attributes (RenderMixin, PhysicsMixin, GraphBehavior) are read via
    getattr so that nodes which do not inherit a mixin still get safe defaults.
    """
    name = _node_type(node)
    r    = node.get_rendering()
    p    = node.get_physics()
    if not r.colour_key:  r.colour_key = name
    if not r.type_label:  r.type_label = name
    if not r.label_lines: r.label_lines = [r.label]
    info: dict = {
        "id":        id(node),
        "node_type": name,
        "rendering": {k: getattr(r, k) for k in vars(type(r)) if not k.startswith("_")},
        "physics":   {k: getattr(p, k) for k in vars(type(p)) if not k.startswith("_")},
    }
    info.update(node.node_extra())
    return info


# ─────────────────────────────────────────────────────────────────────────────
# BFS expansion logic
# ─────────────────────────────────────────────────────────────────────────────

def _broadcast_expanded(node) -> None:
    """Broadcast the 'expanded' event so the browser can mark the node as visited."""
    has_get = callable(getattr(node, "get", None))
    error   = node.get("error", "") if has_get else ""
    label   = node.get_rendering().label
    ev = {"type": "expanded", "id": id(node), "label": label}
    if error:
        ev["error"] = error
        ev["trace"] = node.get("error_trace", "") if has_get else ""
    _bfs._broadcast(ev)


# ─────────────────────────────────────────────────────────────────────────────
# BFSManager — server-side BFS queue + worker pool + SSE broadcast
# ─────────────────────────────────────────────────────────────────────────────

class BFSManager:
    """
    Coordinates the BFS exploration on the server.

    Owns:
      _queue       — queue.Queue of pending node IDs
      _seen        — set of IDs already queued or processed (dedup)
      _pause_event — threading.Event; set=running, clear=paused
      _stop_flag   — True after stop(); cleared by explore() or resume()
      _clients     — list of per-SSE-client queues (broadcast targets)
      _in_flight   — count of workers currently expanding a node
    """

    def __init__(self, n_workers: int = _N_WORKERS) -> None:
        self._queue        = queue.Queue()
        self._seen: set    = set()
        self._seen_lock    = threading.Lock()
        self._pause_event  = threading.Event()
        self._pause_event.set()                    # start in running state
        self._stop_flag    = False
        self._gen          = 0                     # incremented on reset; stale workers check this
        self._clients: List[queue.Queue] = []
        self._cli_lock     = threading.Lock()
        self._in_flight    = 0
        self._flight_lock  = threading.Lock()
        self._n_workers    = n_workers
        self._root_id      = 0                     # set by _init()
        self._event_buf    = queue.Queue()         # workers push here; broadcaster forwards at rate

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Spawn N daemon worker threads plus one broadcaster thread."""
        for _ in range(self._n_workers):
            threading.Thread(target=self._worker,     daemon=True).start()
        threading.Thread(target=self._broadcaster, daemon=True).start()

    # ── Client subscription (one queue per SSE connection) ────────────────────

    def subscribe(self) -> queue.Queue:
        """Register a new SSE client and return its dedicated event queue."""
        q = queue.Queue()
        with self._cli_lock:
            self._clients.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        """Remove a client queue when its connection closes."""
        with self._cli_lock:
            try:
                self._clients.remove(q)
            except ValueError:
                pass
            # Don't reset on disconnect — a browser refresh is a brief gap, not
            # a session end.  /root is the only intended reset point.

    def _broadcast(self, event: dict) -> None:
        """Enqueue an event for the broadcaster thread to forward to clients."""
        self._event_buf.put(event)

    def _broadcaster(self) -> None:
        """Forward events from _event_buf to SSE clients at a controlled rate.

        Drains up to 10 events per 20 ms so workers can run at full speed
        while the browser receives a steady, digestible stream rather than a
        sudden flood that overwhelms the D3 simulation.
        """
        while True:
            # Block until at least one event is ready.
            try:
                first = self._event_buf.get(timeout=1)
            except queue.Empty:
                continue
            batch = [first]
            # Drain up to 9 more that are already queued (no extra wait).
            for _ in range(9):
                try:
                    batch.append(self._event_buf.get_nowait())
                except queue.Empty:
                    break
            with self._cli_lock:
                clients = list(self._clients)
            for event in batch:
                for q in clients:
                    q.put(event)
            time.sleep(0.02)   # 20 ms between batches → ≤500 events/s

    # ── Queue control ─────────────────────────────────────────────────────────

    def queue_request(self, node, fn: str) -> None:
        """Enqueue a task for node.handle_task(fn).

        No-op if stopped or if this (node, fn) pair is already queued/seen.
        The gen is captured at enqueue time so stale tasks are discarded by
        the worker without affecting newer ones.
        """
        if self._stop_flag:
            return
        node_id = id(node)
        if fn == "post_init":
            parent_id = getattr(_worker_tl, "current_node_id", None)
            object.__setattr__(node, "_parent_id", parent_id)
            object.__setattr__(node, "_post_init_pending", True)
        with self._seen_lock:
            key = (node_id, fn)
            if key in self._seen:
                return
            self._seen.add(key)
        self._queue.put((node, fn, self._gen))

    def explore(self, node_id: int) -> None:
        """User-initiated click from POST /explore/<id>.

        Clears the stopped flag and allows re-expansion of an already-seen node.
        """
        self._stop_flag = False
        node = _lookup(node_id)
        if node is None:
            return
        with self._seen_lock:
            self._seen.discard((node_id, "click"))
        self.queue_request(node, "click")

    def pause(self) -> None:
        """Block workers after their current fetch finishes."""
        self._pause_event.clear()

    def resume(self) -> None:
        """Unblock workers and clear the stopped flag."""
        self._stop_flag = False
        self._pause_event.set()

    def stop(self) -> None:
        """Drain the queue; prevent cascade entries until explore() or resume()."""
        self._stop_flag = True
        while True:
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except queue.Empty:
                break

    def reset(self) -> None:
        """Full reset — drain queue, clear seen set, resume workers."""
        self._gen += 1        # invalidate all in-flight cascade pushes
        self.stop()
        with self._seen_lock:
            self._seen.clear()
        self._stop_flag = False
        self._pause_event.set()

    # ── Worker ────────────────────────────────────────────────────────────────

    def _worker(self) -> None:
        """
        Worker thread body.  Runs forever (daemon=True so it exits with the
        process).

        Blocks on _pause_event.wait() when paused, then picks the next node
        with a 1-second timeout (to stay responsive to resume and process
        shutdown).  Expands the node, broadcasts the result, then fires a
        "done" event when both the queue is empty and no sibling worker is
        still in flight.
        """
        while True:
            self._pause_event.wait()       # blocks here when paused

            try:
                node, fn, gen = self._queue.get(timeout=1)
            except queue.Empty:
                continue

            with self._flight_lock:
                self._in_flight += 1

            try:
                if gen == self._gen:
                    _worker_tl.gen = gen
                    _worker_tl.current_node_id = id(node)
                    node.handle_task(fn)
                    if fn == "click":
                        _broadcast_expanded(node)
            finally:
                queued = self._queue.qsize()
                with self._flight_lock:
                    self._in_flight -= 1
                    in_flight = self._in_flight

                if queued == 0 and in_flight == 0:
                    self._broadcast({"type": "done", "queued": 0})
                else:
                    self._broadcast({"type": "status", "queued": queued})

                self._queue.task_done()


# Module-level singleton — workers started in main() after arg parsing.
_bfs = BFSManager()


# ─────────────────────────────────────────────────────────────────────────────
# Graph root
# ─────────────────────────────────────────────────────────────────────────────

_root: Timeline | None = None


def _console_log(msg: str) -> None:
    """Broadcast a console message to all SSE clients."""
    _bfs._broadcast({"type": "console", "msg": msg})


def _chain_notify(node, parent_id: int | None) -> None:
    """_notify callback: register the node and broadcast it to SSE clients.

    Called by BaseNode.add_to_canvas() via the _notify hook.  Handles the
    gen check so stale workers don't pollute a freshly-loaded client.
    """
    tl_gen = getattr(_worker_tl, "gen", -1)
    if tl_gen != -1 and tl_gen != _bfs._gen:
        return
    _register(node)
    info = _node_info(node)
    if parent_id is not None:
        info["parent_id"] = parent_id
    _bfs._broadcast({"type": "node", **info})


def _init() -> Timeline:
    """
    Tear down all node registries, reset the BFS manager, and create a
    fresh Timeline root.

    Called by GET /root whenever the user resets the graph or switches chart
    modes.  If the root node has auto_expand=True the BFS is kicked off here;
    otherwise the client must POST /explore/<root_id> to begin.
    """
    global _root
    for cls in (Timeline, DecadeNode, YearNode, MonthNode, ChartNode, ArtistNode, ReleaseNode):
        cls.clear_registry()
    with _registry_lock:
        _node_registry.clear()
    _bfs.reset()
    model.set_manager(_bfs, _chain_notify, _console_log)
    _root = Timeline.get_or_create("timeline", {})
    _root._digest()
    _root._db_save()
    _register(_root)
    _root.add_to_canvas(None)
    _bfs._root_id = id(_root)
    return _root


# ─────────────────────────────────────────────────────────────────────────────
# HTTP handler
# ─────────────────────────────────────────────────────────────────────────────

class _Handler(BaseHTTPRequestHandler):
    """
    Single handler for all HTTP and SSE endpoints.

    GET endpoints
    ─────────────
      /              Serve ui.html
      /ui.css        Serve ui.css
      /root          Reset graph, return root node JSON
      /stream        Long-lived SSE stream (expand / status / done / heartbeat)
      /stats/<id>    Chart timeline stats for a release (tooltip use)
      /search        Artist name suggestions (backstage API proxy)
      /add-artist    Create an ArtistNode and return its JSON

    POST endpoints
    ──────────────
      /explore/<id>  Push node onto the BFS queue (user-initiated expand)
      /pause         Pause workers
      /resume        Resume workers
      /stop          Drain queue (workers finish current item then idle)
    """

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path   = parsed.path.rstrip("/") or "/"

        if path == "/":
            self._file(200, "text/html; charset=utf-8", _UI_FILE.read_bytes())

        elif path == "/ui.css":
            self._file(200, "text/css; charset=utf-8", _CSS_FILE.read_bytes())

        elif path == "/prefs.js":
            self._file(200, "text/javascript; charset=utf-8", (_HERE / "prefs.js").read_bytes())

        elif path == "/root":
            qs    = urllib.parse.parse_qs(parsed.query)
            chart = qs.get("chart", ["albums"])[0]
            if chart == "both":
                model.set_chart_slugs(["albums-chart", "singles-chart"])
            else:
                model.set_chart_slug("albums-chart" if chart == "albums" else "singles-chart")
            root = _init()
            auto = getattr(root, "auto_expand", False)
            if auto:
                _bfs.explore(id(root))
            self._json(200, {"node": _node_info(root), "auto_expanding": auto})

        elif path == "/info":
            # Non-destructive: returns current root id without resetting.
            root_id = getattr(_bfs, "_root_id", None)
            self._json(200, {"root_id": root_id})

        elif path.startswith("/node/"):
            # GET /node/<type>/<key> — inspect one node; creates it if not yet seen.
            rest  = path[len("/node/"):]
            slash = rest.find("/")
            if slash == -1:
                self._json(400, {"error": "Expected /node/<type>/<key>"}); return
            type_name, key = rest[:slash], rest[slash + 1:]
            node, err = _resolve_node(type_name, key)
            if err:
                self._json(400, {"error": err}); return
            self._json(200, {"node": _node_info(node)})

        elif path.startswith("/nodes/"):
            # GET /nodes/<type>?q=<term> — list registered nodes of a type, optionally filtered.
            type_name = path[len("/nodes/"):]
            cls = _TYPE_MAP.get(type_name)
            if cls is None:
                self._json(400, {"error": f"Unknown type: {type_name!r}"}); return
            qs = urllib.parse.parse_qs(parsed.query)
            q  = qs.get("q", [""])[0].lower()
            with _registry_lock:
                nodes = [n for n in _node_registry.values() if isinstance(n, cls)]
            results = [_node_info(n) for n in nodes
                       if not q or q in _label(n).lower()]
            self._json(200, {"type": type_name, "nodes": results})

        elif path == "/stream":
            self._stream()

        elif path.startswith("/stats/"):
            try:
                node_id = int(path[len("/stats/"):])
            except ValueError:
                self._json(400, {"error": "Bad node id"}); return
            self._stats(node_id)

        elif path == "/search":
            qs    = urllib.parse.parse_qs(parsed.query)
            terms = qs.get("terms", [""])[0]
            self._search(terms)

        elif path == "/add-artist":
            qs          = urllib.parse.parse_qs(parsed.query)
            artist_path = qs.get("path", [""])[0]
            name        = qs.get("name", [""])[0]
            if not artist_path:
                self._json(400, {"error": "Missing path"}); return
            node = ArtistNode.get_or_create(artist_path, {"name": name, "artist_path": artist_path})
            _register(node)
            self._json(200, {"node": _node_info(node)})

        else:
            self._json(404, {"error": "Not found"})

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path   = parsed.path.rstrip("/") or "/"

        if path.startswith("/explore/"):
            rest = path[len("/explore/"):]
            try:
                # Numeric id — browser click path; keep working as before.
                node_id = int(rest)
                if _lookup(node_id) is None:
                    self._json(404, {"error": "Node not found"}); return
                _bfs.explore(node_id)
                self._json(200, {"queued": True})
            except ValueError:
                # <type>/<key> — REST path; resolve or create on demand.
                slash = rest.find("/")
                if slash == -1:
                    self._json(400, {"error": "Expected /explore/<type>/<key>"}); return
                type_name, key = rest[:slash], rest[slash + 1:]
                node, err = _resolve_node(type_name, key)
                if err:
                    self._json(400, {"error": err}); return
                if _root is not None and getattr(node, "_parent_id", None) is None:
                    object.__setattr__(node, "_parent_id", id(_root))
                _bfs.explore(id(node))
                self._json(200, {"queued": True, "node": _node_info(node)})

        elif path == "/search":
            # POST /search?q=<term> — fetch top OCC hit and add it to the canvas.
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            q  = qs.get("q", [""])[0].strip()
            if not q:
                self._json(400, {"error": "Missing q"}); return
            try:
                import json as _json
                raw     = model.BaseNode._fetch_raw(
                    model.BaseNode._SUGGEST_BASE + urllib.parse.quote_plus(q))
                data    = _json.loads(raw)
                artists = data.get("results", {}).get("artist", [])
                if not artists:
                    self._json(200, {"found": False, "q": q}); return
                top     = artists[0]
                url     = top.get("url", "")
                if not url:
                    self._json(200, {"found": False, "q": q}); return
                artist_path = url.replace("https://www.officialcharts.com", "").rstrip("/") + "/"
                name        = top.get("title", "")
                node        = ArtistNode.get_or_create(
                    artist_path, {"name": name, "artist_path": artist_path})
                if _root is not None and getattr(node, "_parent_id", None) is None:
                    object.__setattr__(node, "_parent_id", id(_root))
                if not getattr(node, "_post_init_pending", False):
                    node.add_to_canvas()
                self._json(200, {"found": True, "node": _node_info(node)})
            except Exception as exc:
                self._json(500, {"error": str(exc)})

        elif path == "/pause":
            _bfs.pause()
            self._json(200, {"paused": True})

        elif path == "/resume":
            _bfs.resume()
            self._json(200, {"paused": False})

        elif path == "/stop":
            _bfs.stop()
            self._json(200, {"stopped": True})

        else:
            self._json(404, {"error": "Not found"})

    # ── SSE stream ────────────────────────────────────────────────────────────

    def _stream(self) -> None:
        """
        Long-lived SSE endpoint.

        Subscribes to the BFSManager broadcast queue, then loops:
          • Wait up to 15 s for the next event.
          • If nothing arrives, send a heartbeat to keep proxies from timing out.
          • If the client disconnects (BrokenPipeError), unsubscribe and exit.

        Each event is formatted as:
            event: <type>\\ndata: <json>\\n\\n
        """
        self.send_response(200)
        self.send_header("Content-Type",  "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection",    "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        q = _bfs.subscribe()
        try:
            while True:
                try:
                    event = q.get(timeout=15)
                except queue.Empty:
                    # Heartbeat — keeps the connection alive through idle periods
                    self.wfile.write(b"event: heartbeat\ndata: {}\n\n")
                    self.wfile.flush()
                    continue

                event_type = event.get("type", "expand")
                data       = json.dumps(event).encode()
                msg        = f"event: {event_type}\ndata: ".encode() + data + b"\n\n"
                self.wfile.write(msg)
                self.wfile.flush()

        except (BrokenPipeError, ConnectionResetError, OSError):
            pass    # client closed the tab or navigated away
        finally:
            _bfs.unsubscribe(q)

    # ── Stats ─────────────────────────────────────────────────────────────────

    def _stats(self, node_id: int) -> None:
        """
        Fetch chart run data for a release not yet expanded, then return its
        rendered tooltip so the client can update without a full node refresh.
        """
        node = _lookup(node_id)
        if node is None or not isinstance(node, ReleaseNode):
            self._json(404, {"error": "Not a release node"}); return
        try:
            node._digest()
        except Exception as exc:
            self._json(200, {"error": str(exc)}); return
        r = node.get_rendering()
        self._json(200, {
            "tooltip":     r.tooltip,
            "stats_stale": r.stats_stale,
        })

    # ── Search ────────────────────────────────────────────────────────────────

    def _search(self, terms: str) -> None:
        """Proxy the backstage.officialcharts.com artist-suggestion endpoint."""
        if not terms:
            self._json(200, {"artists": []}); return
        try:
            import json as _json
            raw  = model.BaseNode._fetch_raw(model.BaseNode._SUGGEST_BASE + urllib.parse.quote_plus(terms))
            data = _json.loads(raw)
            out  = []
            for a in data.get("results", {}).get("artist", [])[:8]:
                url = a.get("url", "")
                if not url:
                    continue
                p = url.replace("https://www.officialcharts.com", "").rstrip("/") + "/"
                out.append({"title": a.get("title", ""), "path": p})
            self._json(200, {"artists": out})
        except Exception as exc:
            self._json(200, {"artists": [], "error": str(exc)})

    # ── Low-level response helpers ────────────────────────────────────────────

    def _json(self, code: int, payload: object) -> None:
        body = json.dumps(payload).encode()
        self._file(code, "application/json; charset=utf-8", body)

    def _file(self, code: int, content_type: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type",   content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_) -> None:
        pass    # suppress per-request console output


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="UK Charts Explorer — Node-X demo")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--db", default="", metavar="PATH",
                    help="SQLite cache path, e.g. cache.db (default: no caching)")
    args = ap.parse_args()
    if args.db:
        from node_x.node_x_sqlite import NodeDB
        model.set_node_db(NodeDB(args.db))
        print(f"Cache: {args.db}")
    _bfs.start()    # spin up worker threads before accepting the first request
    server = ThreadingHTTPServer(("", args.port), _Handler)
    print(f"UK Charts Explorer at http://localhost:{args.port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
