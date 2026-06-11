## @file model.py
##
## @brief Node graph model for the UK Charts Explorer.
##
## Structure
## ---------
## The graph has two distinct structural zones:
##
##   Tree spine (Timeline → Decade → Year → Month → Week)
##     Each node has exactly one parent.  No shared references, no cycles.
##     GraphMixin is inherited for get_or_create convenience, but $ref
##     serialisation never fires here.
##
##   Graph (Week → Release ↔ Artist)
##     WeekNodes are referenced from both their MonthNode and from
##     ReleaseNode.chart_weeks.  ReleaseNodes are referenced from both
##     WeekNodes and ArtistNodes.  This is where graph identity matters
##     and $ref serialisation keeps snapshots correct.
##
## All node classes inherit ChartNode, which wires together the full
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
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path
from typing import ClassVar, Iterator

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


def set_node_db(db) -> None:
    """Point the model at a NodeDB instance for cache-aside reads and writes."""
    global _db
    _db = db


def _log(symbol: str, label: str, key: str) -> None:
    print(f"  {symbol} {label:<12} {key}", flush=True)


_FIRST_CHART_YEAR = 1952

_chart_slugs: list[str] = ["albums-chart"]

def set_chart_slugs(slugs: list[str]) -> None:
    global _chart_slugs
    _chart_slugs = list(slugs)

def set_chart_slug(slug: str) -> None:
    set_chart_slugs([slug])


def _medium(path: str) -> str:
    return "album" if path.startswith("/albums/") else "single"




def _related_artists(name: str) -> list[dict]:
    if not name:
        return []
    try:
        import json as _json
        data    = _json.loads(ChartNode._fetch_raw(ChartNode._SUGGEST_BASE + urllib.parse.quote_plus(name)))
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


def _first_chart_date_of_month(year: int, month: int, slug: str) -> "date | None":
    """Return the first real chart publication date in month/year for slug.

    Fetches the chart for the first Sunday of the month; the canonical URL in
    the response tells us the nearest actual publication date for that era.
    """
    d = date(year, month, 1)
    while d.weekday() != 6:
        d += timedelta(days=1)
    try:
        html   = ChartNode._fetch(f"/charts/{slug}/{d.strftime('%Y%m%d')}/")
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


def _parse_week_html(html: str) -> list[dict]:
    """Return only new entries (LW = New) for the given week chart page."""
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


def _parse_release_html(html: str) -> list[dict]:
    """Return raw chart run entries sorted by date."""
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


def _chart_runs(entries: list[dict]) -> list[dict]:
    """Group sorted chart entries into consecutive runs; return start date and run length."""
    if not entries:
        return []
    runs   = []
    start  = entries[0]
    length = 1
    prev   = date.fromisoformat(entries[0]["date"])
    for e in entries[1:]:
        d = date.fromisoformat(e["date"])
        if (d - prev).days <= 8:  # 8 not 7 — occasional mid-week holiday shifts
            length += 1
        else:
            runs.append({"date": start["date"], "run_length": length, "chart_slug": start["chart_slug"]})
            start  = e
            length = 1
        prev = d
    runs.append({"date": start["date"], "run_length": length, "chart_slug": start["chart_slug"]})
    return runs


