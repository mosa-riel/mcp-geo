"""Geo — an MCP server for weather anywhere, places and routes (with wind) for the reSpeaker agent.

Tools: `weer` (forecast for a place and day), `plaats_zoeken` (where is X), `route` (distance,
travel time, direction and, for cycling or walking, the wind on the way), `verkeer` (road
works, closures, accidents along a car route or around a place; NDW open data, ndw.py). Data
from public services without keys (geo.py). Answers are compact Dutch text so one tool call
answers a question. Speaks streamable-HTTP.

Env: MCP_HOST (0.0.0.0), MCP_PORT (8790); outside HA, HA_URL + HA_TOKEN for "thuis".
"""

from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

import geo
import ndw

TZ = ZoneInfo("Europe/Amsterdam")
DAYS = ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"]


def _load_ha_options() -> None:
    try:
        with open("/data/options.json") as f:
            opts = json.load(f)
    except (OSError, ValueError):
        return
    if opts.get("mcp_port"):
        os.environ["MCP_PORT"] = str(opts["mcp_port"])


_load_ha_options()

mcp = FastMCP(
    "geo",
    host=os.getenv("MCP_HOST", "0.0.0.0"),
    port=int(os.getenv("MCP_PORT", "8790")),
    # Server↔server MCP on a trusted internal add-on network — no browser DNS-rebinding guard.
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=12, headers=geo.UA, follow_redirects=True)


def _day(dag: str) -> date:
    today = datetime.now(TZ).date()
    d = (dag or "vandaag").strip().lower()
    if d in ("vandaag", "nu", ""):
        return today
    if d == "morgen":
        return today + timedelta(days=1)
    if d == "overmorgen":
        return today + timedelta(days=2)
    if d in DAYS:
        ahead = (DAYS.index(d) - today.weekday()) % 7
        return today + timedelta(days=ahead or 7)
    try:
        return date.fromisoformat(d)
    except ValueError:
        return today


def _nl(x: float, dec: int = 0) -> str:
    return f"{x:.{dec}f}".replace(".", ",")


def _dayname(d: date) -> str:
    today = datetime.now(TZ).date()
    rel = {0: "vandaag", 1: "morgen", 2: "overmorgen"}.get((d - today).days)
    return f"{rel + ' ' if rel else ''}{DAYS[d.weekday()]} {d.day}-{d.month}"


@mcp.tool()
async def weer(plaats: str = "thuis", dag: str = "vandaag") -> str:
    """Weersverwachting voor een plaats (leeg, 'thuis' of 'hier' = het huis; een plaats, adres
    of 'lat,lon'; ook buitenland; het antwoord noemt de gebruikte plaats: klopt die niet, gebruik
    plaats_zoeken en geef 'lat,lon' mee)
    op een dag: 'vandaag', 'morgen', 'overmorgen', een weekdag ('zaterdag'), een datum
    (2026-11-20) of 'week' (7 dagen). Geeft per dag temperatuur, neerslag, wind en het weer,
    en voor één dag ook per 3 uur."""
    async with _client() as cli:
        try:
            label, lat, lon = await geo.geocode(cli, plaats)
            week = (dag or "").strip().lower() == "week"
            target = datetime.now(TZ).date() if week else _day(dag)
            days = 7 if week else (target - datetime.now(TZ).date()).days + 1
            fc = await geo.forecast(cli, lat, lon, max(days, 1))
        except (geo.GeoError, httpx.HTTPError) as err:
            return f"Weer niet gelukt: {err}"
    d = fc["daily"]
    lines = [f"Weer {'thuis' if label == 'thuis' else 'in ' + label} (Open-Meteo):"]
    for i, ds in enumerate(d["time"]):
        dd = date.fromisoformat(ds)
        if not week and dd != target:
            continue
        lines.append(
            f"- {_dayname(dd)}: {geo.WMO.get(d['weather_code'][i], 'onbekend')}, {_nl(d['temperature_2m_min'][i])} tot "
            f"{_nl(d['temperature_2m_max'][i])} °C, neerslag {_nl(d['precipitation_sum'][i], 1)} mm "
            f"(kans {d['precipitation_probability_max'][i] or 0}%), wind {geo.compass(d['wind_direction_10m_dominant'][i])} "
            f"tot {_nl(d['wind_speed_10m_max'][i])} km/u, zon op {d['sunrise'][i][11:16]} onder {d['sunset'][i][11:16]}")
    if not week:
        h = fc["hourly"]
        hours = [i for i, t in enumerate(h["time"]) if t.startswith(target.isoformat()) and int(t[11:13]) % 3 == 0
                 and 6 <= int(t[11:13]) <= 21]
        lines.append("Per 3 uur (tijd, temp, wind, windstoten, neerslag, kans):")
        lines += [f"  {h['time'][i][11:16]} {_nl(h['temperature_2m'][i])}° {geo.compass(h['wind_direction_10m'][i])} "
                  f"{_nl(h['wind_speed_10m'][i])} km/u (stoten {_nl(h['wind_gusts_10m'][i])}), "
                  f"{_nl(h['precipitation'][i], 1)} mm, {h['precipitation_probability'][i] or 0}% "
                  f"{geo.WMO.get(h['weather_code'][i], '')}" for i in hours]
    return "\n".join(lines)


