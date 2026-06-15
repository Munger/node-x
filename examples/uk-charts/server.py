#!/usr/bin/env python3
## @file server.py
##
## @brief HTTP + SSE back-end for the UK Charts Explorer demo.
##
## Flow
## ────
## 1. Browser loads ui.html, opens a long-lived SSE connection on GET /stream.
## 2. User clicks a node → browser POSTs /explore/<id>.
## 3. server posts a click event targeting that node.
## 4. app.HandleEvents() dispatches to node.handle_event() which calls
##    Async(list, self) — iteration runs on a pool thread without blocking the loop.
## 5. Each child is Registered; rest.SetOnRegister fires _canvas.handle_event()
##    which broadcasts the "node" SSE event to all connected clients.
## 6. Nodes with auto_expand=True post their own click event from __iter__.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @par Licence: MIT

from __future__ import annotations

import argparse
import json
import os
import queue
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import List

_HERE = Path(__file__).resolve().parent
for _p in (_HERE, _HERE.parent, _HERE.parent.parent):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import app
import rest as rest_mod
from cache import SQLiteNodeCache, InitCache

_UI_FILE  = _HERE / "ui.html"
_CSS_FILE = _HERE / "ui.css"

_cache: SQLiteNodeCache | None = None

# ─────────────────────────────────────────────────────────────────────────────
# SSE client management
# ─────────────────────────────────────────────────────────────────────────────

_clients:  List[queue.Queue] = []
_cli_lock: threading.Lock    = threading.Lock()


def _sse_broadcast(event: dict) -> None:
    with _cli_lock:
        clients = list(_clients)
    for q in clients:
        q.put(event)



# ─────────────────────────────────────────────────────────────────────────────
# Canvas event handler — receives add_to_canvas / expanded events from app
# ─────────────────────────────────────────────────────────────────────────────

class _CanvasHandler(app.EventHandler):

    def handle_event(self, event: dict) -> None:
        if event.get("_gen", app._gen) != app._gen:
            return
        t = event.get("type")
        if t == "add_to_canvas":
            node      = event["node"]
            parent_id = event.get("parent_id")
            info      = node.to_info()
            if parent_id is not None:
                info["parent_id"] = parent_id
            if getattr(node, "is_root", False):
                global _root, _root_id
                _root    = node
                _root_id = id(node)
            if getattr(node, "auto_expand", False):
                node.post_event({"type": "click", "target": node})
            _sse_broadcast({"type": "node", **info})

        elif t == "expanded":
            node    = event["node"]
            has_get = callable(getattr(node, "get", None))
            error   = node.get("error", "") if has_get else ""
            label   = node.get_rendering().label
            ev      = {"type": "expanded", "id": id(node), "label": label}
            if error:
                ev["error"] = error
                ev["trace"] = node.get("error_trace", "") if has_get else ""
            _sse_broadcast(ev)

        elif t == "sse":
            _sse_broadcast(event["data"])


_canvas  = _CanvasHandler()
_root    = None
_root_id = 0


# ─────────────────────────────────────────────────────────────────────────────
# Graph reset
# ─────────────────────────────────────────────────────────────────────────────