def make_path_from(date_str: str, slug: str) -> "WeekNode":
    """Create every node from DecadeNode down to WeekNode without triggering any stream."""
    d      = date.fromisoformat(date_str)
    decade = (d.year // 10) * 10
    DecadeNode.get_or_create(str(decade), {"decade": decade})
    YearNode.get_or_create(str(d.year),   {"year": d.year})
    month_node = MonthNode.get_or_create(f"{d.year}-{d.month:02d}", {
        "year": d.year, "month": d.month, "month_name": MONTH_NAMES[d.month - 1],
    })
    wk = WeekNode.get_or_create(f"{slug}/{date_str}", {
        "date": date_str, "chart_slug": slug,
        "label": f"New Entries {d.strftime('%d %b %Y')}",
    })
    weeks = month_node.get("weeks")
    if weeks is None:
        weeks = WeekList()
        month_node["weeks"] = weeks
    if wk not in weeks:
        weeks.append(wk)  # release may chart in a week MonthNode hasn't streamed yet
    return wk


def _parse_artist_html(html: str) -> list[dict]:
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


# ---------------------------------------------------------------------------
# Typed node lists
# ---------------------------------------------------------------------------

class DecadeList(SerialisableNodeList["DecadeNode"]):         pass
class YearList(SerialisableNodeList["YearNode"]):             pass
class MonthList(SerialisableNodeList["MonthNode"]):           pass
class WeekList(SerialisableNodeList["WeekNode"]):             pass
class ReleaseList(SerialisableNodeList["ReleaseNode"]):       pass


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


# ---------------------------------------------------------------------------
# GraphBehavior
# ---------------------------------------------------------------------------

class GraphBehavior:
    ## @brief Mixin: graph-level operational flags carried by every node class.
    ##
    ## Flags here govern how the server handles the node in the graph — whether
    ## it participates in BFS auto-expansion, whether it is searchable, etc.
    ## The server reads these via getattr so adding a new flag to this mixin
    ## is sufficient; no server-side dispatch table needs updating.
    ##
    ## Default is False for all flags — nodes opt in by overriding at class level.

    auto_expand = False
    ## @brief When True the server expands this node automatically without
    ##        waiting for a user click.


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
# TemporalMixin
# ---------------------------------------------------------------------------

class TemporalMixin:
    """Mixin for nodes in the time spine.

    Iterating a temporal node yields self then each ancestor in turn
    (week → month → year → decade).  Reversing that gives root-first
    order (decade → year → month → week), which is exactly what the
    server needs to build parent_id chains without any knowledge of the
    temporal structure.
    """

    def __iter__(self):
        yield self
        if callable(getattr(self, "parents", None)):
            for parent in self.parents():
                if isinstance(parent, TemporalMixin):
                    yield from parent

    def __reversed__(self):
        return reversed(list(self))


# ---------------------------------------------------------------------------
# Composite base classes
# ---------------------------------------------------------------------------

class ChartNode(DBMixin, GraphBehavior, RenderMixin, PhysicsMixin, GraphMixin, Serialisable, StreamMixin, Node):
    ## @brief Base for every serialisable, graph-registered chart node.
    ##
    ## Combines the full mixin stack so subclasses declare only what makes
    ## them distinct.

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

    @classmethod
    def get_or_create(cls, key, data=None):
        # Check _registry via __dict__ (not inheritance) so each concrete class
        # gets its own registry; a miss on the subclass doesn't fall through to
        # a parent class registry that holds a different node type.
        if _db is not None and ("_registry" not in cls.__dict__ or key not in cls._registry):
            hit = cls.db_load(key, _db)
            if hit is not None:
                if "_registry" not in cls.__dict__:
                    cls._registry = {}
                cls._registry[key] = hit
                return hit
        node = super().get_or_create(key, data)
        # Save a stub so any node is findable in DB before its stream() is called.
        # Guard on the primary children field prevents redundant saves once populated.
        children = getattr(cls, "_children", ())
        if _db is not None and (not children or node.get(children[0]) is None):
            node._db_save()
        return node

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
                ChartNode._last_req = time.monotonic()
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

    def stream(self, _data=None):
        field = self._children[0] if self._children else None
        if not field:
            return
        cached = self.get(field)
        if cached is not None:
            self["status"] = self._status_cached
            _log("●", type(self).__name__, self.get("_key", ""))
            yield from cached
            return
        list_cls = self._list_fields[field][0]
        children = list_cls()
        self["status"] = "fetching"
        _log("↓", type(self).__name__, self.get("_key", ""))
        try:
            for child in self.populate_self():
                children.append(child)
                yield child
            self[field] = children
            self["status"] = self._status_fetched
            self._db_save()
        except Exception as exc:
            self["status"] = "error"
            self["error"]  = str(exc)

    def populate_self(self):
        # Subclasses override this to yield their children.
        # stream() wraps it with cache check, status, logging, save, and error handling.
        return iter([])


class TemporalChartNode(TemporalMixin, ChartNode):
    ## @brief Base for nodes that live on the time spine (decade → week).
    ##
    ## Adds ``TemporalMixin`` iteration so the server can walk the spine
    ## root-first without knowing which level a node occupies.
    pass


class Timeline(ChartNode):
    """Single root node. Streams DecadeNodes newest-first. The natural snapshot root for the whole graph."""
    _children: ClassVar[tuple] = ("decades",)
    _restore_via_payload = True
    node_colour   = "#ccad00"
    node_radius   = 14      ## @brief Larger than all others — it is the root of the whole graph.
    target_radius = 0       ## @brief No parent, so no target distance.
    child_spread  = 100
    charge        = -60     ## @brief Strong repulsion keeps decades from stacking on the root.

    def populate_self(self):
        today = date.today()
        end   = (today.year // 10) * 10
        start = (_FIRST_CHART_YEAR // 10) * 10
        for d in range(end, start - 10, -10):
            yield DecadeNode.get_or_create(str(d), {"decade": d})


class DecadeNode(TemporalChartNode):
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

    def populate_self(self):
        decade = self["decade"]
        today  = date.today()
        for y in range(min(decade + 9, today.year), max(decade, _FIRST_CHART_YEAR) - 1, -1):
            yield YearNode.get_or_create(str(y), {"year": y})


class YearNode(TemporalChartNode):
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

    def parents(self) -> Iterator["DecadeNode"]:
        decade = (self["year"] // 10) * 10
        yield DecadeNode.get_or_create(str(decade), {"decade": decade})

    def populate_self(self):
        year       = self["year"]
        today      = date.today()
        last_month = today.month if today.year == year else 12
        for m in range(1, last_month + 1):
            yield MonthNode.get_or_create(f"{year}-{m:02d}", {
                "year": year, "month": m, "month_name": MONTH_NAMES[m - 1]
            })


class MonthNode(TemporalChartNode):
    """A calendar month. Streams WeekNodes downward; parents() links up to its YearNode."""
    _children: ClassVar[tuple] = ("weeks",)
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

    def parents(self) -> Iterator["YearNode"]:
        yield YearNode.get_or_create(str(self["year"]), {"year": self["year"]})

    def populate_self(self):
        year  = self["year"]
        month = self["month"]
        today = date.today()
        for slug in _chart_slugs:
            d = _first_chart_date_of_month(year, month, slug)
            if d is None:
                continue
            while d.month == month and d <= today:
                yield WeekNode.get_or_create(f"{slug}/{d.isoformat()}", {
                    "date":       d.isoformat(),
                    "chart_slug": slug,
                })
                d += timedelta(weeks=1)


class WeekNode(TemporalChartNode):
    """One weekly chart. Fetches entries downward; parents() links up to its MonthNode."""
    _children: ClassVar[tuple] = ("releases",)
    _restore_via_payload = True

    @property
    def label(self) -> str:
        d = self.get("date", "")
        try:
            return f"New Entries {date.fromisoformat(d).strftime('%d %b %Y')}"
        except (ValueError, TypeError):
            return d

    def node_extra(self) -> dict:
        return {"date": self.get("date", ""), "chart_slug": self.get("chart_slug", "")}

    node_colour   = "#3949ab"
    node_radius   = 5
    target_radius = 80
    link_strength = 0.45    ## @brief Weaker than the spine — weeks are leaf nodes on the temporal chain.
    child_spread  = 80
    charge        = -20

    def parents(self) -> Iterator["MonthNode"]:
        d_obj = date.fromisoformat(self["date"])
        yield MonthNode.get_or_create(f"{d_obj.year}-{d_obj.month:02d}", {
            "year": d_obj.year, "month": d_obj.month,
            "month_name": MONTH_NAMES[d_obj.month - 1],
        })

    def populate_self(self):
        dt   = self["date"].replace("-", "")
        slug = self["chart_slug"]
        for e in _parse_week_html(self._fetch(f"/charts/{slug}/{dt}/")):
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


class ArtistNode(ChartNode):
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

    def stream(self, _data=None):
        if self.get("name", "").lower() == "various artists" or not self.get("artist_path"):
            return
        # is_known prevents re-expansion of the same artist within one session
        # (e.g. artist A → release → artist A via collaboration credit).
        if self.is_known:
            yield from self.get("releases") or []
            return
        fresh = self.get("releases") is None  # capture before mark_known/super may set it
        self.mark_known()
        yield from super().stream()
        if fresh:
            # Related artist suggestions are ephemeral — not stored in releases,
            # not saved to DB, only yielded on the first live fetch.
            yield from self._yield_related()

    def populate_self(self):
        path   = self.get("artist_path", "")
        target = set(_chart_slugs)
        count  = 0
        for r in _parse_artist_html(self._fetch(path)):
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

    def _yield_related(self):
        own_path   = self.get("artist_path", "")
        own_name   = self.get("name", "")
        seen_paths = {own_path}

        def _suggestions(name):
            for r in _related_artists(name):
                if r["artist_path"] not in seen_paths:
                    seen_paths.add(r["artist_path"])
                    yield ArtistNode.get_or_create(r["artist_path"], {
                        "name": r["name"], "artist_path": r["artist_path"],
                    })

        yield from _suggestions(own_name)
        # Split collaboration credits ("ARTIST FT OTHER") and search each part
        # so both collaborators surface as related suggestions.
        for part in (p.strip() for p in _COLLAB_RE.split(own_name) if p.strip()):
            yield from _suggestions(part)


class ReleaseNode(ChartNode):
    """A charting release. Wires itself into the time spine on first discovery; streams its artist."""
    _children: ClassVar[tuple] = ("artist_node", "chart_weeks")
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

    def fetch_timeline(self) -> None:
        """Fetch the full chart run and populate chart_weeks. Idempotent."""
        if self.get("chart_weeks") is not None:
            return
        # get_or_create checks DB on a registry miss, but a node created earlier
        # in the same session stays in the registry as a stub.  Check DB here so
        # a previously saved timeline is not re-fetched over the network.
        if _db is not None:
            _rk = self.get("_key") or self.get("song_path") or self.get("path", "")
            if _rk:
                _cached_r = _db.load(ReleaseNode, _rk)
                if _cached_r is not None and _cached_r.get("chart_weeks") is not None:
                    for _f in ("chart_weeks", "run_lengths", "peak_position",
                               "total_weeks", "chart_from", "chart_to", "chart_score"):
                        _v = _cached_r.get(_f)
                        if _v is not None:
                            self[_f] = _v
                    self["status"] = self._status_cached
                    _log("●", "ReleaseNode", self.get("_key", self.get("title", "")))
                    return
        release_path = self.get("song_path") or self.get("path", "")
        if not release_path:
            return
        _log("↓", "ReleaseNode", self.get("_key", self.get("title", "")))
        self["status"] = "fetching"
        try:
            all_entries = _parse_release_html(self._fetch(release_path))
            # Only process entries for the active chart type(s).
            target  = set(_chart_slugs)
            entries = [e for e in all_entries if e["chart_slug"] in target]
            runs = _chart_runs(entries)
            chart_weeks = WeekList()
            rl = Node()
            for run in runs:
                wk = make_path_from(run["date"], run["chart_slug"])
                chart_weeks.append(wk)
                # Key uses the normalised date so it matches add_week_spine's lookup.
                rl[f"{run['chart_slug']}/{wk['date']}"] = run["run_length"]
            self["chart_weeks"] = chart_weeks
            self["run_lengths"] = rl
            # Chart stats for tooltip + visual weight
            if entries:
                positions = [e["position"] for e in entries if 0 < e["position"] <= 100]
                dates     = sorted(e["date"] for e in entries)
                self["peak_position"] = min(positions) if positions else 0
                self["total_weeks"]   = len(entries)
                self["chart_from"]    = dates[0]  if dates else ""
                self["chart_to"]      = dates[-1] if dates else ""
                # log-weighted score: rewards high positions AND longevity
                self["chart_score"]   = sum(math.log(102 - p) for p in positions)
            self["status"] = self._status_fetched
            self._db_save()
        except Exception as exc:
            self["status"] = "error"
            self["error"]  = str(exc)

    def stream(self, _data=None):
        self.fetch_timeline()
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
        # Reverse chain: all weeks this release charted in
        yield from (self.get("chart_weeks") or [])


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
MonthNode._list_fields  = {"weeks":    (WeekList,             WeekNode)}
WeekNode._list_fields   = {"releases": (ReleaseList,          ReleaseNode)}
ArtistNode._list_fields = {"releases": (ReleaseList,          ReleaseNode)}

# ReleaseNode has two list children and one plain-Node child.
# WeekNode and ArtistNode are both defined by this point so no forward refs.
ReleaseNode._list_fields = {
    "chart_weeks": (WeekList,             WeekNode),
    "artist_node": (SerialisableNodeList, ArtistNode),
}
ReleaseNode._node_fields = {"run_lengths": Node}
