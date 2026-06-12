## @file model.py
##
## @brief Node graph model for the UK Charts Explorer.
##
## Structure
## ---------
## The graph has two distinct structural zones:
##
##   Time tree (Timeline → Decade → Year → Month → Chart)
##     Each node has exactly one parent.  No shared references, no cycles.
##     GraphMixin is inherited for get_or_create convenience, but $ref
##     serialisation never fires here.
##
##   Graph (Week → Release ↔ Artist)
##     ChartNodes are referenced from both their MonthNode and from
##     ReleaseNode.chart_weeks.  ReleaseNodes are referenced from both
##     ChartNodes and ArtistNodes.  This is where graph identity matters
##     and $ref serialisation keeps snapshots correct.
##
## All node classes inherit BaseNode, which wires together the full
## node-x mixin stack (DBMixin, GraphMixin, Serialisable, StreamMixin
## and the app-level RenderMixin, PhysicsMixin, GraphBehavior).
##
## @copyright Copyright (c) 2026 Tim Hosking
## @par Licence: MIT

from __future__ import annotations

import math
import re
import sys
import threading
import traceback
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path
from typing import ClassVar

for _p in (
    Path(__file__).parent,
    Path(__file__).parent.parent,
    Path(__file__).parent.parent.parent,
):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from node_x import GraphMixin, Node, Serialisable, SerialisableNodeList, StreamMixin
from node_x.node_x_sqlite import DBMixin

# Module-level NodeDB instance.  None means no caching — behaviour is
# identical to pre-cache code.  Set via set_node_db() at startup.
_db = None

# Expansion manager and notification callback — injected by server.py at
# startup.  _notify(node, parent_id) registers the node and broadcasts it.
# _manager must have a push(node_id) method to queue a node for expansion.
_manager    = None
_notify     = None
_console_fn = None   # console_fn(msg: str) — logs a message to the UI console

# Per-node fetch locks prevent two worker threads from simultaneously running
# fetch_timeline() on the same ReleaseNode (race: both see _chart_runs is None).


def set_node_db(db) -> None:
    """Point the model at a NodeDB instance for cache-aside reads and writes."""
    global _db
    _db = db


def set_manager(mgr, notify_fn, console_fn=None) -> None:
    """Inject the BFS manager and SSE notification callback from server.py."""
    global _manager, _notify, _console_fn
    _manager    = mgr
    _notify     = notify_fn
    _console_fn = console_fn


def _log(symbol: str, label: str, key: str) -> None:
    print(f"  {symbol} {label:<12} {key}", flush=True)


_FIRST_CHART_YEAR = 1952

_chart_slugs: list[str] = ["albums-chart", "singles-chart"]

def set_chart_slugs(slugs: list[str]) -> None:
    global _chart_slugs
    _chart_slugs = list(slugs)

def set_chart_slug(slug: str) -> None:
    set_chart_slugs([slug])


def _medium(path: str) -> str:
    return "album" if path.startswith("/albums/") else "single"






def _first_chart_date_of_month(year: int, month: int, slug: str) -> "date | None":
    """Return the first real chart publication date in month/year for slug.

    Fetches the chart for the first Sunday of the month; the canonical URL in
    the response tells us the nearest actual publication date for that era.
    """
    d = date(year, month, 1)
    while d.weekday() != 6:
        d += timedelta(days=1)
    try:
        html   = BaseNode._fetch(f"/charts/{slug}/{d.strftime('%Y%m%d')}/")
        # The site redirects to the nearest real publication date; read it from
        # the canonical link rather than trusting the URL we requested.
        can_m  = _CANONICAL_RE.search(html)
        if not can_m:
            return None
        dat_m  = _CANONICAL_DATE.search(can_m.group(1))
        if not dat_m:
            return None
        s      = dat_m.group(1)
        chart_d = date(int(s[:4]), int(s[4:6]), int(s[6:]))
        # Early-era charts sometimes snap back to the last week of the previous
        # month; nudge forward one week to stay in the requested month.
        if chart_d.month != month:
            chart_d += timedelta(weeks=1)
        return chart_d if chart_d.month == month else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

