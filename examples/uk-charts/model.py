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
## and the app-level Rendering, Physics, GraphBehavior).
##
## @copyright Copyright (c) 2026 Tim Hosking
## @par Licence: MIT

from __future__ import annotations


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

BASE_URL      = "https://www.officialcharts.com"
_SUGGEST_BASE = "https://backstage.officialcharts.com/ajax/search-suggestions?terms="

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
    """Return the first real chart publication date in month/year for slug."""
    d = date(year, month, 1)
    while d.weekday() != 6:
        d += timedelta(days=1)
    try:
        soup    = BaseNode._fetch(f"/charts/{slug}/{d.strftime('%Y%m%d')}/")
        link  = soup.find("link", rel="canonical")
        if not link:
            return None
        # Canonical href URL date (parts[2], YYYYMMDD) is the chart-week key used
        # throughout — it differs from the display date shown on the page.
        parts = link.get("href", "").replace(BASE_URL, "").strip("/").split("/")
        if len(parts) < 3 or parts[0] != "charts":
            return None
        s       = parts[2]
        chart_d = date(int(s[:4]), int(s[4:6]), int(s[6:]))
        if chart_d.month != month:
            chart_d += timedelta(weeks=1)
        return chart_d if chart_d.month == month else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

_COLLAB_RE      = re.compile(r'\s+(?:FT\.?|FEAT\.?|FEATURING|AND|WITH|VS\.?|X|&)\s+', re.IGNORECASE)







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
class Rendering:
    label       = ""
    node_colour = "#888888"
    node_radius = 6
    colour_key  = ""
    type_label  = ""
    label_lines = []
    tooltip     = []
    stats_stale = False

    def __init__(self, **kw):
        self.__dict__.update(kw)

class Physics:
    target_radius = 100
    link_strength = 0.4
    child_spread  = 80
    charge        = -30
    collide_pad   = 3

    def __init__(self, **kw):
        self.__dict__.update(kw)


# ---------------------------------------------------------------------------
# Composite base classes
# ---------------------------------------------------------------------------

class BaseNode(DBMixin, GraphMixin, Serialisable, StreamMixin, Node):
    ## @brief Base for every serialisable, graph-registered chart node.

    auto_expand = False

    rendering = Rendering()
    physics   = Physics()

    def get_rendering(self):
        self.rendering.label = type(self).__name__.replace("Node", "")
        return self.rendering

    def get_physics(self): return self.physics

    # ── Network ───────────────────────────────────────────────────────────────
    _HEADERS     = {"User-Agent": "uk-charts-explorer/1.0"}
    _pool        = threading.Semaphore(4)


    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if _manager is not None:
            _manager.queue_request(self, "post_init")

    def handle_task(self, fn: str) -> None:
        if fn == "post_init":
            self._digest()
            self._db_save()
            self.add_to_canvas()
            self._check_parents()
        elif fn == "click":
            object.__setattr__(self, "_clicking", True)
            for _ in self:
                pass
            object.__setattr__(self, "_clicking", False)

    def add_to_canvas(self, parent_id=None) -> None:
        """Notify the server that this node is ready on the canvas."""
        if _notify is not None:
            _notify(self, parent_id if parent_id is not None else getattr(self, '_parent_id', None))

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
                hit.add_to_canvas()
                hit._check_parents()
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
    def _fetch(cls, path: str):
        from bs4 import BeautifulSoup
        url = BASE_URL + path
        req = urllib.request.Request(url, headers=cls._HEADERS)
        with cls._pool:
            for attempt in range(4):
                try:
                    with urllib.request.urlopen(req, timeout=20) as r:
                        return BeautifulSoup(r.read().decode("utf-8", errors="replace"), "html.parser")
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
                parent.add_to_canvas(parent_id)
            self.add_to_canvas(id(parent))


    @classmethod
    def _data_from_key(cls, *_) -> dict:
        """Return the minimal data dict needed to cold-create this node from its key alone.

        Used by the REST API when a node isn't in the registry or DB yet.
        Subclasses override this to derive their required fields from the key string.
        The default returns an empty dict, which is correct for nodes whose _digest()
        needs no pre-populated fields (Timeline) or that are always accessed via DB.
        """
        return {}

    def _digest(self) -> None:
        """Fetch and parse this node's own upstream page, populating own fields.

        Default is a no-op — structural nodes (Decade, Year, Month etc.) have no
        upstream page to fetch.  Override in page-backed node classes.
        """
        pass

    def __iter__(self):
        if self.auto_expand or getattr(self, "_clicking", False):
            yield from self.stream()


