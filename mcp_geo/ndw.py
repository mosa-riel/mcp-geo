"""Road works, closures and incidents in the Netherlands from NDW open data (DATEX II 3).

Source: Nationaal Dataportaal Wegverkeer, https://opendata.ndw.nu (public, no key; terms at
https://docs.ndw.nu/algemeen/data-gebruiksvoorwaarden). Measures of Rijkswaterstaat,
provinces and municipalities:

- planningsfeed_wegwerkzaamheden_en_evenementen.xml.gz (~15 MB, ~13k situations): planned and
  running road works, closures of roads and ramps, speed limits, events. Fetched at most every
  30 min and only when asked; the old data is served while a refresh runs (parsing takes ~20 s
  on a small CPU).
- actueel_beeld.xml.gz (~0.3 MB): what is in force right now, including accidents and
  broken-down vehicles. Fetched at most every 3 min.

Measures carry WGS84 geometry but no road numbers. A route is matched by distance and driving
direction (a closed on-ramp towards Eindhoven does not count for a trip towards Maastricht);
around a place by distance, optionally with a road number found in the text.
"""

from __future__ import annotations

import asyncio
import gzip
import io
import math
import re
import time
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

import httpx

BASE = "https://opendata.ndw.nu/"
PLANNING = "planningsfeed_wegwerkzaamheden_en_evenementen.xml.gz"
CURRENT = "actueel_beeld.xml.gz"
_MAX_AGE = {PLANNING: 1800, CURRENT: 180}
_XSI = "{http://www.w3.org/2001/XMLSchema-instance}type"

# What a record means for a driver, most severe first (the order of the output).
SEVERITY = ["weg dicht", "toerit dicht", "afrit dicht", "verbindingsweg dicht", "parallelbaan dicht", "ongeval",
            "rijstrook dicht of versmald", "pechgeval of obstakel", "brug open", "verkeersregelaars",
            "werkzaamheden", "fiets- of voetpad dicht", "evenement", "drukte verwacht", "snelheidsbeperking", "omleiding"]
_CLOSED = {"mainCarriageway": "weg dicht", "entrySlipRoad": "toerit dicht", "exitSlipRoad": "afrit dicht",
           "connectingCarriageway": "verbindingsweg dicht", "parallelCarriageway": "parallelbaan dicht"}
_NOISE = re.compile(r"Volg route\s*(\*\*|\"\")|\bWO\d+\b|\(\d{4,}\)|,?\s*A2VK - [A-Z&]+", re.I)
_ROAD = re.compile(r"\b([AN]\d{1,3})\b")


def _tag(t: str) -> str:
    return t.rsplit("}", 1)[-1]


def _dt(s: str | None) -> datetime | None:
    if not s:
        return None
    s = re.sub(r"(\.\d{6})\d+", r"\1", s.strip()).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _kinds(rec: ET.Element, carriageway: str) -> list[str]:
    typ = (rec.get(_XSI) or "").split(":")[-1]
    out = []
    for x in rec.iter():
        t, v = _tag(x.tag), (x.text or "").strip()
        if t == "roadOrCarriagewayOrLaneManagementType":
            if v in ("carriagewayClosures", "roadClosed"):
                out.append(_CLOSED.get(carriageway, "weg dicht"))
            elif v in ("laneClosures", "narrowLanes", "lanesDeviated"):
                out.append("rijstrook dicht of versmald")
        elif t == "reroutingManagementType" and v in ("followDiversionSigns", "followLocalDiversion"):
            out.append("omleiding")
        elif t == "generalNetworkManagementType":
            out.append({"bridgeSwingInOperation": "brug open",
                        "trafficBeingManuallyDirected": "verkeersregelaars"}.get(v, ""))
    out += {"SpeedManagement": ["snelheidsbeperking"], "Accident": ["ongeval"],
            "VehicleObstruction": ["pechgeval of obstakel"], "GeneralObstruction": ["pechgeval of obstakel"],
            "PublicEvent": ["evenement"], "MaintenanceWorks": ["werkzaamheden"], "ConstructionWorks": ["werkzaamheden"],
            "AbnormalTraffic": ["drukte verwacht"]}.get(typ, [])
    return [k for k in out if k]


