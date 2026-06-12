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
_N_WORKERS = 4


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


# ─────────────────────────────────────────────────────────────────────────────
# Node metadata helpers
# ─────────────────────────────────────────────────────────────────────────────

def _label(node) -> str:
    """Human-readable label for a node — delegated to the node's own label property."""
    return getattr(node, "label", type(node).__name__)


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
    g = callable(getattr(node, "get", None))
    info: dict = {
        "id":        id(node),
        "label":     _label(node),
        "node_type": _node_type(node),
        "status":    (node.get("status", "") if g else ""),
        # RenderMixin — visual defaults; client reads these directly
        "node_colour": getattr(node, "node_colour", "#888888"),
        "node_radius": getattr(node, "node_radius", 6),
        # PhysicsMixin — D3 placement and force parameters; client applies directly
        "target_radius": getattr(node, "target_radius", 100),
        "link_strength": getattr(node, "link_strength", 0.4),
        "child_spread":  getattr(node, "child_spread",  80),
        "charge":        getattr(node, "charge",        -30),
        "collide_pad":   getattr(node, "collide_pad",   3),
        # GraphBehavior — operational flags
        "auto_expand": getattr(node, "auto_expand", False),
    }
    info.update(node.node_extra())
    return info


# ─────────────────────────────────────────────────────────────────────────────
# BFS expansion logic
# ─────────────────────────────────────────────────────────────────────────────

def _expand_node(node_id: int, gen: int = -1) -> None:
    """
    Iterate node_id and broadcast each child to SSE clients as it is created.

    The iteration does the work — parent chains are built, nodes are registered,
    each child is broadcast immediately.  Nothing is returned; the SSE stream
    is the result.
    """
    node = _lookup(node_id)
    if node is None:
        _bfs._broadcast({"type": "error", "node_id": node_id, "error": "Node not found"})
        return
    try:
        for child in node:
            if gen != -1 and gen != _bfs._gen:
                return          # reset happened mid-iteration; stop broadcasting
            _register(child)
            info = _node_info(child)
            info["parent_id"] = node_id
            _bfs._broadcast({"type": "node", **info})
    except Exception as exc:
        import traceback; traceback.print_exc()
        _bfs._broadcast({"type": "error", "node_id": node_id, "error": str(exc)})
    # Broadcast the expanded node's updated status so the browser can mark it
    # as visited and reflect the new status, even when there are no children.
    has_get = callable(getattr(node, "get", None))
    status  = node.get("status", "") if has_get else ""
    label   = node.get("label",  "") if has_get else str(node_id)
    if not label and has_get:
        label = getattr(node, "label", "") or ""
    ev = {"type": "expanded", "id": node_id, "status": status, "label": label}
    if status == "error":
        ev["error"] = node.get("error", "")
        ev["trace"] = node.get("error_trace", "")
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

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Spawn N daemon worker threads.  Call once at process startup."""
        for _ in range(self._n_workers):
            threading.Thread(target=self._worker, daemon=True).start()

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

    def _broadcast(self, event: dict) -> None:
        """Push an event onto every connected SSE client's queue."""
        with self._cli_lock:
            for q in self._clients:
                q.put(event)

    # ── Queue control ─────────────────────────────────────────────────────────

    def push(self, node_id: int, gen: int = -1) -> None:
        """
        Enqueue node_id for cascade expansion.

        No-op if:  stopped (_stop_flag is True), already seen, or gen is
        stale (worker started before the last reset).
        Workers block when paused, so the queue can grow while paused and
        drain naturally on resume.
        """
        if self._stop_flag:
            return
        if gen != -1 and gen != self._gen:
            return           # stale worker from before last reset
        with self._seen_lock:
            if node_id in self._seen:
                return
            self._seen.add(node_id)
        self._queue.put(node_id)

    def explore(self, node_id: int) -> None:
        """
        User-initiated expand (from POST /explore/<id>).

        Clears the stopped flag so cascading can resume.  Removes node_id
        from _seen to allow re-expansion when the user re-clicks a visited
        node.
        """
        self._stop_flag = False
        with self._seen_lock:
            self._seen.discard(node_id)    # allow re-click
        self.push(node_id, gen=self._gen)

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
                node_id = self._queue.get(timeout=1)
            except queue.Empty:
                continue

            with self._flight_lock:
                self._in_flight += 1

            try:
                gen = self._gen
                _expand_node(node_id, gen)
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


def _chain_notify(node, parent_id: int | None) -> None:
    """Register a parent-chain node and broadcast it to SSE clients.

    Called from model._check_parents() when a new ancestor node is created
    during bottom-up chain assembly (e.g. a ChartNode creating its MonthNode).
    """
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
    model.set_manager(_bfs, _chain_notify)
    _root = Timeline.get_or_create("timeline", {})
    _register(_root)
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
            try:
                node_id = int(path[len("/explore/"):])
            except ValueError:
                self._json(400, {"error": "Bad node id"}); return
            if _lookup(node_id) is None:
                self._json(404, {"error": "Node not found"}); return
            _bfs.explore(node_id)
            self._json(200, {"queued": True})

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
        Fetch full chart timeline stats for a single release node.

        Called on hover by the tooltip for releases that haven't been expanded
        yet (so chart_score is still 0).  fetch_timeline() is idempotent.
        """
        node = _lookup(node_id)
        if node is None or not isinstance(node, ReleaseNode):
            self._json(404, {"error": "Not a release node"}); return
        try:
            node.fetch_timeline()
        except Exception as exc:
            self._json(200, {"error": str(exc)}); return
        self._json(200, {
            "peak_position": node.get("peak_position", 0),
            "total_weeks":   node.get("total_weeks",   0),
            "chart_from":    node.get("chart_from",   ""),
            "chart_to":      node.get("chart_to",     ""),
            "chart_score":   node.get("chart_score",  0.0),
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
