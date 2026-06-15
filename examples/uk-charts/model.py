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
##
##   Graph (Week → Release ↔ Artist)
##     ChartNodes are referenced from both their MonthNode and from
##     ReleaseNode.  ReleaseNodes are referenced from both ChartNodes and
##     ArtistNodes.  This is where graph identity matters.
##
## All node classes inherit BaseNode, which wires together the node-x mixin
## stack (CacheableHTTPNode, Graph, Serialisable, Stream, Node) plus
## the app-level Rendering and Physics helpers.
##
## Threading
## ---------
## BaseNode.stream() calls CacheGet() for each child.  SQLiteNodeCache.get()
## is non-blocking — it submits work to an internal thread pool and returns
## immediately.  Pool threads perform HTTP fetching, populate(), serialisation,
## and _ready().  The calling thread (server BFS worker) is never blocked on
## HTTP.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @par Licence: MIT

from __future__ import annotations

import json
import sys
import traceback
import urllib.parse
from datetime import date, timedelta
from pathlib import Path
from typing import ClassVar, Optional

for _p in (
    Path(__file__).parent,
    Path(__file__).parent.parent,
    Path(__file__).parent.parent.parent,
):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from node_x import Node, Serialisable, SerialisableList, Stream
from app import EventHandler, current_gen, worker_gen
from cache import CacheGet
from fetch import HTTPFetch, HTTPFetchSoup
from rest import RESTEndpoint, Register, LookupKey


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL      = "https://www.officialcharts.com"
_SUGGEST_BASE = "https://backstage.officialcharts.com/ajax/search-suggestions?terms="

_FIRST_CHART_YEAR = 1952
_chart_slugs: list[str] = ["albums-chart", "singles-chart"]


def SetChartSlugs(slugs: list[str]) -> None:
    global _chart_slugs
    _chart_slugs = list(slugs)


def SetChartSlug(slug: str) -> None:
    SetChartSlugs([slug])


def _medium(path: str) -> str:
    return "album" if path.startswith("/albums/") else "single"


def _log(symbol: str, label: str, key: str) -> None:
    print(f"  {symbol} {label:<12} {key}", flush=True)




# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Typed node lists
# ---------------------------------------------------------------------------

class DecadeList(SerialisableList["DecadeNode"]):   pass
class YearList(SerialisableList["YearNode"]):       pass
class MonthList(SerialisableList["MonthNode"]):     pass
class ChartList(SerialisableList["ChartNode"]):     pass
class ReleaseList(SerialisableList["ReleaseNode"]): pass


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


# ---------------------------------------------------------------------------
# Rendering / Physics descriptors
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
    target_radius  = 100
    link_strength  = 0.4
    child_spread   = 80
    charge         = -30
    collide_pad    = 3
    link_width     = 0.8
    fx             = None  # pinned x coordinate (None = free)
    fy             = None  # pinned y coordinate (None = free)
    angle_spring   = 1.0   # angular spring strength — spreads children around parent
    radial_spring  = 0.15  # radial spring strength — holds children at target_radius

    def __init__(self, **kw):
        self.__dict__.update(kw)



# ---------------------------------------------------------------------------
# BaseNode
# ---------------------------------------------------------------------------