def _situation(el: ET.Element, live: bool) -> dict | None:
    kinds: set[str] = set()
    texts: list[str] = []
    source = ""
    periods: list[tuple[datetime | None, datetime | None]] = []
    lines: list[tuple[str, list[tuple[float, float]]]] = []  # (carriageway, points in driving order)
    points: list[tuple[str, tuple[float, float]]] = []
    works: set[str] = set()  # carriageways with works
    diversion = both = False
    for rec in el:
        if _tag(rec.tag) != "situationRecord":
            continue
        typ = (rec.get(_XSI) or "").split(":")[-1]
        cw = next((x.text for x in rec.iter() if _tag(x.tag) == "carriageway" and x.text), "mainCarriageway")
        found = _kinds(rec, cw)
        kinds.update(found)
        diversion |= "omleiding" in found
        if typ in ("MaintenanceWorks", "ConstructionWorks"):
            works.add(cw)
        for x in rec.iter():
            t = _tag(x.tag)
            if t == "sourceName":
                source = source or " ".join(v.text or "" for v in x.iter() if _tag(v.tag) == "value").strip()
            elif t in ("generalPublicComment", "causeDescription"):
                texts += [v.text.strip() for v in x.iter() if _tag(v.tag) == "value" and v.text and v.text.strip()]
            elif t == "alertCAffectedDirection" and x.text == "both":
                both = True
            elif typ == "ReroutingManagement":
                continue  # its geometry is the diversion route, not the closed road
            elif t == "posList" and x.text:
                nums = [float(n) for n in x.text.split()]
                line = list(zip(nums[0::2], nums[1::2]))
                if len(line) > 40:  # keep the shape and both ends, drop detail
                    step = len(line) / 39
                    line = [line[int(i * step)] for i in range(39)] + [line[-1]]
                lines.append((cw, line))
            elif t == "pointCoordinates":
                lat, lon = x.findtext("{*}latitude"), x.findtext("{*}longitude")
                if lat and lon:
                    points.append((cw, (float(lat), float(lon))))
        vp = [(_dt(p.findtext("{*}startOfPeriod")), _dt(p.findtext("{*}endOfPeriod")))
              for p in rec.iter() if _tag(p.tag) == "validPeriod"]
        if vp:
            periods += vp
        else:
            spec = next((x for x in rec.iter() if _tag(x.tag) == "validityTimeSpecification"), None)
            if spec is not None:
                periods.append((_dt(spec.findtext("{*}overallStartTime")), _dt(spec.findtext("{*}overallEndTime"))))
    text = " | ".join(dict.fromkeys(_NOISE.sub("", t).strip(" ,|") for t in texts if t != source))
    text = re.sub(r"\s{2,}", " ", text).strip(" |")
    # Rijkswaterstaat often reports a closure as works plus a diversion ("De toerit Born ri.
    # Eindhoven is dicht") without a separate closure record.
    says_closed = re.search(r"\b(dicht|afgesloten)\b", text, re.I)
    if diversion or says_closed:
        kinds.update(_CLOSED[c] for c in works if c in _CLOSED and c != "mainCarriageway")
        if "mainCarriageway" in works and diversion and says_closed:
            kinds.add("weg dicht")
    # "N297 Aldenhofweg (fietspad) Born ... Weg dicht in een richting": the path is closed, not the road
    if re.search(r"\((fiets|voet)pad\)|\b(fiets|voet)pad(en)? (afgesloten|dicht)\b", text, re.I):
        closed = {k for k in kinds if k.endswith(" dicht")}
        if closed:
            kinds = (kinds - closed) | {"fiets- of voetpad dicht"}
    # Road authorities label their measure ("Weg dicht in beide richtingen", "Verminderd aantal
    # rijstroken beschikbaar"); a lanes-only measure is not a closed road, whatever its records say.
    if re.search(r"verminderd aantal rijstroken", text, re.I) and not re.search(r"\bweg dicht\b", text, re.I) \
            and "weg dicht" in kinds:
        kinds = (kinds - {"weg dicht"}) | {"rijstrook dicht of versmald"}
    if not kinds or not (lines or points):
        return None
    road = _ROAD.search(text)
    if "beide richtingen" in text.lower():
        both = True
    return {"id": el.get("id", ""), "kinds": kinds, "text": text[:240], "source": source,
            "periods": sorted(set(periods), key=lambda p: p[0] or datetime.min.replace(tzinfo=timezone.utc)),
            "lines": lines, "points": points, "road": road.group(1) if road else "", "both": both, "live": live}