_BLOCK_RE  = re.compile(r'(?=<div[^>]*class="chart-item relative text-right)')
_POS_RE    = re.compile(r'<strong>(\d+)</strong>')
_DATE_RE   = re.compile(r'<time[^>]*datetime="(\d{4}-\d{2}-\d{2})"')
_SONG_RE   = re.compile(r'href="(/songs/[^"]+)"[^>]*class="chart-name[^"]*"(.*?)</a>', re.DOTALL)
_ALBUM_RE  = re.compile(r'href="(/albums/[^"]+)"[^>]*class="chart-name[^"]*"(.*?)</a>', re.DOTALL)
_ARTIST_RE = re.compile(r'href="(/artist/[^"]+)"[^>]*class="chart-artist[^"]*".*?<span>([^<]+)</span>', re.DOTALL)
_SPAN_RE   = re.compile(r'<span>([^<]+)</span>')
_LW_RE         = re.compile(r'LW:\s*(?:<[^>]+>)*(\w+)')
_CANONICAL_RE   = re.compile(r'<link[^>]+rel="canonical"[^>]+href="([^"]+)"')
_CANONICAL_DATE = re.compile(r'/charts/[^/]+/(\d{8})/')
_COLLAB_RE      = re.compile(r'\s+(?:FT\.?|FEAT\.?|FEATURING|AND|WITH|VS\.?|X|&)\s+', re.IGNORECASE)
_RUN_RE    = re.compile(
    r'href="/charts/([^/]+)/(\d{8})/[^"]*"[^>]*>.*?<span[^>]*>(\d+)</span>',
    re.DOTALL,
)


def _last_span(fragment: str) -> str:
    hits = _SPAN_RE.findall(fragment)
    return hits[-1].strip() if hits else ""







# ---------------------------------------------------------------------------
# Typed node lists
# ---------------------------------------------------------------------------

class DecadeList(SerialisableNodeList["DecadeNode"]):         pass
class YearList(SerialisableNodeList["YearNode"]):             pass
class MonthList(SerialisableNodeList["MonthNode"]):           pass
class ChartList(SerialisableNodeList["ChartNode"]):             pass
class ReleaseList(SerialisableNodeList["ReleaseNode"]):       pass


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


# ---------------------------------------------------------------------------
# RenderMixin
# ---------------------------------------------------------------------------

class RenderMixin:
    ## @brief Mixin: visual defaults for the client renderer.
    ##
    ## Each node class sets the colour and radius it should be drawn with.
    ## The server includes these in the JSON payload so the client never has
    ## to switch on node_type to look up visual properties.

    node_colour = "#888888"
    ## @brief CSS hex fill colour for this node type.

    node_radius = 6
    ## @brief Display radius in pixels.


# ---------------------------------------------------------------------------
# PhysicsMixin
# ---------------------------------------------------------------------------

class PhysicsMixin:
    ## @brief Mixin: D3 force-simulation parameters for this node class.
    ##
    ## All values that were previously scattered across the PHYSICS block and
    ## LINK_DIST table in ui.html now live here as class-level defaults.  The
    ## server sends them in the JSON payload; the client reads them directly
    ## off the node object rather than computing or looking them up locally.
    ##
    ## Prefs can override any of these per node type at runtime without
    ## restarting the server or resetting the graph.

    target_radius = 100
    ## @brief Expected distance from the parent node in the simulation.

    link_strength = 0.4
    ## @brief Strength of the D3 link force connecting this node to its parent.

    child_spread = 80
    ## @brief Scatter radius used when placing a new child node randomly.
    ##        Larger values give chart nodes room to breathe on first appearance.

    charge = -30
    ## @brief Repulsion strength for the many-body force.  Negative = repel.

    collide_pad = 3
    ## @brief Extra padding added to the node radius in the collision force.


# ---------------------------------------------------------------------------
# Composite base classes
# ---------------------------------------------------------------------------

