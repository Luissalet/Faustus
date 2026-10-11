"""Live public data (src/public_data) and its built-in MCP server, against canned source answers -- no network."""

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

pytest.importorskip("sgp4")

from src import public_data as pd
from src.public_data.orbits import parse_tle, passes

ISS_TLE = """ISS (ZARYA)
1 25544U 98067A   26284.50000000  .00012000  00000+0  21000-3 0  9990
2 25544  51.6400 120.0000 0004000  90.0000 270.0000 15.50000000400000
"""

QUAKES = {"features": [
    {"properties": {"mag": 6.2, "place": "far away", "time": 1791700000000, "tsunami": 1, "url": "u1"},
     "geometry": {"coordinates": [155.0, -6.0, 30.0]}},
    {"properties": {"mag": 3.1, "place": "near Granada", "time": 1791710000000, "tsunami": 0, "url": "u2"},
     "geometry": {"coordinates": [-3.6, 37.2, 10.0]}},
    {"properties": {"mag": 2.0, "place": "tiny", "time": 1791720000000}, "geometry": {"coordinates": [-3.7, 40.4, 5.0]}},
]}
AIRCRAFT = {"ac": [
    {"hex": "4951d4", "flight": "TAP837  ", "r": "CS-TNT", "t": "A320", "alt_baro": 33000, "gs": 494.5, "track": 253.4,
     "lat": 40.6, "lon": -3.5, "squawk": "4044", "emergency": "none"},
    {"hex": "3443c1", "flight": "IBE06AA ", "r": "EC-IXD", "t": "A321", "alt_baro": 1800, "gs": 150, "track": 180,
     "lat": 40.48, "lon": -3.56, "squawk": "7700", "emergency": "general"},
    {"hex": "abc123", "alt_baro": "ground", "lat": None, "lon": None},
]}
STORMS = {"activeStorms": [{"id": "al092026", "name": "Simon", "classification": "HU", "intensity": "120", "pressure": "950",
                            "latitudeNumeric": 25.1, "longitudeNumeric": -70.2, "movementDir": 300, "movementSpeed": 12,
                            "lastUpdate": "2026-10-11T21:00:00.000Z", "publicAdvisory": {"url": "https://nhc/adv"}}]}


@pytest.fixture
def sources(tmp_path, monkeypatch):
    monkeypatch.setenv("FAUSTUS_PUBLIC_DATA_DIR", str(tmp_path))
    calls, down = [], set()

    def answer(url):
        calls.append(url)
        if any(d in url for d in down):
            raise ConnectionError("source down")
        if "earthquake.usgs.gov" in url:
            return QUAKES
        if "nhc.noaa.gov" in url:
            return STORMS
        if "adsb.lol" in url:
            return AIRCRAFT
        if "adsbdb.com/v0/aircraft" in url:
            return {"response": {"aircraft": {"type": "A320 214SL", "manufacturer": "Airbus", "registration": "CS-TNT",
                                              "registered_owner": "TAP Air Portugal"}}}
        if "adsbdb.com/v0/callsign" in url:
            return {"response": {"flightroute": {"callsign": "TAP837", "airline": {"name": "TAP"},
                                                 "origin": {"iata_code": "FCO", "municipality": "Rome"},
                                                 "destination": {"iata_code": "LIS", "municipality": "Lisbon"}}}}
        if "opensky" in url:
            return {"callsign": "TAP837  ", "path": [[1791745146 + 10 * i, 41.8 - 0.01 * i, 12.2 - 0.01 * i, 300 * i, 250, False]
                                                    for i in range(300)]}
        if "open-meteo" in url:
            return {"current": {"wind_speed_10m": 12.0, "wind_direction_10m": 270, "wind_gusts_10m": 25.0},
                    "hourly": {"time": ["2026-10-12T00:00", "2026-10-12T01:00"], "wind_speed_10m": [10, 14],
                               "wind_direction_10m": [260, 280], "wind_gusts_10m": [20, 31]}}
        if "celestrak" in url:
            return ISS_TLE
        raise AssertionError(url)

    pd.set_transport(answer)
    yield {"calls": calls, "down": down}
    pd.set_transport(None)


def test_earthquakes_worldwide_and_near(sources):
    world = pd.earthquakes(2.5, "day")
    assert [r["magnitude"] for r in world["data"]] == [3.1, 6.2] and world["freshness"] == "fresh"
    assert "M6.2 far away" in world["summary"] and world["data"][1]["tsunami"] is True
    assert "2.5_day.geojson" in sources["calls"][-1]
    near = pd.earthquakes(1.0, "week", 40.4, -3.7, 400)
    assert [r["place"] for r in near["data"]] == ["tiny", "near Granada"] and near["data"][0]["distance_km"] < 1
    assert "1.0_week" in sources["calls"][-1]
    with pytest.raises(pd.PublicDataError, match="period"):
        pd.earthquakes(period="year")


def test_cached_then_stale_when_the_source_dies(sources):
    pd.earthquakes(4.5, "day")
    n = len(sources["calls"])
    again = pd.earthquakes(4.5, "day")
    assert again["freshness"] == "cached" and len(sources["calls"]) == n
    feed = pd.feed("usgs-earthquakes", 120)
    feed.ttl_s = 0                                           # pretend the TTL passed
    sources["down"].add("earthquake.usgs.gov")
    stale = pd.earthquakes(4.5, "day")
    assert stale["freshness"] == "stale" and "source down" in stale["error"] and stale["data"]
    sources["down"].add("nhc.noaa.gov")
    with pytest.raises(pd.PublicDataError, match="nothing recent is cached"):
        pd.cyclones()