def parse(data: bytes, live: bool) -> list[dict]:
    out = []
    for _, el in ET.iterparse(gzip.GzipFile(fileobj=io.BytesIO(data)), events=("end",)):
        if _tag(el.tag) == "situation":
            m = _situation(el, live)
            if m:
                out.append(m)
            el.clear()
    return out


class Feeds:
    """The two feeds, parsed and cached; refreshed lazily when asked and old."""

    def __init__(self) -> None:
        self._data: dict[str, list[dict]] = {}
        self._at: dict[str, float] = {}
        self._busy: dict[str, asyncio.Task] = {}

    async def _load(self, name: str) -> None:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as cli:
            r = await cli.get(BASE + name)
            r.raise_for_status()
        self._data[name] = await asyncio.to_thread(parse, r.content, name == CURRENT)
        self._at[name] = time.monotonic()

    async def get(self, name: str, wait: float = 45.0) -> list[dict]:
        old = name not in self._at or time.monotonic() - self._at[name] > _MAX_AGE[name]
        if old and (name not in self._busy or self._busy[name].done()):
            self._busy[name] = asyncio.create_task(self._load(name))
        if name not in self._data:  # first time: wait for it
            await asyncio.wait_for(asyncio.shield(self._busy[name]), timeout=wait)
        return self._data.get(name, [])

    def age_min(self, name: str) -> int | None:
        return round((time.monotonic() - self._at[name]) / 60) if name in self._at else None


feeds = Feeds()


def active(m: dict, start: datetime, end: datetime) -> list[tuple[datetime | None, datetime | None]]:
    """The periods of measure m that overlap [start, end]."""
    lo, hi = datetime.min.replace(tzinfo=timezone.utc), datetime.max.replace(tzinfo=timezone.utc)
    return [(a, b) for a, b in m["periods"] if (a or lo) <= end and (b or hi) >= start]


# ── geometry (local metres; fine at the scale of the Netherlands) ─────────────────────────────

_KX = 111320 * math.cos(math.radians(52.0))
_KY = 110540


def _xy(p: tuple[float, float]) -> tuple[float, float]:
    return p[1] * _KX, p[0] * _KY


