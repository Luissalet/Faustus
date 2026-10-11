"""Live public data without keys: earthquakes, tropical cyclones, aircraft (position, identity, route, track), wind and
satellite passes over a place.

Every answer has the same shape -- ``summary`` (one or two sentences a model can quote), ``data`` (the rows), ``truncated``,
and the freshness of the source (``fresh`` | ``cached`` | ``stale``, ``age_s``, ``source``) -- because every source goes
through ``hoard_link.livefeed.LiveFeed``: one request per key at a time, retries, and the last good answer served as
``stale`` when the source is down.

Sources: USGS earthquake feeds, NOAA NHC current storms, adsb.lol (live positions), adsbdb (aircraft and route by
callsign), OpenSky (track of the last flight), Open-Meteo (wind), CelesTrak (orbital elements, propagated here with SGP4).
"""

from __future__ import annotations

import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

from src.hoard_link.livefeed import LiveFeed, LiveFeedError

USER_AGENT = "Faustus-public-data/1.0 (+local assistant)"
TIMEOUT_S = 15.0
_FEEDS: dict[str, LiveFeed] = {}


class PublicDataError(ValueError):
    """A bad argument or a source that failed with nothing cached: the message is for the user."""


def _data_dir() -> Path:
    override = os.environ.get("FAUSTUS_PUBLIC_DATA_DIR")
    if override:
        return Path(override)
    try:
        from src.constants import DATA_DIR
        return Path(DATA_DIR) / "public_data"
    except Exception:  # noqa: BLE001 - standalone use
        return Path.home() / ".faustus-public-data"


def feed(source: str, ttl_s: float) -> LiveFeed:
    got = _FEEDS.get(source)
    if got is None:
        got = _FEEDS[source] = LiveFeed(source, ttl_s=ttl_s, disk_dir=_data_dir(), retries=1, backoff_s=0.6)
    return got


_http_get: Optional[Callable[[str], Any]] = None


def http_json(url: str) -> Any:
    """GET a JSON document (replaceable in tests through ``set_transport``)."""
    if _http_get is not None:
        return _http_get(url)
    with httpx.Client(timeout=TIMEOUT_S, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}, follow_redirects=True) as c:
        r = c.get(url)
        r.raise_for_status()
        return r.json()


def http_text(url: str) -> str:
    if _http_get is not None:
        return _http_get(url)
    with httpx.Client(timeout=TIMEOUT_S, headers={"User-Agent": USER_AGENT}, follow_redirects=True) as c:
        r = c.get(url)
        r.raise_for_status()
        return r.text


def set_transport(fn: Optional[Callable[[str], Any]]) -> None:
    """Tests: answer every URL with ``fn(url)`` instead of the network; ``None`` restores it. Clears the in-memory feeds."""
    global _http_get
    _http_get = fn
    _FEEDS.clear()


def _fetch(source: str, ttl_s: float, key: str, fn: Callable[[], Any]):
    try:
        return feed(source, ttl_s).get(key, fn)
    except LiveFeedError as exc:
        raise PublicDataError(f"{source} is not answering and nothing recent is cached: {exc}") from exc


def _answer(summary: str, rows: Any, got, *, limit: Optional[int] = None, total: Optional[int] = None, **extra) -> dict[str, Any]:
    truncated = bool(limit is not None and total is not None and total > limit)
    out = {"summary": summary, "data": rows, "truncated": truncated}
    if total is not None:
        out["total"] = total
    out.update(extra)
    if got is not None:
        out.update(got.meta())
    return out


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _check_point(lat: float, lon: float) -> None:
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise PublicDataError("lat must be within -90..90 and lon within -180..180")


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0088 * math.asin(math.sqrt(a))


def _hex6(value: str) -> str:
    value = (value or "").strip().lower()
    if len(value) != 6 or not all(c in "0123456789abcdef" for c in value):
        raise PublicDataError("the ICAO hex code has 6 hexadecimal characters (e.g. 4951d4)")
    return value