@mcp.tool()
async def plaats_zoeken(naam: str) -> str:
    """Zoek waar een plaats, adres of straat ligt. Geeft de standaardkeuze (die weer en route ook
    gebruiken) en de alternatieven, met afstand en richting vanaf thuis. Vindt het de naam niet
    precies (verkeerd verstaan?), dan geeft het gelijkende Nederlandse namen. Kies zelf het juiste
    en geef dan 'lat,lon' door aan weer of route. Zoek dezelfde naam niet opnieuw in andere
    schrijfwijzen: één keer is genoeg."""
    import math

    async with _client() as cli:
        try:
            _, hlat, hlon = await geo.home(cli)
            q = (naam or "").strip()
            cands = await geo.candidates(cli, q)
        except (geo.GeoError, httpx.HTTPError) as err:
            return f"Niet gevonden: {err}"
    if not cands:
        return f"Geen plaats gevonden voor '{naam}'."
    pick = geo.best(q, cands)

    def line(c: dict) -> str:
        d = 6371 * 2 * math.asin(math.sqrt(math.sin(math.radians(c["lat"] - hlat) / 2) ** 2 + math.cos(math.radians(hlat))
                                           * math.cos(math.radians(c["lat"])) * math.sin(math.radians(c["lon"] - hlon) / 2) ** 2))
        b = geo.bearing(hlat, hlon, c["lat"], c["lon"])
        extra = f", {c['population']} inwoners" if c.get("population") else ""
        return (f"{c['label']} ({c['kind']}{extra}): {c['lat']:.5f},{c['lon']:.5f}, {_nl(d)} km {geo.compass_long(b)} "
                f"van thuis")

    others = [c for c in cands if c is not pick]
    head = (f"'{naam}' zelf is niet gevonden; wel deze gelijkende namen. Noem dat, en vraag welke bedoeld wordt als "
            "het niet duidelijk is.\n" if pick.get("approx") else "")
    return head + "Standaard: " + line(pick) + ("\nAlternatieven:\n" + "\n".join("- " + line(c) for c in others)
                                               if others else "")