class Route:
    """A route polyline with a grid of its segments, to find measures on it."""

    CELL = 250.0

    def __init__(self, coords: list[tuple[float, float]]) -> None:
        self.coords = coords
        self.pts = [_xy(p) for p in coords]
        self.cum = [0.0]
        for a, b in zip(self.pts, self.pts[1:]):
            self.cum.append(self.cum[-1] + math.dist(a, b))
        self.grid: dict[tuple[int, int], list[int]] = {}
        for i, (a, b) in enumerate(zip(self.pts, self.pts[1:])):
            for cx in range(int(min(a[0], b[0]) // self.CELL), int(max(a[0], b[0]) // self.CELL) + 1):
                for cy in range(int(min(a[1], b[1]) // self.CELL), int(max(a[1], b[1]) // self.CELL) + 1):
                    self.grid.setdefault((cx, cy), []).append(i)
        xs, ys = zip(*self.pts)
        self.box = (min(xs) - 300, min(ys) - 300, max(xs) + 300, max(ys) + 300)

    def nearest(self, p: tuple[float, float]) -> tuple[float, int, float] | None:
        """→ (distance m, segment index, metres along the route) of the closest segment nearby."""
        cx, cy = int(p[0] // self.CELL), int(p[1] // self.CELL)
        best = None
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for i in self.grid.get((cx + dx, cy + dy), ()):
                    a, b = self.pts[i], self.pts[i + 1]
                    vx, vy = b[0] - a[0], b[1] - a[1]
                    ll = vx * vx + vy * vy
                    t = 0.0 if ll == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * vx + (p[1] - a[1]) * vy) / ll))
                    d = math.dist(p, (a[0] + t * vx, a[1] + t * vy))
                    if best is None or d < best[0]:
                        best = (d, i, self.cum[i] + t * math.sqrt(ll))
        return best

    def at(self, along: float) -> tuple[float, float]:
        """(lat, lon) of the route point nearest to `along` metres (clamped to the route)."""
        i = min(range(len(self.cum)), key=lambda j: abs(self.cum[j] - along))
        return self.coords[i]

    def follows(self, m: dict) -> float:
        """Share of the points of m's ramp lines that lie on the route (within 15 m, same way)."""
        total = on = 0
        for cw, line in m["lines"]:
            if cw not in ("entrySlipRoad", "exitSlipRoad") or len(line) < 2:
                continue
            xy = [_xy(p) for p in line]
            for a, b in zip(xy, xy[1:]):
                total += 1
                n = self.nearest(((a[0] + b[0]) / 2, (a[1] + b[1]) / 2))
                if n is not None and n[0] <= 15.0:
                    diff = abs((math.atan2(b[0] - a[0], b[1] - a[1]) - self.heading(n[1]) + math.pi) % (2 * math.pi) - math.pi)
                    on += diff <= math.radians(45)
        return on / total if total else 0.0

    def heading(self, i: int) -> float:
        a, b = self.pts[i], self.pts[i + 1]
        return math.atan2(b[0] - a[0], b[1] - a[1])

    def match(self, m: dict, names: list[set[str]] | None = None) -> tuple[float, str, int] | None:
        """Where measure m lies on the route → (metres along it, 'op' or 'bij', segment), else None.
        Main roads count in the driving direction only (lines are drawn in the direction of
        the closed carriageway); a ramp counts when it is next to the route, in any direction
        (junction loops turn), so 'toerit Born ri. Eindhoven' shows up on a trip south too and
        its own text says which way it goes; a point counts when its text names the road there
        (points have no direction, and side streets cross the route)."""
        hits: list[tuple[float, str, int]] = []
        for cw, line in m["lines"]:
            ramp = cw in ("entrySlipRoad", "exitSlipRoad")
            xy = [_xy(p) for p in line]
            for a, b in zip(xy, xy[1:]) if len(xy) > 1 else [(xy[0], xy[0])]:
                mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
                if not (self.box[0] <= mid[0] <= self.box[2] and self.box[1] <= mid[1] <= self.box[3]):
                    continue
                n = self.nearest(mid)
                if n is None or n[0] > (40.0 if ramp else 20.0):
                    continue
                if not ramp and a != b:
                    diff = abs((math.atan2(b[0] - a[0], b[1] - a[1]) - self.heading(n[1]) + math.pi) % (2 * math.pi) - math.pi)
                    if diff > math.radians(45) and not (m["both"] and diff > math.radians(135)):
                        continue  # a road crossing, or the other carriageway
                hits.append((n[2], "bij" if ramp else "op", n[1]))
        text = m["text"].lower()
        for _, p in m["points"]:
            xy = _xy(p)
            if self.box[0] <= xy[0] <= self.box[2] and self.box[1] <= xy[1] <= self.box[3]:
                n = self.nearest(xy)
                if n is not None and n[0] <= 25.0 and names and any(
                        re.search(rf"\b{re.escape(w.lower())}\b", text) for w in names[n[1]] if len(w) >= 2):
                    hits.append((n[2], "op", n[1]))
        return min(hits) if hits else None


def near(m: dict, lat: float, lon: float, radius_m: float) -> float | None:
    """Distance in metres from (lat, lon) to the closest point of measure m, if within radius."""
    c = _xy((lat, lon))
    pts = [p for _, line in m["lines"] for p in line] + [p for _, p in m["points"]]
    d = min((math.dist(c, _xy(p)) for p in pts), default=None)
    return d if d is not None and d <= radius_m else None
