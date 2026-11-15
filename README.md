# mcp-geo

Weather, places and routes MCP server for the [reSpeaker agent](https://github.com/mosa-riel/respeaker-agent).
Returns data only (compact Dutch text); the model decides whether to show anything on the
e-paper screen (mcp-screen draws maps and compasses itself). Speaks **streamable-HTTP**; runs
as its own container / Home Assistant add-on; the agent connects by `url`.

## Tools

| tool | what |
|---|---|
| `weer(plaats="thuis", dag="vandaag")` | forecast for a place on a day (`vandaag`, `morgen`, `overmorgen`, a weekday, `YYYY-MM-DD`, or `week`): per day temperature, rain, wind, weather, sunrise/sunset; for one day also every 3 hours |
| `plaats_zoeken(naam)` | where a place, address, street or point of interest is: the default pick plus alternatives, each with distance and direction from home, so the model can choose and pass `lat,lon` on |
| `route(naar, van="thuis", vervoer="fiets", dag="vandaag", tijd="")` | distance, travel time and general direction by `fiets`, `auto` or `lopen`; for cycling and walking the wind (with, against, side; component along the route) and rain on the way at departure; directions in degrees for a compass |

`thuis`, `hier` or no place is Home Assistant's `zone.home` (Supervisor proxy, `homeassistant_api: true`); `lat,lon` is used as is.

## Data sources (online, public, no keys or accounts)

- Places: [OpenStreetMap Nominatim](https://nominatim.openstreetmap.org) (addresses, towns and
  points of interest worldwide, ranked by importance; usage policy: at most 1 request/s with an
  identifying User-Agent; results cached in memory). The default pick is Nominatim's first
  result; every tool names the place it used, and `plaats_zoeken` lists alternatives.
  [Open-Meteo geocoding](https://open-meteo.com/en/docs/geocoding-api) as fallback.
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