class BaseNode(EventHandler, RESTEndpoint, Serialisable, Stream, Node):
    """Base for every serialisable, graph-registered chart node."""

    auto_expand = False
    rendering   = Rendering()
    physics     = Physics()

    def __init__(self, _parent_id=None, **kwargs):
        super().__init__(**kwargs)
        if _parent_id is not None:
            object.__setattr__(self, "_parent_id", _parent_id)
        try:
            self.populate()
        except Exception as exc:
            self["error"]       = str(exc)
            self["error_trace"] = traceback.format_exc()
            _log("!", "ERROR", f"{self.get('_key', '')} — {exc}")
            print(self["error_trace"], flush=True)

    def populate(self) -> None:
        pass

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    # ── Rendering / physics ───────────────────────────────────────────────────

    def get_rendering(self):
        self.rendering.label = type(self).__name__.replace("Node", "")
        return self.rendering

    def get_physics(self):
        return self.physics

    def node_extra(self) -> dict:
        return {}

    @property
    def node_type(self) -> str:
        return type(self).__name__.replace("Root", "").replace("Node", "").lower()

    def to_info(self) -> dict:
        r = self.get_rendering()
        p = self.get_physics()
        name = self.node_type
        if not r.colour_key:  r.colour_key = name
        if not r.type_label:  r.type_label = name
        if not r.label_lines: r.label_lines = [r.label]
        info: dict = {
            "id":        id(self),
            "node_type": name,
            "rendering": {k: getattr(r, k) for k in vars(type(r)) if not k.startswith("_")},
            "physics":   {k: getattr(p, k) for k in vars(type(p)) if not k.startswith("_")},
        }
        info.update(self.node_extra())
        if getattr(self, "is_root", False):
            info["is_root"] = True
        return info

    # ── Key derivation (REST API cold-start) ─────────────────────────────────

    @classmethod
    def data_from_key(cls, *_) -> dict:
        return {}

    # ── Streaming ────────────────────────────────────────────────────────────

    def get_node(self, cls, key, **kwargs):
        node = LookupKey(cls.rest_slug, key)
        if node is None:
            node = CacheGet(cls, key, **kwargs)
        return node

    def on_event(self, event: dict) -> None:
        if event.get("type") == "click":
            self.Async(list, self)

    def stream(self):
        """Yield child nodes.  Override in each concrete class."""
        yield from ()

    def _store_child(self, child) -> None:
        """Route *child* into the correct list_fields collection."""
        lf = getattr(type(self), 'list_fields', None) or {}
        for field, (list_cls, item_cls) in lf.items():
            if isinstance(child, item_cls):
                col = self.get(field)
                if col is None:
                    col = list_cls()
                    self[field] = col
                col.append(child)
                return

    def __iter__(self):
        lf = getattr(type(self), 'list_fields', None)
        if not lf:
            return
        gen = worker_gen()
        if any(self.get(field) for field in lf):
            for field in lf:
                for child in (self.get(field) or []):
                    if current_gen() != gen:
                        return
                    child.post_event({"type": "click", "target": child})
            return
        for child in self.stream():
            if current_gen() != gen:
                return
            if child is not None:
                self._store_child(child)
                Register(child)
                yield child
                if getattr(type(child), 'auto_expand', False):
                    child.post_event({"type": "click", "target": child})

    # ── Back-compat shims (server.py references these) ───────────────────────

    @staticmethod
    def FetchRaw(url: str) -> str:
        return FetchRaw(url)

    _SUGGEST_BASE = _SUGGEST_BASE


# ---------------------------------------------------------------------------
# Timeline
# ---------------------------------------------------------------------------