class Timeline(BaseNode):
    """Single root node. Streams DecadeNodes newest-first. The natural snapshot root for the whole graph."""
    _children: ClassVar[tuple] = ("decades",)
    _restore_via_payload = True
    auto_expand = True
    rendering   = Rendering(node_colour="#ccad00", node_radius=14)
    physics     = Physics(target_radius=0, child_spread=100, charge=-60)

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
    rendering = Rendering(node_colour="#cc7a00", node_radius=10)
    physics   = Physics(target_radius=220, link_strength=0.8, charge=-40)

    @classmethod
    def _data_from_key(cls, key: str) -> dict:
        return {"decade": int(key)}

    def get_rendering(self):
        self.rendering.label = f"{self.get('decade', '')}s"
        return self.rendering

    def node_extra(self) -> dict:
        return {"decade": self.get("decade", 0)}

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
    rendering = Rendering(node_colour="#e05a00", node_radius=8)
    physics   = Physics(target_radius=160, link_strength=0.8)

    @classmethod
    def _data_from_key(cls, key: str) -> dict:
        return {"year": int(key)}

    def get_rendering(self):
        self.rendering.label = str(self.get("year", ""))
        return self.rendering

    def node_extra(self) -> dict:
        return {"year": self.get("year", 0)}

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
    rendering = Rendering(node_colour="#c0392b")
    physics   = Physics(target_radius=120, link_strength=0.8, charge=-25)

    @classmethod
    def _data_from_key(cls, key: str) -> dict:
        year, month = int(key[:4]), int(key[5:7])
        return {"year": year, "month": month, "month_name": MONTH_NAMES[month - 1]}

    def get_rendering(self):
        m = self.get("month_name", "")
        y = str(self.get("year", ""))
        r = self.rendering
        r.label       = f"{m} {y}" if y else m
        r.label_lines = [m, y]
        return r

    def node_extra(self) -> dict:
        return {"year": self.get("year", 0), "month": self.get("month", 0)}

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

    @classmethod
    def _data_from_key(cls, key: str) -> dict:
        slug, date_str = key.split("/", 1)
        return {"chart_slug": slug, "date": date_str}

    rendering = Rendering(node_radius=5)
    physics   = Physics(target_radius=80, link_strength=0.45, charge=-20)

    def get_rendering(self):
        slug = self.get("chart_slug", "")
        kind = "Singles" if "single" in slug else "Albums"
        r    = self.rendering
        r.node_colour = "#6a1b9a" if "single" in slug else "#3949ab"
        try:
            line2         = date.fromisoformat(self.get("date", "")).strftime("%d %b %Y")
            r.label       = f"{kind} {line2}"
            r.label_lines = [kind, line2]
        except (ValueError, TypeError):
            r.label       = self.get("date", "")
            r.label_lines = [r.label]
        return r

    def node_extra(self) -> dict:
        return {"date": self.get("date", ""), "chart_slug": self.get("chart_slug", "")}

    def _parent_specs(self) -> list:
        d_obj = date.fromisoformat(self["date"])
        return [(MonthNode, f"{d_obj.year}-{d_obj.month:02d}", {
            "year": d_obj.year, "month": d_obj.month,
            "month_name": MONTH_NAMES[d_obj.month - 1],
        })]

    @staticmethod
    def _parse_html(soup) -> list[dict]:
        entries = []
        for item in soup.select("div.chart-item:has(a.chart-name span.new)"):
            name_a   = item.select_one("a.chart-name")
            artist_a = item.select_one("a.chart-artist")
            entries.append({
                "position":    int("".join(filter(str.isdigit, item.select_one("span.chart-key").get_text()))),
                "title":       name_a.select_one("span:not([class])").get_text(strip=True),
                "release_path": name_a["href"],
                "artist":      artist_a.get_text(strip=True),
                "artist_path": artist_a.get("href", ""),
            })
        return entries

    def _digest(self) -> None:
        """Fetch and parse the chart week page, storing new entries."""
        if self.get("entries") is not None:
            return
        dt   = self["date"].replace("-", "")
        slug = self["chart_slug"]
        _log("↓", "ChartNode", self.get("_key", ""))
        try:
            self["entries"] = self._parse_html(self._fetch(f"/charts/{slug}/{dt}/"))
        except Exception as exc:
            self["error"]       = str(exc)
            self["error_trace"] = traceback.format_exc()
            _log("!", "ERROR", f"{self.get('_key', '')} — {exc}")
            print(self["error_trace"], flush=True)

    def stream(self):
        slug = self["chart_slug"]
        for e in (self.get("entries") or []):
            release_path = e.get("release_path", "")
            yield ReleaseNode.get_or_create(release_path, {
                "title":        e.get("title", ""),
                "artist":       e.get("artist", ""),
                "position":     e.get("position", 0),
                "release_path": release_path,
                "artist_path":  e.get("artist_path", ""),
                "chart_date":   self["date"],
                "chart_slug":   slug,
                "medium":       _medium(release_path),
            })