def _init() -> None:
    global _root, _root_id
    rest_mod.Clear()
    app.reset()
    _root    = None
    _root_id = 0
    rest_mod.Register(model.Timeline())


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
      /stream        Long-lived SSE stream
      /stats/<id>    Chart timeline stats for a release (tooltip use)
      /search        Artist name suggestions (backstage API proxy)
      /add-artist    Create an ArtistNode and return its JSON

    POST endpoints
    ──────────────
      /explore/<id>  Post a click event for the node
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
                model.SetChartSlugs(["albums-chart", "singles-chart"])
            else:
                model.SetChartSlug("albums-chart" if chart == "albums" else "singles-chart")
            _init()
            root_cls = rest_mod.RootClass()
            auto = getattr(root_cls, "auto_expand", False)
            self._json(200, {"auto_expanding": auto})

        elif path == "/info":
            self._json(200, {"root_id": _root_id})

        elif path.startswith("/node/"):
            rest  = path[len("/node/"):]
            slash = rest.find("/")
            if slash == -1:
                self._json(400, {"error": "Expected /node/<type>/<key>"}); return
            type_name, key = rest[:slash], urllib.parse.unquote(rest[slash + 1:])
            node = rest_mod.LookupKey(type_name, key)
            if node is None:
                self._json(404, {"error": "Node not found"}); return
            self._json(200, {"node": node.to_info()})

        elif path.startswith("/nodes/"):
            type_name = path[len("/nodes/"):]
            qs    = urllib.parse.parse_qs(parsed.query)
            q     = qs.get("q", [""])[0].lower()
            nodes = rest_mod.NodesOfSlug(type_name)
            if not nodes and not rest_mod._types.get(type_name):
                self._json(400, {"error": f"Unknown type: {type_name!r}"}); return
            results = [n.to_info() for n in nodes
                       if not q or q in n.get_rendering().label.lower()]
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
            node = rest_mod.LookupKey("artist", artist_path)
            if node is None:
                _cache.get(rest_mod._types["artist"], artist_path,
                           name=name, artist_path=artist_path)
                self._json(202, {"queued": True}); return
            self._json(200, {"node": node.to_info()})

        else:
            self._json(404, {"error": "Not found"})

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path   = parsed.path.rstrip("/") or "/"

        if path == "/stop":
            app.reset()
            self._json(200, {"stopped": True})

        elif path == "/restart":
            self._json(200, {"restarting": True})
            def _do_restart():
                import time as _time
                _time.sleep(0.15)
                os.execv(sys.executable, [sys.executable] + sys.argv)
            threading.Thread(target=_do_restart, daemon=True).start()

        elif path.startswith("/explore/"):
            rest = path[len("/explore/"):]
            try:
                node_id = int(rest)
                node = rest_mod.LookupId(node_id)
                if node is None:
                    self._json(404, {"error": "Node not found"}); return
                node.post_event({"type": "click", "target": node})
                self._json(200, {"queued": True})
            except ValueError:
                slash = rest.find("/")
                if slash == -1:
                    self._json(400, {"error": "Expected /explore/<type>/<key>"}); return
                type_name, key = rest[:slash], urllib.parse.unquote(rest[slash + 1:])
                node = rest_mod.LookupKey(type_name, key)
                if node is None:
                    self._json(404, {"error": "Node not found"}); return
                if _root is not None and getattr(node, "_parent_id", None) is None:
                    object.__setattr__(node, "_parent_id", id(_root))
                node.post_event({"type": "click", "target": node})
                self._json(200, {"queued": True, "node": node.to_info()})

        elif path == "/search":
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
                node = rest_mod.LookupKey("artist", artist_path)
                if node is None:
                    _cache.get(rest_mod._types["artist"], artist_path,
                               name=name, artist_path=artist_path)
                    self._json(202, {"found": True, "queued": True}); return
                if _root is not None and getattr(node, "_parent_id", None) is None:
                    object.__setattr__(node, "_parent_id", id(_root))
                self._json(200, {"found": True, "node": node.to_info()})
            except Exception as exc:
                self._json(500, {"error": str(exc)})

        else:
            self._json(404, {"error": "Not found"})

    # ── SSE stream ────────────────────────────────────────────────────────────

    def _stream(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type",  "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection",    "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        q: queue.Queue = queue.Queue()
        with _cli_lock:
            _clients.append(q)
        try:
            while True:
                try:
                    event = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b"event: heartbeat\ndata: {}\n\n")
                    self.wfile.flush()
                    continue
                event_type = event.get("type", "expand")
                data       = json.dumps(event).encode()
                msg        = f"event: {event_type}\ndata: ".encode() + data + b"\n\n"
                self.wfile.write(msg)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with _cli_lock:
                try:
                    _clients.remove(q)
                except ValueError:
                    pass

    # ── Stats ─────────────────────────────────────────────────────────────────

    def _stats(self, node_id: int) -> None:
        node = rest_mod.LookupId(node_id)
        if node is None or getattr(type(node), "rest_slug", "") != "release":
            self._json(404, {"error": "Not a release node"}); return
        if not node.get("runs"):
            ds = node._data_source()
            if ds is not None:
                try:
                    node.populate(ds.fetch())
                except Exception as exc:
                    self._json(500, {"error": str(exc)}); return
        r = node.get_rendering()
        self._json(200, {"tooltip": r.tooltip, "stats_stale": r.stats_stale})

    # ── Search ────────────────────────────────────────────────────────────────

    def _search(self, terms: str) -> None:
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

    # ── Response helpers ──────────────────────────────────────────────────────

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
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    global _cache, model
    ap = argparse.ArgumentParser(description="UK Charts Explorer — Node-X demo")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--db", default="uk_charts.db", metavar="PATH",
                    help="SQLite cache path (default: uk_charts.db)")
    args = ap.parse_args()

    _cache = SQLiteNodeCache(args.db)
    InitCache(_cache)

    rest_mod.SetOnRegister(
        lambda node, parent_id: _canvas.handle_event(
            {"type": "add_to_canvas", "node": node, "parent_id": parent_id, "_gen": app.worker_gen()}
        )
    )

    import model

    server = ThreadingHTTPServer(("", args.port), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"Cache: {args.db}")
    print(f"UK Charts Explorer at http://localhost:{args.port}/")
    try:
        app.run()
    except KeyboardInterrupt:
        print("\nStopped.")
        _cache.shutdown()


if __name__ == "__main__":
    main()