def test_cyclones(sources):
    out = pd.cyclones()
    assert out["data"][0]["wind_kt"] == 120 and out["data"][0]["kind"] == "hurricane"
    assert "hurricane Simon (120 kt)" in out["summary"] and "Atlantic" in out["coverage"]


def test_aircraft_nearby_lookup_and_track(sources):
    near = pd.aircraft_in_area(40.47, -3.56, 30, limit=1)
    assert near["total"] == 2 and near["truncated"] and near["data"][0]["callsign"] == "IBE06AA"
    assert "emergency" in near["summary"] and near["data"][0]["emergency"] == "general"
    assert "/point/40.4700/-3.5600/30" in sources["calls"][-1]
    info = pd.aircraft_info("4951D4", "tap837")
    assert info["data"]["aircraft"]["registration"] == "CS-TNT" and info["data"]["route"]["destination"]["iata_code"] == "LIS"
    assert "Rome (FCO) to Lisbon (LIS)" in info["summary"]
    track = pd.aircraft_track("4951d4", max_points=50)
    assert track["total"] == 300 and len(track["data"]) == 50 and track["truncated"]
    assert track["data"][0]["time"] < track["data"][-1]["time"] and "highest 89700 m" in track["summary"]
    for bad in ("xyz", "12345", "zzzzzz"):
        with pytest.raises(pd.PublicDataError, match="6 hexadecimal"):
            pd.aircraft_track(bad)
    with pytest.raises(pd.PublicDataError):
        pd.aircraft_info()
    with pytest.raises(pd.PublicDataError, match="lat must be"):
        pd.aircraft_in_area(95, 0)


def test_wind(sources):
    out = pd.wind(40.4, -3.7, 2)
    assert "12.0 km/h from the W" in out["summary"] and "31 km/h" in out["summary"] and len(out["data"]["hourly"]) == 2


def test_satellite_passes_shape_and_geometry(sources):
    out = pd.satellite_passes(40.4, -3.7, hours=24, start=datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc))
    assert out["satellite"] == "ISS (ZARYA)" and out["data"]
    for p in out["data"]:
        assert p["rise"] < p["peak"] < p["set"] and 10 <= p["max_elevation_deg"] <= 90 and p["duration_s"] > 0
        assert p["rise_compass"] in {"N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"}
    assert "CATNR=25544" in sources["calls"][-1]


def test_parse_tle_with_and_without_names():
    named = parse_tle(ISS_TLE)
    bare = parse_tle("\n".join(ISS_TLE.splitlines()[1:]))
    assert named[0][0] == "ISS (ZARYA)" and bare[0][0] == "NORAD 25544" and named[0][1:] == bare[0][1:]
    assert parse_tle("garbage") == []


def test_passes_never_report_below_the_mask():
    _, l1, l2 = parse_tle(ISS_TLE)[0]
    start = datetime(2026, 10, 11, 0, 0, tzinfo=timezone.utc)
    low = passes(l1, l2, 40.4, -3.7, 0, start, start.replace(hour=23), 0)
    high = passes(l1, l2, 40.4, -3.7, 0, start, start.replace(hour=23), 45)
    assert len(high) < len(low) and all(p["max_elevation_deg"] >= 45 for p in high)


# -- the MCP server -----------------------------------------------------------------------------------------------------
pytest.importorskip("mcp")
import mcp_servers.public_data_server as srv  # noqa: E402


def _call(name, arguments):
    return asyncio.run(srv.call_tool(name, arguments))[0].text


def test_server_surface_and_registration():
    tools = asyncio.run(srv.list_tools())
    assert [t.name for t in tools] == ["earthquakes", "tropical_cyclones", "aircraft_nearby", "aircraft_lookup",
                                       "aircraft_track", "wind_at", "satellite_passes"]
    for t in tools:
        assert len(t.description) > 60 and "Read-only" in t.description
        for req in t.inputSchema.get("required", []):
            assert req in t.inputSchema["properties"]
    from src import builtin_mcp
    script, label = builtin_mcp._BUILTIN_SERVERS["public_data"]
    assert script == "mcp_servers/public_data_server.py" and (Path(__file__).parent.parent / script).is_file()
    assert "public_data" not in builtin_mcp.NATIVE_TWIN_SERVERS


def test_server_calls_and_errors(sources):
    out = json.loads(_call("aircraft_nearby", {"lat": 40.47, "lon": -3.56, "radius_nm": 30}))
    assert out["total"] == 2 and out["freshness"] == "fresh"
    assert json.loads(_call("tropical_cyclones", {}))["data"][0]["name"] == "Simon"
    assert json.loads(_call("satellite_passes", {"lat": 40.4, "lon": -3.7}))["satellite"] == "ISS (ZARYA)"
    assert "Unknown tool" in _call("nope", {})
    assert "missing argument" in _call("wind_at", {"lat": 1})
    assert "6 hexadecimal" in _call("aircraft_track", {"hex": "x"})
    assert "Error in aircraft_track" in asyncio.run(srv.call_tool("aircraft_track", None))[0].text