class ArtistNode(BaseNode):
    """An artist. Fetches discography, streams ReleaseNodes for the active chart type."""
    _children: ClassVar[tuple] = ("releases",)
    _restore_via_payload = True

    @classmethod
    def _data_from_key(cls, key: str) -> dict:
        return {"artist_path": key}

    def get_rendering(self):
        self.rendering.label = self.get("name", "")
        entries = self.get("entries")
        if entries is not None:
            albums  = sum(1 for e in entries if e["release_path"].startswith("/albums/"))
            singles = sum(1 for e in entries if e["release_path"].startswith("/songs/"))
            rows = []
            if albums:  rows.append({"label": "Albums",  "value": str(albums)})
            if singles: rows.append({"label": "Singles", "value": str(singles)})
            years = sorted({e["chart_date"][:4] for e in entries if e.get("chart_date", "")[:4].isdigit()})
            if years:
                span = years[0] if years[0] == years[-1] else f"{years[0]}–{years[-1]}"
                rows.append({"label": "Chart years", "value": span})
            self.rendering.tooltip = rows
        return self.rendering

    rendering = Rendering(node_colour="#2e7d32", node_radius=7)
    physics   = Physics(target_radius=50, link_strength=0.1, child_spread=300, charge=-20)

    @staticmethod
    def _parse_html(soup) -> list[dict]:
        releases = []
        seen = set()
        for item in soup.select("main div.chart-item"):
            name_a = item.select_one("a.chart-name")
            if not name_a:
                continue
            path = name_a.get("href", "")
            if not path.startswith(("/songs/", "/albums/")) or path in seen:
                continue
            seen.add(path)
            time_tag    = item.select_one("time.date")
            releases.append({
                "title":      (name_a.select_one("span:not([class])") or name_a).get_text(strip=True),
                "release_path": path,
                "chart_slug": "albums-chart" if path.startswith("/albums/") else "singles-chart",
                "chart_date": time_tag.get("datetime", "") if time_tag else "",
            })
        return releases

    def _digest(self) -> None:
        """Fetch and parse the artist page, storing the discography entries and related artists."""
        path = self.get("artist_path", "")
        if not path:
            return
        if self.get("entries") is None:
            _log("↓", "ArtistNode", self.get("_key", ""))
            try:
                self["entries"] = self._parse_html(self._fetch(path))
            except Exception as exc:
                self["error"]       = str(exc)
                self["error_trace"] = traceback.format_exc()
                _log("!", "ERROR", f"{self.get('_key', '')} — {exc}")
                print(self["error_trace"], flush=True)
                return
        if self.get("related") is None:
            self["related"] = self._fetch_related()

    def __iter__(self):
        if self.auto_expand or getattr(self, "_clicking", False):
            yield from super().__iter__()
            for r in (self.get("related") or []):
                yield ArtistNode.get_or_create(r["artist_path"], r)

    def stream(self):
        target = set(_chart_slugs)
        count  = 0
        path   = self.get("artist_path", "")
        for r in (self.get("entries") or []):
            if r["chart_slug"] not in target or count >= 100:
                continue
            count += 1
            yield ReleaseNode.get_or_create(r["release_path"], {
                "title":        r["title"],
                "release_path": r["release_path"],
                "chart_date":   r.get("chart_date", ""),
                "chart_slug":   r["chart_slug"],
                "artist_path":  path,
                "medium":       _medium(r["release_path"]),
            })

    @staticmethod
    def _related_artists(name: str) -> list[dict]:
        if not name:
            return []
        try:
            import json as _json
            data    = _json.loads(ArtistNode._fetch_raw(_SUGGEST_BASE + urllib.parse.quote_plus(name)))
            artists = data.get("results", {}).get("artist", [])
            out = []
            for a in artists[:6]:
                url = a.get("url", "")
                if not url:
                    continue
                path = url.replace(BASE_URL, "").rstrip("/") + "/"
                out.append({"artist_path": path, "name": a.get("title", "")})
            return out
        except Exception:
            return []

    def _fetch_related(self) -> list[dict]:
        own_path   = self.get("artist_path", "")
        own_name   = self.get("name", "")
        seen_paths = {own_path}
        results    = []
        def _collect(name):
            for r in self._related_artists(name):
                if r["artist_path"] not in seen_paths:
                    seen_paths.add(r["artist_path"])
                    results.append(r)
        _collect(own_name)
        for part in (p.strip() for p in _COLLAB_RE.split(own_name) if p.strip()):
            _collect(part)
        return results