class BaseNode(DBMixin, RenderMixin, PhysicsMixin, GraphMixin, Serialisable, StreamMixin, Node):
    ## @brief Base for every serialisable, graph-registered chart node.
    ##
    ## Combines the full mixin stack so subclasses declare only what makes
    ## them distinct.

    auto_expand = False
    ## @brief When True the server expands this node automatically without
    ##        waiting for a user click.  False by default — nodes opt in.

    # ── Network ───────────────────────────────────────────────────────────────
    BASE_URL     = "https://www.officialcharts.com"
    _SUGGEST_BASE = "https://backstage.officialcharts.com/ajax/search-suggestions?terms="
    _HEADERS     = {"User-Agent": "uk-charts-explorer/1.0"}
    _MIN_GAP     = 1.5
    _pool        = threading.Semaphore(4)
    _rate_lock   = threading.Lock()
    _last_req    = 0.0

    # ── Status strings ────────────────────────────────────────────────────────
    _status_cached  = "Loaded"
    _status_fetched = "Fetched"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._digest()

    @staticmethod
    def add_to_canvas(node, parent_id=None) -> None:
        """Notify the server that the node is visible on the canvas.

        The node is already fully assembled and persisted by __init__ before
        reaching this point.  This call solely handles registration and SSE
        broadcast.
        """
        if _notify is not None:
            _notify(node, parent_id)

    @classmethod
    def get_or_create(cls, key, data=None):
        # Check _registry via __dict__ (not inheritance) so each concrete class
        # gets its own registry; a miss on the subclass doesn't fall through to
        # a parent class registry that holds a different node type.
        registry = cls.__dict__.get("_registry", {})
        if key in registry:
            if _console_fn is not None:
                _console_fn(".")
            return registry[key]
        if _db is not None:
            hit = cls.db_load(key, _db)
            if hit is not None:
                if "_registry" not in cls.__dict__:
                    cls._registry = {}
                cls._registry[key] = hit
                if _console_fn is not None:
                    _console_fn(".")
                return hit
        if _console_fn is not None:
            _console_fn(cls._console_label(key, data or {}))
        return super().get_or_create(key, {"_key": key, **(data or {})})

    @classmethod
    def _console_label(cls, key: str, _data: dict) -> str:
        return key

    def _db_save(self) -> None:
        """Persist this node to the DB if caching is enabled."""
        if _db is not None:
            self.db_save(_db)

    @classmethod
    def _fetch(cls, path: str) -> str:
        url = cls.BASE_URL + path
        req = urllib.request.Request(url, headers=cls._HEADERS)
        with cls._pool:
            with cls._rate_lock:
                gap = cls._MIN_GAP - (time.monotonic() - cls._last_req)
                if gap > 0:
                    time.sleep(gap)
                BaseNode._last_req = time.monotonic()
            for attempt in range(4):
                try:
                    with urllib.request.urlopen(req, timeout=20) as r:
                        return r.read().decode("utf-8", errors="replace")
                except urllib.error.HTTPError as exc:
                    if exc.code == 429 and attempt < 3:
                        time.sleep(2 + 2 ** attempt)
                        continue
                    raise

    @classmethod
    def _fetch_raw(cls, url: str) -> str:
        req = urllib.request.Request(url, headers=cls._HEADERS)
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.read().decode("utf-8", errors="replace")

    @property
    def label(self) -> str:
        ## @brief Human-readable display label for this node.
        ##
        ## Subclasses override this to derive the label from their payload.
        ## The default falls back to the class name so unknown subclasses
        ## still produce a legible string in the UI.
        return type(self).__name__

    def node_extra(self) -> dict:
        ## @brief Type-specific fields to include in the JSON sent to the browser.
        ##
        ## Subclasses return a dict of any additional payload keys the client
        ## needs beyond the common fields emitted by ``_node_info()``.  The base
        ## implementation returns an empty dict; only override where extra fields
        ## are needed.
        return {}

    def _parent_specs(self) -> list:
        """Return [(cls, key, data), ...] for each parent this node should have.

        BaseNode has no parents; temporal subclasses override this.
        """
        return []

    def _check_parents(self) -> None:
        """Create any missing parent nodes, notify the server, and queue for expansion."""
        if _notify is None:
            return
        for cls, key, data in self._parent_specs():
            existed = "_registry" in cls.__dict__ and key in cls._registry
            parent  = cls.get_or_create(key, data)
            if not existed:
                parent._check_parents()
                parent_id = None
                for p_cls, p_key, _ in parent._parent_specs():
                    reg = p_cls.__dict__.get("_registry", {})
                    if p_key in reg:
                        parent_id = id(reg[p_key])
                        break
                BaseNode.add_to_canvas(parent, parent_id)
                if parent.auto_expand and _manager is not None:
                    _manager.push(id(parent))

    def _digest(self) -> None:
        """Fetch and parse this node's own upstream page, populating own fields.

        Default marks the node as Loaded — structural nodes (Decade, Year, Month
        etc.) never fetch from upstream so they are always cache hits.  Override
        in page-backed node classes (ChartNode, ReleaseNode, ArtistNode) to fetch
        on first visit and set status accordingly; the override must be idempotent.
        """
        self["status"] = self._status_cached

    def __iter__(self):
        # Lifecycle: digest own page → check parents → cache check → stream children.
        self._digest()
        self._check_parents()
        field = self._children[0] if self._children else None
        if not field:
            return
        cached = self.get(field)
        if cached is None and _db is not None:
            _rk = self.get("_key", "")
            if _rk:
                _hit = type(self).db_load(_rk, _db)
                if _hit is not None:
                    _c = _hit.get(field)
                    if _c is not None:
                        self[field] = _c
                        cached = _c
        def _emit(child):
            BaseNode.add_to_canvas(child, id(self))
            if getattr(child, "auto_expand", False) and _manager is not None:
                _manager.push(id(child))

        if cached is not None:
            self["status"] = self._status_cached
            for child in cached:
                _emit(child)
                yield child
            return
        # _digest() may have already established Loaded/Fetched; only override
        # with "fetching" if the node hasn't been marked cached by _digest().
        if self.get("status") != self._status_cached:
            self["status"] = "fetching"
            _log("↓", type(self).__name__, self.get("_key", ""))
        list_cls = self._list_fields[field][0]
        children = list_cls()
        try:
            for child in self.stream():
                children.append(child)
                _emit(child)
                yield child
            self[field] = children
            if self.get("status") != self._status_cached:
                self["status"] = self._status_fetched
        except Exception as exc:
            self["status"]      = "error"
            self["error"]       = str(exc)
            self["error_trace"] = traceback.format_exc()
            _log("!", "ERROR", f"{type(self).__name__} {self.get('_key', '')} — {exc}")
            print(self["error_trace"], flush=True)