# ---------------------------------------------------------------------------------------------------------- earthquakes
def earthquakes(min_magnitude: float = 2.5, period: str = "day", lat: Optional[float] = None, lon: Optional[float] = None,
                radius_km: float = 500.0, limit: int = 20) -> dict[str, Any]:
    if period not in ("hour", "day", "week", "month"):
        raise PublicDataError("period must be hour, day, week or month")
    bucket = "4.5" if min_magnitude >= 4.5 else "2.5" if min_magnitude >= 2.5 else "1.0" if min_magnitude >= 1.0 else "all"
    name = f"{bucket}_{period}"
    url = f"https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/{name}.geojson"
    got = _fetch("usgs-earthquakes", 120, name, lambda: http_json(url))
    near = lat is not None and lon is not None
    if near:
        _check_point(lat, lon)
    rows = []
    for f in got.data.get("features", []):
        p = f.get("properties") or {}
        g = (f.get("geometry") or {}).get("coordinates") or [None, None, None]
        mag = p.get("mag")
        if mag is None or mag < min_magnitude:
            continue
        row = {"time": _iso(p["time"] / 1000) if p.get("time") else None, "magnitude": mag, "place": p.get("place"),
               "lat": g[1], "lon": g[0], "depth_km": g[2], "tsunami": bool(p.get("tsunami")), "alert": p.get("alert"),
               "url": p.get("url")}
        if near:
            if g[1] is None:
                continue
            row["distance_km"] = round(haversine_km(lat, lon, g[1], g[0]), 1)
            if row["distance_km"] > radius_km:
                continue
        rows.append(row)
    if near:
        rows.sort(key=lambda r: r["distance_km"])
    else:
        rows.sort(key=lambda r: r["time"] or "", reverse=True)
    total = len(rows)
    where = f" within {radius_km:g} km of {lat:.2f}, {lon:.2f}" if near else " worldwide"
    if rows:
        big = max(rows, key=lambda r: r["magnitude"])
        summary = (f"{total} earthquakes of magnitude {min_magnitude:g}+ in the last {period}{where}; the strongest was "
                   f"M{big['magnitude']} {big['place']} at {big['time']}.")
    else:
        summary = f"No earthquakes of magnitude {min_magnitude:g}+ in the last {period}{where}."
    return _answer(summary, rows[:limit], got, limit=limit, total=total)


# ---------------------------------------------------------------------------------------------------------- cyclones
_KINDS = {"TD": "tropical depression", "STD": "subtropical depression", "TS": "tropical storm", "STS": "subtropical storm",
          "HU": "hurricane", "PTC": "potential tropical cyclone", "PC": "post-tropical cyclone"}


def _num(value):
    try:
        text = str(value)
        return float(text) if "." in text else int(text)
    except (TypeError, ValueError):
        return value


def cyclones() -> dict[str, Any]:
    got = _fetch("nhc-storms", 600, "current", lambda: http_json("https://www.nhc.noaa.gov/CurrentStorms.json"))
    rows = []
    for s in got.data.get("activeStorms", []) or []:
        rows.append({"id": s.get("id"), "name": s.get("name"), "classification": s.get("classification"),
                     "kind": _KINDS.get(s.get("classification"), s.get("classification")),
                     "wind_kt": _num(s.get("intensity")), "pressure_mb": _num(s.get("pressure")),
                     "lat": s.get("latitudeNumeric"), "lon": s.get("longitudeNumeric"),
                     "moving_deg": _num(s.get("movementDir")), "moving_mph": _num(s.get("movementSpeed")),
                     "updated": s.get("lastUpdate"), "advisory": (s.get("publicAdvisory") or {}).get("url")})
    if rows:
        parts = [f"{r['kind']} {r['name']} ({r['wind_kt']} kt)" for r in rows]
        summary = f"{len(rows)} active systems in the Atlantic and eastern/central Pacific: " + "; ".join(parts) + "."
    else:
        summary = "No active tropical cyclones in the Atlantic or eastern/central Pacific."
    return _answer(summary, rows, got, coverage="NOAA NHC: Atlantic, eastern and central Pacific")


