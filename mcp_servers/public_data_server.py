"""public_data_server.py

MCP server for live public data with no keys (src/public_data): earthquakes, tropical cyclones, aircraft near a point,
aircraft identity and route, the track of a flight, wind at a place and satellite passes over it.

Same discipline as the other built-in servers: stdout is the JSON-RPC stream (``src/stdio_guard.py`` is raised before
anything prints), the work module is imported on first use, and ``call_tool`` never raises. Read-only: every call is a GET to
a public source, cached on disk under ``DATA_DIR/public_data`` so a source that is down still answers with its last data,
marked ``stale``.
"""

import asyncio
import json
import sys
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from src.stdio_guard import guard as stdout_guard
except Exception:  # pragma: no cover - the server must start regardless
    from contextlib import nullcontext as stdout_guard

server = Server("public_data")

_LAT = {"type": "number", "minimum": -90, "maximum": 90, "description": "Latitude in degrees."}
_LON = {"type": "number", "minimum": -180, "maximum": 180, "description": "Longitude in degrees."}
_HEX = {"type": "string", "description": "ICAO 24-bit address, 6 hex characters (e.g. 4951d4), as aircraft_nearby returns it."}

TOOLS = [
    Tool(name="earthquakes",
         description=("Recent earthquakes from the USGS feeds: worldwide or near a point, by minimum magnitude and period "
                      "(hour, day, week, month). Rows: time, magnitude, place, position, depth, tsunami flag, distance. "
                      "Terremotos recientes. Read-only."),
         inputSchema={"type": "object", "properties": {
             "min_magnitude": {"type": "number", "minimum": 0, "maximum": 10, "description": "Default 2.5."},
             "period": {"type": "string", "enum": ["hour", "day", "week", "month"], "description": "Default day."},
             "lat": _LAT, "lon": _LON,
             "radius_km": {"type": "number", "minimum": 1, "maximum": 20000, "description": "With lat/lon; default 500."},
             "limit": {"type": "integer", "minimum": 1, "maximum": 200, "description": "Default 20."}}}),
    Tool(name="tropical_cyclones",
         description=("Active tropical cyclones from NOAA NHC (Atlantic, eastern and central Pacific): name, class, wind in "
                      "knots, pressure, position, movement and the public advisory. Huracanes y tormentas activas. Read-only."),
         inputSchema={"type": "object", "properties": {}}),
    Tool(name="aircraft_nearby",
         description=("Aircraft flying near a point right now (ADS-B, adsb.lol): callsign, type, registration, altitude, "
                      "speed, heading, distance, emergency squawks. Aviones cerca de un punto. Read-only."),
         inputSchema={"type": "object", "required": ["lat", "lon"], "properties": {
             "lat": _LAT, "lon": _LON,
             "radius_nm": {"type": "number", "minimum": 1, "maximum": 250, "description": "Nautical miles; default 25."},
             "limit": {"type": "integer", "minimum": 1, "maximum": 200, "description": "Default 25."}}}),
    Tool(name="aircraft_lookup",
         description=("Who an aircraft is and where a flight goes: the registry entry for an ICAO hex (type, registration, "
                      "operator) and/or the route of a callsign (airline, origin, destination). Avión y ruta. Read-only."),
         inputSchema={"type": "object", "properties": {"hex": _HEX, "callsign": {"type": "string", "description": "e.g. IBE3166."}}}),
    Tool(name="aircraft_track",
         description=("The path of an aircraft's current or last flight (OpenSky): points with time, position, altitude and "
                      "heading, thinned evenly to max_points; summary with distance and highest altitude. Trayectoria. Read-only."),
         inputSchema={"type": "object", "required": ["hex"], "properties": {
             "hex": _HEX, "max_points": {"type": "integer", "minimum": 2, "maximum": 1000, "description": "Default 120."}}}),
    Tool(name="wind_at",
         description=("Wind at a place now and hour by hour (Open-Meteo): speed, direction and gusts in km/h at 10 m. "
                      "Viento en un sitio. Read-only."),
         inputSchema={"type": "object", "required": ["lat", "lon"], "properties": {
             "lat": _LAT, "lon": _LON, "hours": {"type": "integer", "minimum": 1, "maximum": 72, "description": "Default 12."}}}),
    Tool(name="satellite_passes",
         description=("When a satellite passes over a place: CelesTrak orbital elements propagated with SGP4; rise, peak and "
                      "set times (UTC), compass directions, maximum elevation, closest distance. Default: the ISS (NORAD "
                      "25544). Pases de satélites. Read-only."),
         inputSchema={"type": "object", "required": ["lat", "lon"], "properties": {
             "lat": _LAT, "lon": _LON,
             "norad_id": {"type": "integer", "minimum": 1, "description": "NORAD catalogue number; default 25544 (ISS)."},
             "name": {"type": "string", "description": "Or a name to search on CelesTrak (e.g. HUBBLE, TIANGONG)."},
             "hours": {"type": "integer", "minimum": 1, "maximum": 72, "description": "Default 24."},
             "min_elevation": {"type": "number", "minimum": 0, "maximum": 89, "description": "Degrees; default 10."},
             "altitude_m": {"type": "number", "description": "Observer altitude; default 0."},
             "limit": {"type": "integer", "minimum": 1, "maximum": 50, "description": "Default 10."}}}),
]


def _text_result(text: str) -> list[TextContent]:
    return [TextContent(type="text", text=text)]


@server.list_tools()
async def list_tools() -> list[Tool]:
    return list(TOOLS)


def _dispatch(name: str, a: dict):
    from src import public_data as pd

    if name == "earthquakes":
        return pd.earthquakes(float(a.get("min_magnitude", 2.5)), str(a.get("period", "day")), a.get("lat"), a.get("lon"),
                              float(a.get("radius_km", 500)), int(a.get("limit", 20)))
    if name == "tropical_cyclones":
        return pd.cyclones()
    if name == "aircraft_nearby":
        return pd.aircraft_in_area(float(a["lat"]), float(a["lon"]), float(a.get("radius_nm", 25)), int(a.get("limit", 25)))
    if name == "aircraft_lookup":
        return pd.aircraft_info(str(a.get("hex") or ""), str(a.get("callsign") or ""))
    if name == "aircraft_track":
        return pd.aircraft_track(str(a.get("hex") or ""), int(a.get("max_points", 120)))
    if name == "wind_at":
        return pd.wind(float(a["lat"]), float(a["lon"]), int(a.get("hours", 12)))
    if name == "satellite_passes":
        return pd.satellite_passes(float(a["lat"]), float(a["lon"]), int(a.get("norad_id", 25544)), str(a.get("name") or ""),
                                   int(a.get("hours", 24)), float(a.get("min_elevation", 10)), float(a.get("altitude_m", 0)),
                                   int(a.get("limit", 10)))
    return None


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Dispatch, and never raise -- an exception here would kill the server."""
    if name not in {t.name for t in TOOLS}:
        return _text_result(f"Unknown tool: {name}")
    args = arguments if isinstance(arguments, dict) else {}
    try:
        result = await asyncio.to_thread(_dispatch, name, args)
    except KeyError as exc:
        return _text_result(f"Error in {name}: missing argument {exc}")
    except Exception as exc:  # noqa: BLE001 - bad arguments and dead sources come back as text
        return _text_result(f"Error in {name}: {exc}")
    return _text_result(json.dumps(result, indent=2, ensure_ascii=False, default=str))


async def run():
    async with stdio_server() as (read_stream, write_stream):
        with stdout_guard():
            await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(run())