class Timeline(BaseNode):
    """Single root node. Streams DecadeNodes newest-first. The natural snapshot root for the whole graph."""
    _children: ClassVar[tuple] = ("decades",)
    _restore_via_payload = True
    node_colour   = "#ccad00"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        BaseNode.add_to_canvas(self, None)
        
    node_radius   = 14      ## @brief Larger than all others — it is the root of the whole graph.
    target_radius = 0       ## @brief No parent, so no target distance.
    child_spread  = 100
    charge        = -60     ## @brief Strong repulsion keeps decades from stacking on the root.

    def stream(self):
        today = date.today()
        end   = (today.year // 10) * 10
        start = (_FIRST_CHART_YEAR // 10) * 10
        for d in range(end, start - 10, -10):
            yield DecadeNode.get_or_create(str(d), {"decade": d})


class DecadeNode(BaseNode):
    """A decade. Streams YearNodes. GraphMixin-keyed so it can be found from any direction."""
    _children: ClassVar[tuple] = ("years",)
    _restore_via_payload = True
    node_colour   = "#cc7a00"

    @property
    def label(self) -> str:
        return f"{self.get('decade', '')}s"

    def node_extra(self) -> dict:
        return {"decade": self.get("decade", 0)}

    node_radius   = 10
    target_radius = 220
    link_strength = 0.8
    child_spread  = 80
    charge        = -40

    def _parent_specs(self) -> list:
        return [(Timeline, "timeline", {})]

    def stream(self):
        decade = self["decade"]
        today  = date.today()
        for y in range(min(decade + 9, today.year), max(decade, _FIRST_CHART_YEAR) - 1, -1):
            yield YearNode.get_or_create(str(y), {"year": y})


class YearNode(BaseNode):
    """A calendar year. Streams MonthNodes downward; parents() links up to its DecadeNode."""
    _children: ClassVar[tuple] = ("months",)
    _restore_via_payload = True
    node_colour   = "#e05a00"

    @property
    def label(self) -> str:
        return str(self.get("year", ""))

    def node_extra(self) -> dict:
        return {"year": self.get("year", 0)}

    node_radius   = 8
    target_radius = 160
    link_strength = 0.8
    child_spread  = 80
    charge        = -30

    def _parent_specs(self) -> list:
        decade = (self["year"] // 10) * 10
        return [(DecadeNode, str(decade), {"decade": decade})]

    def stream(self):
        year       = self["year"]
        today      = date.today()
        last_month = today.month if today.year == year else 12
        for m in range(1, last_month + 1):
            yield MonthNode.get_or_create(f"{year}-{m:02d}", {
                "year": year, "month": m, "month_name": MONTH_NAMES[m - 1]
            })


class MonthNode(BaseNode):
    """A calendar month. Streams ChartNodes downward; parents() links up to its YearNode."""
    _children: ClassVar[tuple] = ("charts",)
    _restore_via_payload = True
    node_colour   = "#c0392b"

    @property
    def label(self) -> str:
        m = self.get("month_name", "")
        y = self.get("year", "")
        return f"{m} {y}" if y else m

    def node_extra(self) -> dict:
        return {"year": self.get("year", 0), "month": self.get("month", 0)}

    node_radius   = 6
    target_radius = 120
    link_strength = 0.8
    child_spread  = 80
    charge        = -25

    def _parent_specs(self) -> list:
        year = self["year"]
        return [(YearNode, str(year), {"year": year})]

    def stream(self):
        year  = self["year"]
        month = self["month"]
        today = date.today()
        for slug in _chart_slugs:
            d = _first_chart_date_of_month(year, month, slug)
            if d is None:
                continue
            while d.month == month and d <= today:
                yield ChartNode.get_or_create(f"{slug}/{d.isoformat()}", {
                    "date":       d.isoformat(),
                    "chart_slug": slug,
                })
                d += timedelta(weeks=1)


class ChartNode(BaseNode):
    """One weekly chart. Fetches entries downward; parents() links up to its MonthNode."""
    _children: ClassVar[tuple] = ("releases",)
    _restore_via_payload = True

    @property
    def label(self) -> str:
        d    = self.get("date", "")
        slug = self.get("chart_slug", "")
        kind = "Singles" if "single" in slug else "Albums"
        try:
            return f"{kind} {date.fromisoformat(d).strftime('%d %b %Y')}"
        except (ValueError, TypeError):
            return d

    @property
    def node_colour(self) -> str:
        slug = self.get("chart_slug", "")
        return "#6a1b9a" if "single" in slug else "#3949ab"

    def node_extra(self) -> dict:
        return {"date": self.get("date", ""), "chart_slug": self.get("chart_slug", "")}
    node_radius   = 5
    target_radius = 80
    link_strength = 0.45    ## @brief Weaker than month — charts are leaf nodes of the time tree.
    child_spread  = 80
    charge        = -20

    def _parent_specs(self) -> list:
        d_obj = date.fromisoformat(self["date"])
        return [(MonthNode, f"{d_obj.year}-{d_obj.month:02d}", {
            "year": d_obj.year, "month": d_obj.month,
            "month_name": MONTH_NAMES[d_obj.month - 1],
        })]

    @staticmethod
    def _parse_html(html: str) -> list[dict]:
        entries = []
        for block in _BLOCK_RE.split(html)[1:]:
            lw_m = _LW_RE.search(block)
            if not lw_m or lw_m.group(1) != "New":
                continue
            song_m   = _SONG_RE.search(block) or _ALBUM_RE.search(block)
            artist_m = _ARTIST_RE.search(block)
            if not song_m or not artist_m:
                continue
            pos_m = _POS_RE.search(block)
            entries.append({
                "position":    int(pos_m.group(1)) if pos_m else 0,
                "title":       _last_span(song_m.group(2)),
                "song_path":   song_m.group(1),
                "artist":      artist_m.group(2).strip(),
                "artist_path": artist_m.group(1),
            })
        return entries

    def _digest(self) -> None:
        """Fetch and parse the chart week page, storing new entries."""
        if self.get("entries") is not None:
            self["status"] = self._status_cached
            return
        dt   = self["date"].replace("-", "")
        slug = self["chart_slug"]
        _log("↓", "ChartNode", self.get("_key", ""))
        self["status"] = "fetching"
        try:
            self["entries"] = self._parse_html(self._fetch(f"/charts/{slug}/{dt}/"))
            self["status"]  = self._status_fetched
        except Exception as exc:
            self["status"]      = "error"
            self["error"]       = str(exc)
            self["error_trace"] = traceback.format_exc()
            _log("!", "ERROR", f"{self.get('_key', '')} — {exc}")
            print(self["error_trace"], flush=True)

    def stream(self):
        slug = self["chart_slug"]
        for e in (self.get("entries") or []):
            song_path = e.get("song_path", "")
            yield ReleaseNode.get_or_create(song_path, {
                "title":       e.get("title", ""),
                "artist":      e.get("artist", ""),
                "position":    e.get("position", 0),
                "song_path":   song_path,
                "artist_path": e.get("artist_path", ""),
                "chart_date":  self["date"],
                "chart_slug":  slug,
                "medium":      _medium(song_path),
            })



class ArtistNode(BaseNode):
    """An artist. Fetches discography, streams ReleaseNodes for the active chart type."""
    _children: ClassVar[tuple] = ("releases",)
    _restore_via_payload = True

    @property
    def label(self) -> str:
        return self.get("name", "")

    def node_extra(self) -> dict:
        return {"artist_path": self.get("artist_path", "")}

    node_colour   = "#2e7d32"
    node_radius   = 7       ## @brief Slightly larger than releases — artists are the anchor of the cluster.
    target_radius = 50
    link_strength = 0.1     ## @brief Weak — artist–artist links should suggest proximity, not force it.
    child_spread  = 300     ## @brief Large scatter so releases don't spawn on top of each other.
    charge        = -20

    @staticmethod
    def _parse_html(html: str) -> list[dict]:
        releases = []
        for block in _BLOCK_RE.split(html)[1:]:
            song_m = _SONG_RE.search(block) or _ALBUM_RE.search(block)
            if not song_m:
                continue
            path       = song_m.group(1)
            chart_slug = "albums-chart" if path.startswith("/albums/") else "singles-chart"
            date_m     = _DATE_RE.search(block)
            releases.append({
                "title":      _last_span(song_m.group(2)),
                "path":       path,
                "chart_slug": chart_slug,
                "chart_date": date_m.group(1) if date_m else "",
            })
        return releases

    def _digest(self) -> None:
        """Fetch and parse the artist page, storing the discography entries."""
        if self.get("name", "").lower() == "various artists" or not self.get("artist_path"):
            self["status"] = self._status_cached
            return
        if self.get("entries") is not None:
            self["status"] = self._status_cached
            return
        path = self.get("artist_path", "")
        _log("↓", "ArtistNode", self.get("_key", ""))
        self["status"] = "fetching"
        try:
            self["entries"] = self._parse_html(self._fetch(path))
            self["status"]  = self._status_fetched
            object.__setattr__(self, "_just_fetched", True)
        except Exception as exc:
            self["status"]      = "error"
            self["error"]       = str(exc)
            self["error_trace"] = traceback.format_exc()
            _log("!", "ERROR", f"{self.get('_key', '')} — {exc}")
            print(self["error_trace"], flush=True)

    def __iter__(self):
        if self.get("name", "").lower() == "various artists" or not self.get("artist_path"):
            return
        # is_known prevents re-expansion of the same artist within one session
        # (e.g. artist A → release → artist A via collaboration credit).
        if self.is_known:
            yield from self.get("releases") or []
            return
        fresh = getattr(self, "_just_fetched", False)
        self.mark_known()
        yield from super().__iter__()
        if fresh:
            # Related artist suggestions are ephemeral — not stored in entries,
            # not saved to DB, only yielded on the first live fetch.
            yield from self._yield_related()

    def stream(self):
        target = set(_chart_slugs)
        count  = 0
        path   = self.get("artist_path", "")
        for r in (self.get("entries") or []):
            if r["chart_slug"] not in target or count >= 100:
                continue
            count += 1
            yield ReleaseNode.get_or_create(r["path"], {
                "title":       r["title"],
                "path":        r["path"],
                "chart_date":  r.get("chart_date", ""),
                "chart_slug":  r["chart_slug"],
                "artist_path": path,
                "medium":      _medium(r["path"]),
            })

    @staticmethod
    def _related_artists(name: str) -> list[dict]:
        if not name:
            return []
        try:
            import json as _json
            data    = _json.loads(ArtistNode._fetch_raw(ArtistNode._SUGGEST_BASE + urllib.parse.quote_plus(name)))
            artists = data.get("results", {}).get("artist", [])
            out = []
            for a in artists[:6]:
                url = a.get("url", "")
                if not url:
                    continue
                path = url.replace("https://www.officialcharts.com", "").rstrip("/") + "/"
                out.append({"artist_path": path, "name": a.get("title", "")})
            return out
        except Exception:
            return []

    def _yield_related(self):
        own_path   = self.get("artist_path", "")
        own_name   = self.get("name", "")
        seen_paths = {own_path}

        def _suggestions(name):
            for r in self._related_artists(name):
                if r["artist_path"] not in seen_paths:
                    seen_paths.add(r["artist_path"])
                    yield ArtistNode.get_or_create(r["artist_path"], {
                        "name": r["name"], "artist_path": r["artist_path"],
                    })

        yield from _suggestions(own_name)
        for part in (p.strip() for p in _COLLAB_RE.split(own_name) if p.strip()):
            yield from _suggestions(part)



class ReleaseNode(BaseNode):
    """A charting release. Links to its chart on first discovery; streams its artist."""
    _children: ClassVar[tuple] = ("artist_node", "chart_runs")
    _restore_via_payload = True

    @property
    def label(self) -> str:
        return self.get("title", "")

    def node_extra(self) -> dict:
        return {
            "chart_date":    self.get("chart_date",    ""),
            "chart_slug":    self.get("chart_slug",    ""),
            "title":         self.get("title",         ""),
            "position":      self.get("position",       0),
            "artist":        self.get("artist",        ""),
            "path":          self.get("path", "") or self.get("song_path", ""),
            "medium":        self.get("medium",        ""),
            "peak_position": self.get("peak_position", 0),
            "total_weeks":   self.get("total_weeks",   0),
            "chart_from":    self.get("chart_from",   ""),
            "chart_to":      self.get("chart_to",     ""),
            "chart_score":   self.get("chart_score",  0.0),
        }

    node_colour   = "#1a237e"
    node_radius   = 5
    target_radius = 70
    link_strength = 0.55
    child_spread  = 300     ## @brief Large scatter — releases appear far from the week so artists have room.
    charge        = -20

    @staticmethod
    def _parse_html(html: str) -> list[dict]:
        idx = html.find("Chart run")
        if idx == -1:
            return []
        section = html[idx:idx + 60_000]
        return sorted(
            [{"chart_slug": m.group(1),
              "date": f"{m.group(2)[:4]}-{m.group(2)[4:6]}-{m.group(2)[6:]}",
              "position": int(m.group(3))}
             for m in _RUN_RE.finditer(section)],
            key=lambda e: e["date"],
        )

    @staticmethod
    def _group_runs(entries: list[dict]) -> list[dict]:
        if not entries:
            return []
        runs      = []
        start     = entries[0]
        positions = [entries[0]["position"]]
        prev      = date.fromisoformat(entries[0]["date"])
        for e in entries[1:]:
            d = date.fromisoformat(e["date"])
            if (d - prev).days <= 8:  # 8 not 7 — occasional mid-week holiday shifts
                positions.append(e["position"])
            else:
                runs.append({
                    "chart_slug": start["chart_slug"],
                    "date":       start["date"],
                    "run_length": len(positions),
                    "positions":  positions,
                })
                start     = e
                positions = [e["position"]]
            prev = d
        runs.append({
            "chart_slug": start["chart_slug"],
            "date":       start["date"],
            "run_length": len(positions),
            "positions":  positions,
        })
        return runs

    def _digest(self) -> None:
        """Fetch and parse the release page, populating runs and aggregate stats."""
        if self.get("runs") is not None:
            self["status"] = self._status_cached
            return
        release_path = self.get("song_path") or self.get("path", "")
        if not release_path:
            return
        _log("↓", "ReleaseNode", self.get("_key", self.get("title", "")))
        self["status"] = "fetching"
        try:
            all_entries = self._parse_html(self._fetch(release_path))
            target  = set(_chart_slugs)
            entries = [e for e in all_entries if e["chart_slug"] in target]
            runs    = self._group_runs(entries)
            self["runs"] = runs
            if entries:
                all_positions = [p for r in runs for p in r["positions"] if 0 < p <= 100]
                dates         = sorted(e["date"] for e in entries)
                self["peak_position"] = min(all_positions) if all_positions else 0
                self["total_weeks"]   = len(entries)
                self["chart_from"]    = dates[0]  if dates else ""
                self["chart_to"]      = dates[-1] if dates else ""
                self["chart_score"]   = sum(math.log(102 - p) for p in all_positions)
            self["status"] = self._status_fetched
        except Exception as exc:
            self["status"]      = "error"
            self["error"]       = str(exc)
            self["error_trace"] = traceback.format_exc()
            _log("!", "ERROR", f"{self.get('_key', '')} — {exc}")
            print(self["error_trace"], flush=True)

    def _parent_specs(self) -> list:
        runs = self.get("runs")
        if not runs:
            return []
        return [
            (ChartNode, f"{r['chart_slug']}/{r['date']}", {
                "date": r["date"], "chart_slug": r["chart_slug"],
            })
            for r in runs
        ]

    def __iter__(self):
        self._digest()
        self._check_parents()
        if not self.get("chart_runs"):
            chart_runs = ChartList()
            for r in (self.get("runs") or []):
                key = f"{r['chart_slug']}/{r['date']}"
                chart_runs.append(ChartNode.get_or_create(key, {
                    "date": r["date"], "chart_slug": r["chart_slug"],
                }))
            self["chart_runs"] = chart_runs
        # Artist
        if not self.get("artist_node"):
            artist_path = self.get("artist_path", "")
            if artist_path:
                artist = ArtistNode.get_or_create(artist_path, {
                    "name": self.get("artist", ""), "artist_path": artist_path,
                })
                al = SerialisableNodeList(); al.append(artist)
                self["artist_node"] = al
        if self.get("artist_node"):
            yield from self["artist_node"]
        # Reverse chain: chart entry node for each run
        yield from (self.get("chart_runs") or [])


# ---------------------------------------------------------------------------
# Child-field restore declarations
#
# _list_fields and _node_fields cannot be set inside the class bodies above
# because each entry references a class defined later in the file (forward
# references).  Setting them here, after all classes exist, resolves that
# without changing the structure of the classes themselves.
# ---------------------------------------------------------------------------

Timeline._list_fields   = {"decades":  (DecadeList,           DecadeNode)}
DecadeNode._list_fields = {"years":    (YearList,             YearNode)}
YearNode._list_fields   = {"months":   (MonthList,            MonthNode)}
MonthNode._list_fields  = {"charts":   (ChartList,             ChartNode)}
ChartNode._list_fields   = {"releases": (ReleaseList,          ReleaseNode)}
ArtistNode._list_fields = {"releases": (ReleaseList,          ReleaseNode)}

# ReleaseNode has two list children. run_lengths is stored as a plain dict in
# the snapshot (written by _from_payload via dict.__init__) so it does not
# need a _node_fields entry — Node has no restore() and the plain dict works
# for all the iteration/lookup code that consumes run_lengths.
ReleaseNode._list_fields = {
    "chart_runs": (ChartList,             ChartNode),
    "artist_node": (SerialisableNodeList, ArtistNode),
}