# ---------------------------------------------------------------------------------------------------------- aircraft
def aircraft_in_area(lat: float, lon: float, radius_nm: float = 25.0, limit: int = 25) -> dict[str, Any]:
    _check_point(lat, lon)
    radius = max(1, min(int(round(radius_nm)), 250))
    key = f"{lat:.2f},{lon:.2f},{radius}"
    got = _fetch("adsb-lol", 15, key, lambda: http_json(f"https://api.adsb.lol/v2/point/{lat:.4f}/{lon:.4f}/{radius}"))
    rows = []
    for a in got.data.get("ac", []) or []:
        if a.get("lat") is None or a.get("lon") is None:
            continue
        alt = a.get("alt_baro")
        emergency = a.get("emergency") if a.get("emergency") not in (None, "none") else None
        rows.append({"hex": a.get("hex"), "callsign": (a.get("flight") or "").strip() or None, "registration": a.get("r"),
                     "type": a.get("t"), "altitude_ft": 0 if alt == "ground" else alt, "on_ground": alt == "ground",
                     "ground_speed_kt": a.get("gs"), "track_deg": a.get("track"), "lat": a.get("lat"), "lon": a.get("lon"),
                     "distance_nm": round(haversine_km(lat, lon, a["lat"], a["lon"]) / 1.852, 1),
                     "squawk": a.get("squawk"), "emergency": emergency})
    rows.sort(key=lambda r: r["distance_nm"])
    total = len(rows)
    airborne = sum(1 for r in rows if not r["on_ground"])
    alarms = [r for r in rows if r["emergency"] or r["squawk"] in ("7500", "7600", "7700")]
    summary = f"{total} aircraft within {radius} NM of {lat:.2f}, {lon:.2f} ({airborne} airborne)."
    if rows:
        c = rows[0]
        summary += f" Closest: {c['callsign'] or c['hex']} ({c['type'] or 'type unknown'}) at {c['distance_nm']} NM, {c['altitude_ft']} ft."
    if alarms:
        summary += f" {len(alarms)} squawking an emergency code."
    return _answer(summary, rows[:limit], got, limit=limit, total=total)


def _port(p) -> dict[str, Any]:
    return {k: (p or {}).get(k) for k in ("iata_code", "icao_code", "name", "municipality", "country_name")}


def aircraft_info(hex_code: str = "", callsign: str = "") -> dict[str, Any]:
    callsign = (callsign or "").strip().upper()
    if not (hex_code or "").strip() and not callsign:
        raise PublicDataError("give the ICAO hex code (e.g. 4951d4) or the callsign (e.g. TAP837)")
    data: dict[str, Any] = {}
    got = None
    if (hex_code or "").strip():
        hx = _hex6(hex_code)
        got = _fetch("adsbdb", 86400, f"aircraft:{hx}", lambda: http_json(f"https://api.adsbdb.com/v0/aircraft/{hx}"))
        resp = got.data.get("response") if isinstance(got.data, dict) else None
        a = resp.get("aircraft") if isinstance(resp, dict) else None
        if a:
            data["aircraft"] = {k: a.get(k) for k in ("type", "icao_type", "manufacturer", "registration", "registered_owner",
                                                      "registered_owner_country_name", "url_photo")}
    if callsign:
        got = _fetch("adsbdb", 3600, f"callsign:{callsign}", lambda: http_json(f"https://api.adsbdb.com/v0/callsign/{callsign}"))
        resp = got.data.get("response") if isinstance(got.data, dict) else None
        r = resp.get("flightroute") if isinstance(resp, dict) else None
        if r:
            data["route"] = {"callsign": r.get("callsign"), "airline": (r.get("airline") or {}).get("name"),
                             "origin": _port(r.get("origin")), "destination": _port(r.get("destination"))}
    parts = []
    if data.get("aircraft"):
        a = data["aircraft"]
        parts.append(f"{a['registration']} is a {a['manufacturer']} {a['type']} of {a['registered_owner']}")
    if data.get("route"):
        r = data["route"]
        parts.append(f"{r['callsign']} ({r['airline']}) flies {r['origin']['municipality']} ({r['origin']['iata_code']}) to "
                     f"{r['destination']['municipality']} ({r['destination']['iata_code']})")
    summary = ". ".join(parts) + "." if parts else "The registry has no record for that aircraft or callsign."
    return _answer(summary, data, got)


def aircraft_track(hex_code: str, max_points: int = 120) -> dict[str, Any]:
    hx = _hex6(hex_code)
    max_points = max(2, min(int(max_points), 1000))
    got = _fetch("opensky-tracks", 60, hx, lambda: http_json(f"https://opensky-network.org/api/tracks/all?icao24={hx}&time=0"))
    path = (got.data.get("path") or []) if isinstance(got.data, dict) else []
    pts = [{"time": _iso(p[0]), "lat": p[1], "lon": p[2], "altitude_m": p[3], "track_deg": p[4], "on_ground": p[5]} for p in path]
    total = len(pts)
    if total > max_points:                                  # keep the shape: evenly spaced, first and last always in
        step = (total - 1) / (max_points - 1)
        pts = [pts[round(i * step)] for i in range(max_points)]
    if path:
        dist = sum(haversine_km(a[1], a[2], b[1], b[2]) for a, b in zip(path, path[1:]))
        top = max((p[3] or 0) for p in path)
        name = (got.data.get("callsign") or hx).strip()
        summary = (f"Track of {name}: {total} points from {_iso(path[0][0])} to {_iso(path[-1][0])}, {dist:.0f} km flown, "
                   f"highest {top} m; last seen at {path[-1][1]:.3f}, {path[-1][2]:.3f}.")
    else:
        summary = f"No track for {hx} in the last flight data."
    return _answer(summary, pts, got, limit=max_points, total=total)


