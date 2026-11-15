# mcp-geo

Weather, places, routes and road works MCP server for the [reSpeaker agent](https://github.com/mosa-riel/respeaker-agent).
Returns data only (compact Dutch text); the model decides whether to show anything on the
e-paper screen (mcp-screen draws maps and compasses itself). Speaks **streamable-HTTP**; runs
as its own container / Home Assistant add-on; the agent connects by `url`.

## Tools

| tool | what |
|---|---|
| `weer(plaats="thuis", dag="vandaag")` | forecast for a place on a day (`vandaag`, `morgen`, `overmorgen`, a weekday, `YYYY-MM-DD`, or `week`): per day temperature, rain, wind, weather, sunrise/sunset; for one day also every 3 hours |
| `plaats_zoeken(naam)` | where a place, address, street or point of interest is: the default pick plus alternatives, each with distance and direction from home, so the model can choose and pass `lat,lon` on |
| `route(naar, van="thuis", vervoer="fiets", dag="vandaag", tijd="")` | distance, travel time and general direction by `fiets`, `auto` or `lopen`; for cycling and walking the wind (with, against, side; component along the route) and rain on the way at departure; directions in degrees for a compass |
| `verkeer(naar="", van="thuis", weg="", plaats="", dag="vandaag", tijd="")` | road works, closures (also on- and off-ramps), accidents, broken-down vehicles and events in the Netherlands from NDW open data. With `naar`: along the car route from `van`; the answer starts with a conclusion (closures on the route with their end times, or none), then "Op je route" and, separately, "NIET op je route" (other direction, or a ramp you don't take). Two signals decide whether a measure applies to the trip: its geometry (main roads in the driving direction, ramps when the route drives along them) and a direction in its text ("ri. Eindhoven") checked against the way the route goes there. Either can be wrong (NDW's text for the Born on-ramp said towards Eindhoven; Rijkswaterstaat had closed the one towards Maastricht, where the geometry lay), so on a disagreement the geometry wins when another registration of the same works (nearby, same time; NDW often carries the same works twice, plus the live record) lies the same way, and otherwise the measure is listed on the route with a note that the data disagree. Point measures count only when their text names the road there. Without `naar`: around `plaats` (8 km, or 30 km on one `weg` such as `A2`). Without `tijd` the whole day (today: from now); `week` = 7 days. No traffic jams |

`thuis`, `hier` or no place is Home Assistant's `zone.home` (Supervisor proxy, `homeassistant_api: true`); `lat,lon` is used as is.

## Data sources (online, public, no keys or accounts)

- Places: [OpenStreetMap Nominatim](https://nominatim.openstreetmap.org) (addresses, towns and
  points of interest worldwide, ranked by importance; usage policy: at most 1 request/s with an
  identifying User-Agent; results cached in memory). The default pick is Nominatim's first
  result; every tool names the place it used, and `plaats_zoeken` lists alternatives.
  When Nominatim finds nothing (a misheard street name), [PDOK Locatieserver](https://api.pdok.nl/bzk/locatieserver/search/v3_1/ui/)
  is searched with fuzzy terms (up to two letters off; Dutch addresses, streets and towns),
  results in a town named in the query first; these are marked as similar names, so the
  answer can say the asked-for place was not found. Then
  [Open-Meteo geocoding](https://open-meteo.com/en/docs/geocoding-api) as the last fallback.
- Road works and closures: [NDW open data](https://opendata.ndw.nu) (Nationaal Dataportaal
  Wegverkeer; DATEX II 3; [terms](https://docs.ndw.nu/algemeen/data-gebruiksvoorwaarden)): the
  planning feed of road works and events by all road authorities (~15 MB, fetched at most every
  30 min and only when `verkeer` is used; parsing takes ~15-25 s, in the background after the
  first time) and `actueel_beeld` (what is in force now, incl. accidents; at most every 3 min).
  Measures carry WGS84 geometry but no road numbers, so they are matched to the route
  geometry; measures without text borrow it from the matching one nearby.
- Routes: [OpenStreetMap routing by FOSSGIS](https://routing.openstreetmap.de) (OSRM: car, bike,
  foot), a public service with a fair-use policy.
- Weather: [Open-Meteo](https://open-meteo.com) forecast.

## Run

```bash
cd mcp_geo && HA_URL=... HA_TOKEN=... MCP_PORT=8790 python server.py   # → http://127.0.0.1:8790/mcp
```

## Home Assistant add-on

Bridged, internal only (no ports on the LAN); the agent reaches it at
`http://<repo-hash>-mcp-geo:8790/mcp`. Dependencies pinned in the Dockerfile (an unpinned
`mcp` pulls 2.x, where FastMCP was renamed).

| option | env | default |
|---|---|---|
| `mcp_port` | `MCP_PORT` | `8790` |
| — | `MCP_HOST` | `0.0.0.0` |
