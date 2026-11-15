"""Geo — an MCP server for weather anywhere, places and routes (with wind) for the reSpeaker agent.

Tools: `weer` (forecast for a place and day), `plaats_zoeken` (where is X), `route` (distance,
travel time, direction and, for cycling or walking, the wind on the way; plus ready-made
blocks for the e-paper screen). Data from public services without keys (geo.py). Answers are
compact Dutch text so one tool call answers a question. Speaks streamable-HTTP.

Env: MCP_HOST (0.0.0.0), MCP_PORT (8790); outside HA, HA_URL + HA_TOKEN for "thuis".
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

import geo

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
    of 'lat,lon'; ook buitenland)
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
    """Zoek waar een plaats, adres of gemeente ligt (Nederland via PDOK, elders via Open-Meteo).
    Geeft naam, coördinaten en afstand en richting vanaf thuis."""
    async with _client() as cli:
        try:
            label, lat, lon = await geo.geocode(cli, naam)
            _, hlat, hlon = await geo.home(cli)
        except (geo.GeoError, httpx.HTTPError) as err:
            return f"Niet gevonden: {err}"
    import math

    dist = 6371 * 2 * math.asin(math.sqrt(math.sin(math.radians(lat - hlat) / 2) ** 2 + math.cos(math.radians(hlat))
                                          * math.cos(math.radians(lat)) * math.sin(math.radians(lon - hlon) / 2) ** 2))
    b = geo.bearing(hlat, hlon, lat, lon)
    return (f"{label}: lat {lat:.5f}, lon {lon:.5f}. Hemelsbreed {_nl(dist)} km {geo.compass_long(b)} van thuis "
            f"(richting {round(b)}°).")


@mcp.tool()
async def route(naar: str, van: str = "thuis", vervoer: str = "fiets", dag: str = "vandaag", tijd: str = "") -> str:
    """Route van A naar B met 'fiets', 'auto' of 'lopen'. Zonder startpunt, of 'thuis'/'hier',
    is het startpunt het huis (het adres van Home Assistant). Geeft afstand,
    reistijd en de algemene richting. Bij fiets en lopen ook de wind onderweg op het vertrek-
    moment (dag als bij weer, tijd 'HH:MM'; standaard nu, of 08:00 voor een andere dag): mee,
    tegen of zij. Geeft ook de richtingen in graden."""
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
                            f"({'tegen' if along > 0 else 'mee'} ~{_nl(abs(along))} km/u in je rijrichting).")
                wind_to = round((wdir + 180) % 360)
        except (geo.GeoError, httpx.HTTPError, ValueError) as err:
            return f"Route niet gelukt: {err}"
    mins = round(rt["duration_s"] / 60)
    dur = f"{mins // 60} u {mins % 60} min" if mins >= 60 else f"{mins} min"
    degrees = f" Richtingen in graden (waarheen): route {round(heading)}°" + (f", wind {wind_to}°." if wind_to is not None else ".")
    return (f"Route {vervoer} van {a_label} naar {b_label}: {_nl(rt['distance_m'] / 1000, 1)} km, ongeveer {dur}, "
            f"algemene richting {geo.compass_long(heading)}.{wind_txt}{degrees}")


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