class Timeline(BaseNode):
    """Single root node.  Streams DecadeNodes newest-first."""
    rest_slug  = "timeline"
    is_root    = True
    _children: ClassVar[tuple] = ("decades",)
    restore_via_payload = True
    auto_expand = True
    rendering   = Rendering(node_colour="#ccad00", node_radius=14)
    physics     = Physics(target_radius=0, child_spread=100, charge=-60, fx=0, fy=0,
                          angle_spring=8.0, radial_spring=0.25)

    def stream(self):
        today = date.today()
        end   = (today.year // 10) * 10
        start = (_FIRST_CHART_YEAR // 10) * 10
        for d in range(end, start - 10, -10):
            yield self.get_node(DecadeNode, str(d), decade=d, _parent_id=id(self))


# ---------------------------------------------------------------------------
# DecadeNode
# ---------------------------------------------------------------------------

class DecadeNode(BaseNode):
    """A decade.  Streams YearNodes."""
    rest_slug  = "decade"
    _children: ClassVar[tuple] = ("years",)
    restore_via_payload = True
    rendering = Rendering(node_colour="#cc7a00", node_radius=10)
    physics   = Physics(target_radius=220, link_strength=0.8, charge=-40)

    @classmethod
    def data_from_key(cls, key: str) -> dict:
        return {"decade": int(key)}

    def get_rendering(self):
        self.rendering.label = f"{self.get('decade', '')}s"
        return self.rendering

    def node_extra(self) -> dict:
        decade = self.get("decade", 0)
        return {"decade": decade, "sort_index": decade}

    def parent_specs(self) -> list:
        return [(Timeline, "timeline", {})]

    def stream(self):
        decade = self["decade"]
        today  = date.today()
        for y in range(min(decade + 9, today.year), max(decade, _FIRST_CHART_YEAR) - 1, -1):
            yield self.get_node(YearNode, str(y), year=y, _parent_id=id(self))


# ---------------------------------------------------------------------------
# YearNode
# ---------------------------------------------------------------------------

class YearNode(BaseNode):
    """A calendar year.  Streams MonthNodes."""
    rest_slug  = "year"
    _children: ClassVar[tuple] = ("months",)
    restore_via_payload = True
    rendering = Rendering(node_colour="#e05a00", node_radius=8)
    physics   = Physics(target_radius=160, link_strength=0.8)

    @classmethod
    def data_from_key(cls, key: str) -> dict:
        return {"year": int(key)}

    def get_rendering(self):
        self.rendering.label = str(self.get("year", ""))
        return self.rendering

    def node_extra(self) -> dict:
        return {"year": self.get("year", 0)}

    def parent_specs(self) -> list:
        decade = (self["year"] // 10) * 10
        return [(DecadeNode, str(decade), {"decade": decade})]

    def stream(self):
        year       = self["year"]
        today      = date.today()
        last_month = today.month if today.year == year else 12
        for m in range(1, last_month + 1):
            yield self.get_node(MonthNode, f"{year}-{m:02d}",
                           year=year, month=m, month_name=MONTH_NAMES[m - 1],
                           _parent_id=id(self))


# ---------------------------------------------------------------------------
# MonthNode
# ---------------------------------------------------------------------------

class MonthNode(BaseNode):
    """A calendar month.  Streams ChartNodes."""
    rest_slug  = "month"
    _children: ClassVar[tuple] = ("charts",)
    restore_via_payload = True
    rendering = Rendering(node_colour="#c0392b")
    physics   = Physics(target_radius=120, link_strength=0.8, charge=-25)

    @classmethod
    def data_from_key(cls, key: str) -> dict:
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

    def parent_specs(self) -> list:
        year = self["year"]
        return [(YearNode, str(year), {"year": year})]

    def stream(self):
        year  = self["year"]
        month = self["month"]
        today = date.today()
        for slug in _chart_slugs:
            d = self._first_chart_date(year, month, slug)
            if d is None:
                continue
            while d.month == month and d <= today:
                yield self.get_node(ChartNode, f"{slug}/{d.isoformat()}",
                               date=d.isoformat(), chart_slug=slug,
                               _parent_id=id(self))
                d += timedelta(weeks=1)

    @staticmethod
    def _first_chart_date(year: int, month: int, slug: str) -> "date | None":
        """Return the first real chart publication date in month/year for slug."""
        d = date(year, month, 1)
        while d.weekday() != 6:
            d += timedelta(days=1)
        try:
            soup  = HTTPFetchSoup(f"{BASE_URL}/charts/{slug}/{d.strftime('%Y%m%d')}/")
            link  = soup.find("link", rel="canonical")
            if not link:
                return None
            parts = link.get("href", "").replace("https://www.officialcharts.com", "").strip("/").split("/")
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
# ChartNode
# ---------------------------------------------------------------------------

class ChartNode(BaseNode):
    """One weekly chart.  Fetches entries; streams ReleaseNodes."""
    rest_slug  = "chart"
    _children: ClassVar[tuple] = ("releases",)
    restore_via_payload = True
    rendering = Rendering(node_radius=5)
    physics   = Physics(target_radius=80, link_strength=0.45, charge=-20)

    @classmethod
    def data_from_key(cls, key: str) -> dict:
        slug, date_str = key.split("/", 1)
        return {"chart_slug": slug, "date": date_str}

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

    def parent_specs(self) -> list:
        d_obj = date.fromisoformat(self["date"])
        return [(MonthNode, f"{d_obj.year}-{d_obj.month:02d}", {
            "year": d_obj.year, "month": d_obj.month,
            "month_name": MONTH_NAMES[d_obj.month - 1],
        })]

    def url(self) -> str:
        dt   = self["date"].replace("-", "")
        slug = self["chart_slug"]
        return f"{BASE_URL}/charts/{slug}/{dt}/"

    def populate(self) -> None:
        _log("↓", "ChartNode", self.get("_key", ""))
        soup    = HTTPFetchSoup(self.url())
        entries = []
        for item in soup.select("div.chart-item:has(a.chart-name span.new)"):
            name_a   = item.select_one("a.chart-name")
            artist_a = item.select_one("a.chart-artist")
            entries.append({
                "position":     int("".join(filter(str.isdigit, item.select_one("span.chart-key").get_text()))),
                "title":        name_a.select_one("span:not([class])").get_text(strip=True),
                "release_path": name_a["href"],
                "artist":       artist_a.get_text(strip=True),
                "artist_path":  artist_a.get("href", ""),
            })
        self["entries"] = entries

    def stream(self):
        slug = self["chart_slug"]
        for e in (self.get("entries") or []):
            release_path = e.get("release_path", "")
            yield self.get_node(ReleaseNode, release_path,
                           title=e.get("title", ""),
                           artist=e.get("artist", ""),
                           position=e.get("position", 0),
                           release_path=release_path,
                           artist_path=e.get("artist_path", ""),
                           chart_date=self["date"],
                           chart_slug=slug,
                           medium=_medium(release_path),
                           _parent_id=id(self))


# ---------------------------------------------------------------------------
# ArtistNode
# ---------------------------------------------------------------------------

class ArtistNode(BaseNode):
    """An artist.  Fetches discography; streams ReleaseNodes and related artists."""
    rest_slug  = "artist"
    _children: ClassVar[tuple] = ("releases",)
    restore_via_payload = True
    rendering = Rendering(node_colour="#2e7d32", node_radius=7)
    physics   = Physics(target_radius=50, link_strength=0.1, child_spread=300, charge=-20)

    @classmethod
    def data_from_key(cls, key: str) -> dict:
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

    def url(self) -> Optional[str]:
        path = self.get("artist_path", "")
        return BASE_URL + path if path else None

    def populate(self) -> None:
        url = self.url()
        if not url:
            return
        _log("↓", "ArtistNode", self.get("_key", ""))
        soup     = HTTPFetchSoup(url)
        releases = []
        seen     = set()
        for item in soup.select("main div.chart-item"):
            name_a = item.select_one("a.chart-name")
            if not name_a:
                continue
            path = name_a.get("href", "")
            if not path.startswith(("/songs/", "/albums/")) or path in seen:
                continue
            seen.add(path)
            time_tag = item.select_one("time.date")
            releases.append({
                "title":        (name_a.select_one("span:not([class])") or name_a).get_text(strip=True),
                "release_path": path,
                "chart_slug":   "albums-chart" if path.startswith("/albums/") else "singles-chart",
                "chart_date":   time_tag.get("datetime", "") if time_tag else "",
            })
        self["entries"] = releases
        self["mbid"]    = self._fetch_mbid()

    def stream(self):
        target = set(_chart_slugs)
        count  = 0
        path   = self.get("artist_path", "")
        for r in (self.get("entries") or []):
            if r["chart_slug"] not in target or count >= 100:
                continue
            count += 1
            yield self.get_node(ReleaseNode, r["release_path"],
                           title=r["title"],
                           release_path=r["release_path"],
                           chart_date=r.get("chart_date", ""),
                           chart_slug=r["chart_slug"],
                           artist_path=path,
                           medium=_medium(r["release_path"]),
                           _parent_id=id(self))
    def _fetch_mbid(self) -> str:
        name = self.get("name", "")
        if not name:
            return ""
        try:
            data = json.loads(HTTPFetch(
                f"https://musicbrainz.org/ws/2/artist/?query={urllib.parse.quote_plus(name)}&fmt=json"
            ))
            artists = data.get("artists", [])
            if artists:
                return artists[0].get("id", "")
        except Exception:
            pass
        return ""



# ---------------------------------------------------------------------------
# ReleaseNode
# ---------------------------------------------------------------------------

class ReleaseNode(BaseNode):
    """A charting release.  Fetches chart history; streams chart runs and artist."""
    rest_slug  = "release"
    _children: ClassVar[tuple] = ("artist_node", "chart_runs")
    restore_via_payload = True
    rendering = Rendering(node_colour="#1a237e", node_radius=5)
    physics   = Physics(target_radius=70, link_strength=0.55, child_spread=300, charge=-20)

    @classmethod
    def data_from_key(cls, key: str) -> dict:
        return {"release_path": key, "medium": _medium(key)}

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
        r             = self.rendering
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

    def url(self) -> Optional[str]:
        path = self.get("release_path", "")
        return BASE_URL + path if path else None

    def populate(self) -> None:
        url = self.url()
        if not url:
            return
        _log("↓", "ReleaseNode", self.get("_key", self.get("title", "")))
        soup    = HTTPFetchSoup(url)
        target  = set(_chart_slugs)
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
            entries.append({
                "chart_slug": parts[2],
                "date":       f"{s[:4]}-{s[4:6]}-{s[6:]}",
                "position":   pos,
            })
        entries = sorted(entries, key=lambda e: e["date"])
        self["runs"] = self._group_runs([e for e in entries if e["chart_slug"] in target])

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
                if (d - prev).days <= 8:
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

    def parent_specs(self) -> list:
        runs = self.get("runs")
        if not runs:
            return []
        return [
            (ChartNode, f"{r['chart_slug']}/{r['date']}", {
                "date": r["date"], "chart_slug": r["chart_slug"],
            })
            for r in runs
        ]

    def get_stats(self) -> dict:
        if not self.get("runs"):
            try:
                self.populate()
            except Exception:
                pass
        r = self.get_rendering()
        return {"tooltip": r.tooltip, "stats_stale": r.stats_stale}

    def stream(self):
        for r in (self.get("runs") or []):
            key = f"{r['chart_slug']}/{r['date']}"
            yield self.get_node(ChartNode, key,
                           date=r["date"], chart_slug=r["chart_slug"],
                           _parent_id=id(self))
        artist_path = self.get("artist_path", "")
        if artist_path:
            yield self.get_node(ArtistNode, artist_path,
                           name=self.get("artist", ""),
                           artist_path=artist_path,
                           _parent_id=id(self))


# ---------------------------------------------------------------------------
# Child-field restore declarations (forward references resolved here)
# ---------------------------------------------------------------------------

Timeline.list_fields   = {"decades":  (DecadeList,   DecadeNode)}
DecadeNode.list_fields = {"years":    (YearList,     YearNode)}
YearNode.list_fields   = {"months":   (MonthList,    MonthNode)}
MonthNode.list_fields  = {"charts":   (ChartList,    ChartNode)}
ChartNode.list_fields  = {"releases": (ReleaseList,  ReleaseNode)}
ArtistNode.list_fields = {"releases": (ReleaseList,  ReleaseNode)}
ReleaseNode.list_fields = {
    "chart_runs":  (ChartList,            ChartNode),
    "artist_node": (SerialisableList, ArtistNode),
}

# ---------------------------------------------------------------------------