class ReleaseNode(BaseNode):
    """A charting release. Links to its chart on first discovery; streams its artist."""
    _children: ClassVar[tuple] = ("artist_node", "chart_runs")
    _restore_via_payload = True

    @classmethod
    def _data_from_key(cls, key: str) -> dict:
        return {"release_path": key, "medium": _medium(key)}

    rendering = Rendering(node_colour="#1a237e", node_radius=5)
    physics   = Physics(target_radius=70, link_strength=0.55, child_spread=300, charge=-20)

    def get_rendering(self):
        medium = self.get("medium", "single")
        runs   = self.get("runs") or []
        rows   = []
        if self.get("artist"):
            rows.append({"value": self.get("artist", "")})
        if runs:
            peak  = min(r["peak_position"] for r in runs)
            total = sum(r["total_weeks"] for r in runs)
            rows.append({"label": "Peak",  "value": f"#{peak}"})
            rows.append({"label": "Weeks", "value": str(total)})
        elif self.get("position"):
            rows.append({"label": "Position", "value": f"#{self.get('position')}"})
        if self.get("release_path"):
            rows.append({"value": self.get("release_path"), "small": True})
        r = self.rendering
        r.label       = self.get("title", "")
        r.colour_key  = f"release_{medium}"
        r.type_label  = f"release — {medium}"
        artist        = self.get("artist", "")
        r.label_lines = [artist, r.label] if artist else [r.label]
        r.tooltip     = rows
        r.stats_stale = not bool(runs)
        return r

    def node_extra(self) -> dict:
        return {"runs": self.get("runs", [])}

    @staticmethod
    def _parse_html(soup) -> list[dict]:
        entries = []
        for a in soup.select('section.gutter ol.mt-2 li a[href^="/charts/"]'):
            parts = a["href"].split("/")
            if len(parts) < 4 or len(parts[3]) != 8:
                continue
            span = a.select_one("span:first-child")
            if not span:
                continue
            try:
                pos = int(span.get_text(strip=True))
            except ValueError:
                continue
            s = parts[3]
            # URL date (YYYYMMDD) is the canonical chart-week key — matches ChartNode keys
            # built by _first_chart_date_of_month. The <time datetime> is the display date
            # and may differ.
            entries.append({
                "chart_slug": parts[2],
                "date":       f"{s[:4]}-{s[4:6]}-{s[6:]}",
                "position":   pos,
            })
        return sorted(entries, key=lambda e: e["date"])

    @staticmethod
    def _group_runs(entries: list[dict]) -> list[dict]:
        by_slug: dict[str, list[dict]] = {}
        for e in entries:
            by_slug.setdefault(e["chart_slug"], []).append(e)
        runs = []
        for slug, slug_entries in by_slug.items():
            slug_entries.sort(key=lambda e: e["date"])
            start     = slug_entries[0]
            positions = [slug_entries[0]["position"]]
            prev      = date.fromisoformat(slug_entries[0]["date"])
            for e in slug_entries[1:]:
                d = date.fromisoformat(e["date"])
                if (d - prev).days <= 8:  # 8 not 7 — occasional mid-week holiday shifts
                    positions.append(e["position"])
                else:
                    runs.append({
                        "chart_slug":    slug,
                        "date":          start["date"],
                        "total_weeks":   len(positions),
                        "peak_position": min(positions),
                        "mean_position": sum(positions) / len(positions),
                    })
                    start     = e
                    positions = [e["position"]]
                prev = d
            runs.append({
                "chart_slug":    slug,
                "date":          start["date"],
                "total_weeks":   len(positions),
                "peak_position": min(positions),
                "mean_position": sum(positions) / len(positions),
            })
        return runs

    def _digest(self) -> None:
        """Fetch and parse the release page, populating runs and aggregate stats."""
        if self.get("runs") is not None:
            return
        release_path = self.get("release_path", "")
        if not release_path:
            return
        _log("↓", "ReleaseNode", self.get("_key", self.get("title", "")))
        try:
            all_entries = self._parse_html(self._fetch(release_path))
            target  = set(_chart_slugs)
            entries = [e for e in all_entries if e["chart_slug"] in target]
            self["runs"] = self._group_runs(entries)
        except Exception as exc:
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
        if self.auto_expand or getattr(self, "_clicking", False):
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
            if not self.get("artist_node"):
                artist_path = self.get("artist_path", "")
                if artist_path:
                    artist = ArtistNode.get_or_create(artist_path, {
                        "name": self.get("artist", ""), "artist_path": artist_path,
                    })
                    al = SerialisableNodeList(); al.append(artist)
                    self["artist_node"] = al
            yield from list(self.get("artist_node") or []) + list(self.get("chart_runs") or [])


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