@mcp.tool()
async def route(naar: str, van: str = "thuis", vervoer: str = "fiets", dag: str = "vandaag", tijd: str = "") -> str:
    """Route van A naar B met 'fiets', 'auto' of 'lopen'. Zonder startpunt, of 'thuis'/'hier',
    is het startpunt het huis (het adres van Home Assistant); van en naar mogen ook 'lat,lon' zijn
    (controleer de genoemde plaatsen; klopt er een niet, gebruik plaats_zoeken). Geeft afstand,
    reistijd en de algemene richting. Bij fiets en lopen ook de wind onderweg op het vertrek-
    moment (dag als bij weer, tijd 'HH:MM'; standaard nu, of 08:00 voor een andere dag): mee,
    tegen of zij, en de regen onderweg (mm en kans). Geeft ook de richtingen in graden."""
    vervoer = vervoer if vervoer in geo.PROFILES else "fiets"
    async with _client() as cli:
        try:
            a_label, alat, alon = await geo.geocode(cli, van)
            b_label, blat, blon = await geo.geocode(cli, naar)
            rt = await geo.route(cli, (alat, alon), (blat, blon), vervoer)
            day = _day(dag)
            now = datetime.now(TZ)
            if tijd:
                hh, _, mm = tijd.partition(":")
                start = datetime(day.year, day.month, day.day, int(hh), int(mm or 0), tzinfo=TZ)
            else:
                start = now if day == now.date() else datetime(day.year, day.month, day.day, 8, tzinfo=TZ)
            wind_txt, wind_to = "", None
            heading = geo.bearing(alat, alon, blat, blon)
            if vervoer in ("fiets", "lopen", "wandelen"):
                mid = rt["path"][len(rt["path"]) // 2]
                fc = await geo.forecast(cli, mid[0], mid[1], (day - now.date()).days + 1)
                key = start.strftime("%Y-%m-%dT%H:00")
                h = fc["hourly"]
                i = h["time"].index(key) if key in h["time"] else 0
                wdir, wspd, gust = h["wind_direction_10m"][i], h["wind_speed_10m"][i], h["wind_gusts_10m"][i]
                rain = sum(h["precipitation"][i:i + max(1, round(rt["duration_s"] / 3600 + 0.49))])
                chance = max(h["precipitation_probability"][i:i + 2] or [0]) or 0
                rel = (wdir - heading + 360) % 360  # 0 = wind from straight ahead
                if rel < 30 or rel > 330:
                    kind = "tegenwind"
                elif 150 < rel < 210:
                    kind = "wind mee"
                elif rel < 75 or rel > 285:
                    kind = "schuin tegen"
                elif 105 < rel < 255:
                    kind = "schuin mee"
                else:
                    kind = "zijwind"
                import math

                along = wspd * math.cos(math.radians(rel))
                wind_txt = (f" Wind om {start:%H:%M} onderweg: uit het {geo.compass_long(wdir)} ({geo.compass(wdir)}), "
                            f"{_nl(wspd)} km/u, stoten {_nl(gust)}: {kind} "
                            f"({'tegen' if along > 0 else 'mee'} ~{_nl(abs(along))} km/u in je rijrichting). "
                            f"Regen onderweg: {_nl(rain, 1)} mm, kans {chance}% "
                            f"({geo.WMO.get(h['weather_code'][i], 'onbekend')}).")
                wind_to = round((wdir + 180) % 360)
        except (geo.GeoError, httpx.HTTPError, ValueError) as err:
            return f"Route niet gelukt: {err}"
    mins = round(rt["duration_s"] / 60)
    dur = f"{mins // 60} u {mins % 60} min" if mins >= 60 else f"{mins} min"
    degrees = f" Richtingen in graden (waarheen): route {round(heading)}°" + (f", wind {wind_to}°." if wind_to is not None else ".")
    return (f"Route {vervoer} van {a_label} naar {b_label}: {_nl(rt['distance_m'] / 1000, 1)} km, ongeveer {dur}, "
            f"algemene richting {geo.compass_long(heading)}.{wind_txt}{degrees}")


_DAY_SHORT = ["ma", "di", "wo", "do", "vr", "za", "zo"]


def _when(d: datetime | None) -> str:
    if d is None:
        return "onbekend"
    d = d.astimezone(TZ)
    year = f"-{d.year}" if d.year != datetime.now(TZ).year else ""
    return f"{_DAY_SHORT[d.weekday()]} {d.day}-{d.month}{year} {d:%H:%M}"


def _window(dag: str, tijd: str, duration_s: float = 0) -> tuple[datetime, datetime, str]:
    now = datetime.now(TZ)
    if (dag or "").strip().lower() == "week":
        return now, now + timedelta(days=7), "de komende 7 dagen"
    day = _day(dag)
    if tijd:
        hh, _, mm = tijd.partition(":")
        start = datetime(day.year, day.month, day.day, int(hh), int(mm or 0), tzinfo=TZ)
        end = start + (timedelta(seconds=duration_s + 1800) if duration_s else timedelta(hours=2))
        return start, end, f"{_dayname(day)} {start:%H:%M}-{end:%H:%M}"
    if day == now.date():
        return now, datetime(day.year, day.month, day.day, 23, 59, tzinfo=TZ), "vandaag vanaf nu"
    start = datetime(day.year, day.month, day.day, tzinfo=TZ)
    return start, start + timedelta(days=1), _dayname(day)


_TOWARDS = re.compile(r"\b(?:ri\.|richting)\s+([A-Z][\w'-]+(?:[ -][A-Z][\w'-]+)?)")


async def _sides(cli: httpx.AsyncClient, route: ndw.Route, found: list[tuple[float, dict, list]]) -> list[tuple[str, str]]:
    """Does each measure found along the route apply to this trip? → [('op' | 'andere richting' |
    'naast', note)]. Two signals: the geometry (main roads were matched in the driving direction;
    a ramp counts when the route drives along it) and a direction in the text ("De toerit Born ri.
    Eindhoven is dicht"), checked against the way the route goes there. Either can be wrong: on
    2026-11-15 the text said 'ri. Eindhoven' while Rijkswaterstaat had closed the ramp towards
    Maastricht, where the geometry of all three registrations lay. So on a disagreement the
    geometry wins when another registration of the same works (nearby, same time) confirms it;
    otherwise the measure counts as on the route, with a note that the data disagree."""
    places: dict = {}
    geo_v, text_v, names = [], [], []
    for k, m, _ in found:
        geo_v.append("op" if m.get("at") != "bij" or route.follows(m) >= 0.5 else "naast")
        verdict, d = None, _TOWARDS.search(m["text"])
        if d:
            name = d.group(1)
            if name not in places:
                try:
                    places[name] = (await geo.geocode(cli, name))[1:]
                except (geo.GeoError, httpx.HTTPError, ValueError):
                    places[name] = None
            here, ahead = route.at(k), route.at(k + 3000)
            if places[name] and here != ahead:
                diff = abs((geo.bearing(*here, *places[name]) - geo.bearing(*here, *ahead) + 180) % 360 - 180)
                verdict = "andere richting" if diff > 120 else "op" if diff < 60 else None
        text_v.append(verdict)
        names.append(d.group(1) if d else "")
    out = []
    for i, (k, m, p) in enumerate(found):
        g, t = geo_v[i], text_v[i]
        if t is None or (t == "op") == (g == "op"):
            out.append((g if t is None else ("op" if g == "op" else t), ""))
            continue
        twins = [j for j, (k2, m2, p2) in enumerate(found) if j != i and abs(k2 - k) < 500 and text_v[j] is None
                 and m2.get("at") == m.get("at") and _overlap(p, p2)]
        if twins and all(geo_v[j] == g for j in twins):
            where = "jouw rijrichting" if g == "op" else "een andere op-/afrit"
            out.append((g, f"de tekst noemt 'richting {names[i]}', maar {len(twins) + 1} registraties liggen op "
                           f"{where}; uitgegaan van de ligging"))
        else:
            out.append(("op", f"de tekst noemt 'richting {names[i]}', de ligging iets anders; de gegevens spreken "
                              "elkaar tegen, ga uit van dicht en controleer het"))
    return out


def _overlap(p1: list, p2: list) -> bool:
    lo, hi = 0.0, 1e11  # open start / end
    return any((a1.timestamp() if a1 else lo) <= (b2.timestamp() if b2 else hi)
               and (a2.timestamp() if a2 else lo) <= (b1.timestamp() if b1 else hi) for a1, b1 in p1 for a2, b2 in p2)


def _summarize(found: list[tuple[float, dict, list]], where: str, limit: int = 8) -> list[str]:
    """(sort key, measure, active periods) → lines, the same text and kind merged."""
    groups: dict[tuple[str, str], dict] = {}
    for key, m, periods in found:
        head = min(m["kinds"], key=ndw.SEVERITY.index)
        g = groups.setdefault((head, m["text"] or m["id"]),
                              {"key": key, "m": m, "head": head, "periods": set(), "live": False})
        g["key"] = min(g["key"], key)
        g["periods"].update(periods)
        g["live"] |= m["live"]
    order = sorted(groups.values(), key=lambda g: (ndw.SEVERITY.index(g["head"]), g["key"]))
    lines = []
    for g in order[:limit]:
        m = g["m"]
        ps = sorted(g["periods"], key=lambda p: p[0].timestamp() if p[0] else 0)
        when = "; ".join(f"{_when(a)} tot {_when(b)}" for a, b in ps[:3]) + (f" (en nog {len(ps) - 3} keer)" if len(ps) > 3 else "")
        extra = sorted(m["kinds"] - {g["head"]}, key=ndw.SEVERITY.index)
        place = where.format(km=g["key"] / 1000) + (f" ({m['on']})" if m.get("on") else "")
        if m.get("note"):
            lines.append(f"- {m['note'].upper()[:1]}{m['note'][1:]}:")
        lines.append(f"- {g['head']}{' (nu)' if g['live'] else ''}{', ' + ', '.join(extra) if extra else ''}"
                     f" [{place}{', ' + m['road'] if m['road'] else ''}; {m['source'] or 'NDW'}]: "
                     f"{m['text'] or 'geen omschrijving'}. Wanneer: {when}.")
    if len(order) > limit:
        lines.append(f"- en nog {len(order) - limit} kleinere maatregelen")
    return lines


_MINOR = {"snelheidsbeperking", "omleiding"}
_ROAD_REF = re.compile(r"^[AEN]\d+$")


def _merge_textless(found: list[tuple[float, dict, list]]) -> list[tuple[float, dict, list]]:
    """Some measures come without text: one in force now (actueel_beeld) for a planned one, or a
    second registration of the same works. Take the text of the nearest measure with text (within
    500 m, overlapping in time); a live one replaces its planned twin, a planned one is dropped."""
    lo, hi = 0.0, 1e11  # open start / end

    def overlap(p1: list, p2: list) -> bool:
        return any((a1.timestamp() if a1 else lo) <= (b2.timestamp() if b2 else hi)
                   and (a2.timestamp() if a2 else lo) <= (b1.timestamp() if b1 else hi) for a1, b1 in p1 for a2, b2 in p2)
    texted = [(k, m, p) for k, m, p in found if m["text"]]
    live_bare = [(k, p) for k, m, p in found if m["live"] and not m["text"]]
    out, drop = [], set()
    for k, m, p in found:
        if not m["text"] and not m["live"] and any(abs(lk - k) < 500 and overlap(p, lp) for lk, lp in live_bare):
            continue  # the same works, in force now: keep that one
        if not m["text"]:
            near = [(abs(tk - k), tm) for tk, tm, tp in texted if abs(tk - k) < 500 and overlap(p, tp)]
            if near:
                twin = min(near, key=lambda x: x[0])[1]
                if not m["live"]:
                    continue  # the measure with text says it
                if not twin["live"]:
                    drop.add(twin["id"])
                m = dict(m, text=twin["text"], source=twin["source"] or m["source"], road=twin["road"],
                         note=twin.get("note") or m.get("note", ""))
        out.append((k, m, p))
    return [x for x in out if x[1]["id"] not in drop or x[1]["live"]]


@mcp.tool()
async def verkeer(naar: str = "", van: str = "thuis", weg: str = "", plaats: str = "", dag: str = "vandaag",
                  tijd: str = "") -> str:
    """Wegwerkzaamheden, afsluitingen (ook van op- en afritten), ongevallen, pech en evenementen op
    de weg in Nederland, uit de officiële open data van NDW (Rijkswaterstaat, provincies,
    gemeenten). Gebruik dit voor elke vraag of een weg, oprit of route open is of hinder heeft,
    in plaats van op internet te zoeken. Met 'naar': langs de autoroute van 'van' (standaard
    thuis) naar 'naar', alleen in je rijrichting (ook de oprit waar je de snelweg op gaat).
    Zonder 'naar': rond 'plaats' (standaard thuis), eventueel alleen op 'weg' (bv. 'A2').
    dag en tijd als bij weer en route; zonder tijd de hele dag (vandaag: vanaf nu), 'week' = 7
    dagen. Files en drukte van dit moment staan er niet in."""
    start, end, label = _window(dag, tijd)
    async with httpx.AsyncClient(timeout=20, headers=geo.UA, follow_redirects=True) as cli:
        try:
            planned = await ndw.feeds.get(ndw.PLANNING)
            current = await ndw.feeds.get(ndw.CURRENT) if start <= datetime.now(TZ) + timedelta(hours=1) else []
        except (httpx.HTTPError, TimeoutError, OSError) as err:
            return f"Verkeersinformatie niet beschikbaar (NDW): {err}"
        measures = {m["id"]: m for m in planned}
        measures.update({m["id"]: m for m in current})  # what is in force now wins
        try:
            if naar:
                a_label, alat, alon = await geo.geocode(cli, van)
                b_label, blat, blon = await geo.geocode(cli, naar)
                rt = await geo.route(cli, (alat, alon), (blat, blon), "auto", full=True)
                if tijd:
                    start, end, label = _window(dag, tijd, rt["duration_s"])
            else:
                c_label, clat, clon = await geo.geocode(cli, plaats or "thuis")
        except (geo.GeoError, httpx.HTTPError, ValueError) as err:
            return f"Verkeer niet gelukt: {err}"
    found = []
    if naar:
        route = ndw.Route(rt["coords"])
        lats, lons = zip(*rt["coords"])
        box = (min(lats) - 0.005, max(lats) + 0.005, min(lons) - 0.008, max(lons) + 0.008)
        for m in measures.values():
            if m["kinds"] <= _MINOR | {"fiets- of voetpad dicht", "werkzaamheden"} and "fiets- of voetpad dicht" in m["kinds"]:
                continue  # a car route
            if m["kinds"] <= _MINOR:
                continue
            pts = [p for _, line in m["lines"] for p in line[:: max(1, len(line) // 4)]] + [p for _, p in m["points"]]
            if not any(box[0] <= p[0] <= box[1] and box[2] <= p[1] <= box[3] for p in pts):
                continue
            periods = ndw.active(m, start, end)
            if periods and (at := route.match(m, rt.get("names"))) is not None:
                names = sorted((rt.get("names") or [set()] * (at[2] + 1))[at[2]], key=lambda x: (not _ROAD_REF.match(x), x))
                found.append((at[0], dict(m, at=at[1], on=", ".join(names[:2])), periods))
        mins = round(rt["duration_s"] / 60)
        head = (f"Verkeer langs de autoroute van {a_label} naar {b_label} ({_nl(rt['distance_m'] / 1000, 1)} km, "
                f"~{mins} min{', via ' + rt['via'] if rt.get('via') else ''}), {label}")
        where = "km {km:.0f} van de route"
    else:
        road = weg.strip().upper()
        radius = 30000 if road else 8000
        for m in measures.values():
            if m["kinds"] <= _MINOR or (road and m["road"] != road and not re.search(rf"\b{re.escape(road)}\b", m["text"])):
                continue
            periods = ndw.active(m, start, end)
            if periods and (d := ndw.near(m, clat, clon, radius)) is not None:
                found.append((d, m, periods))
        head = f"Verkeer {'op de ' + road + ' ' if road else ''}binnen {radius // 1000} km van {c_label}, {label}"
        where = "{km:.0f} km van " + c_label
    age = ndw.feeds.age_min(ndw.PLANNING)
    head += f" (NDW{f', {age} min oud' if age else ''}):"
    if naar:  # decide before textless twins are merged away: they are a second registration
        async with httpx.AsyncClient(timeout=20, headers=geo.UA, follow_redirects=True) as cli:
            sides = await _sides(cli, route, found)
        found = [(k, dict(m, at=side, note=note), p) for (k, m, p), (side, note) in zip(found, sides)]
    found = _merge_textless(found)
    if not naar:
        if not found:
            return head + "\nGeen afsluitingen, werkzaamheden of ongevallen gevonden. Files staan niet in deze bron."
        return "\n".join([head] + _summarize(found, where))
    on = [x for x in found if x[1].get("at") == "op"]
    off = [x for x in found if x[1].get("at") != "op"]
    closed = [x for x in on if any(k.endswith(" dicht") or k == "ongeval" for k in x[1]["kinds"])]
    lines = [head]
    if closed:
        brief = []
        for k, m, p in sorted(closed, key=lambda x: x[0]):
            head = min(m["kinds"], key=ndw.SEVERITY.index)
            ends = [b for _, b in p]
            until = "einde onbekend" if None in ends else f"tot {_when(max(ends))}"
            brief.append(f"{head} bij km {k / 1000:.0f}{' (' + m['on'] + ')' if m.get('on') else ''} {until}"
                         + (" (gegevens tegenstrijdig over de richting)" if "tegen" in m.get("note", "") else ""))
        lines.append(f"Conclusie: op je route in deze periode {'; '.join(dict.fromkeys(brief))}.")
    else:
        lines.append("Conclusie: geen afsluitingen of ongevallen op je route in deze periode"
                     + (" (alleen kleinere hinder)." if on else ".") + " Files staan niet in deze bron.")
    if on:
        lines += ["Op je route:"] + _summarize(on, where)
    if off:
        lines += ["Vlakbij, maar NIET op je route (andere rijrichting of een op-/afrit die je niet neemt); alleen ter info:"]
        lines += _summarize(off, where, limit=3)
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