# ---------------------------------------------------------------------------------------------------------- wind
def _compass(deg) -> str:
    if deg is None:
        return "?"
    names = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return names[int((float(deg) % 360) / 22.5 + 0.5) % 16]


def wind(lat: float, lon: float, hours: int = 12) -> dict[str, Any]:
    _check_point(lat, lon)
    hours = max(1, min(int(hours), 72))
    url = ("https://api.open-meteo.com/v1/forecast?latitude={:.3f}&longitude={:.3f}"
           "&current=wind_speed_10m,wind_direction_10m,wind_gusts_10m"
           "&hourly=wind_speed_10m,wind_direction_10m,wind_gusts_10m&forecast_hours={}&timezone=UTC").format(lat, lon, hours)
    got = _fetch("open-meteo-wind", 600, f"{lat:.2f},{lon:.2f},{hours}", lambda: http_json(url))
    cur, hourly = got.data.get("current") or {}, got.data.get("hourly") or {}
    rows = [{"time": t, "speed_kmh": s, "direction_deg": d, "gusts_kmh": g}
            for t, s, d, g in zip(hourly.get("time", []), hourly.get("wind_speed_10m", []), hourly.get("wind_direction_10m", []),
                                  hourly.get("wind_gusts_10m", []))]
    summary = (f"Wind at {lat:.2f}, {lon:.2f}: {cur.get('wind_speed_10m')} km/h from the {_compass(cur.get('wind_direction_10m'))} "
               f"({cur.get('wind_direction_10m')} deg), gusts {cur.get('wind_gusts_10m')} km/h.")
    if rows:
        peak = max(rows, key=lambda r: r["gusts_kmh"] or 0)
        summary += f" Strongest gusts in the next {hours} h: {peak['gusts_kmh']} km/h at {peak['time']}Z."
    return _answer(summary, {"current": cur, "hourly": rows}, got)


# ---------------------------------------------------------------------------------------------------------- satellites
def satellite_passes(lat: float, lon: float, norad_id: int = 25544, name: str = "", hours: int = 24,
                     min_elevation: float = 10.0, altitude_m: float = 0.0, limit: int = 10,
                     start: Optional[datetime] = None) -> dict[str, Any]:
    from .orbits import parse_tle, passes
    _check_point(lat, lon)
    hours = max(1, min(int(hours), 72))
    query = f"NAME={name.strip()}" if (name or "").strip() else f"CATNR={int(norad_id)}"
    got = _fetch("celestrak-tle", 6 * 3600, query,
                 lambda: http_text(f"https://celestrak.org/NORAD/elements/gp.php?{query}&FORMAT=TLE"))
    sats = parse_tle(got.data if isinstance(got.data, str) else "")
    if not sats:
        raise PublicDataError(f"CelesTrak has no orbital elements for {name or norad_id}")
    sat_name, l1, l2 = sats[0]
    begin = (start or datetime.now(timezone.utc)).replace(microsecond=0)
    rows = passes(l1, l2, lat, lon, altitude_m, begin, begin + timedelta(hours=hours), min_elevation)
    total = len(rows)
    if rows:
        best = max(rows, key=lambda r: r["max_elevation_deg"])
        summary = (f"{total} passes of {sat_name} above {min_elevation:g} deg over {lat:.2f}, {lon:.2f} in the next {hours} h. "
                   f"Next: rises {rows[0]['rise']} in the {rows[0]['rise_compass']}, peaks at {rows[0]['max_elevation_deg']} deg. "
                   f"Highest: {best['max_elevation_deg']} deg at {best['peak']}.")
    else:
        summary = f"{sat_name} does not rise above {min_elevation:g} deg over {lat:.2f}, {lon:.2f} in the next {hours} h."
    return _answer(summary, rows[:limit], got, limit=limit, total=total, satellite=sat_name)


__all__ = ["PublicDataError", "aircraft_in_area", "aircraft_info", "aircraft_track", "cyclones", "earthquakes",
           "haversine_km", "satellite_passes", "set_transport", "wind"]
