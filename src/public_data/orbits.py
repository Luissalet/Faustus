"""Satellite passes from two-line elements: SGP4 positions (TEME), rotated to Earth-fixed by Greenwich sidereal time, seen
from the observer as elevation and azimuth. Polar motion and nutation are ignored: fine to a few tenths of a degree, which
is what "when can I see it" needs.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from sgp4.api import Satrec, jday

A_WGS84 = 6378.137
F_WGS84 = 1 / 298.257223563
STEP_S = 20
_COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def parse_tle(text: str) -> list[tuple[str, str, str]]:
    lines = [ln.rstrip() for ln in (text or "").splitlines() if ln.strip()]
    out = []
    i = 0
    while i < len(lines):
        if lines[i].startswith("1 ") and i + 1 < len(lines) and lines[i + 1].startswith("2 "):
            out.append((f"NORAD {lines[i][2:7].strip()}", lines[i], lines[i + 1]))
            i += 2
        elif i + 2 < len(lines) and lines[i + 1].startswith("1 ") and lines[i + 2].startswith("2 "):
            out.append((lines[i].strip(), lines[i + 1], lines[i + 2]))
            i += 3
        else:
            i += 1
    return out


def gmst_rad(jd_ut1: float) -> float:
    t = (jd_ut1 - 2451545.0) / 36525.0
    deg = 280.46061837 + 360.98564736629 * (jd_ut1 - 2451545.0) + 0.000387933 * t * t - t ** 3 / 38710000.0
    return math.radians(deg % 360.0)


def observer_ecef(lat: float, lon: float, alt_m: float) -> tuple[float, float, float]:
    la, lo = math.radians(lat), math.radians(lon)
    e2 = F_WGS84 * (2 - F_WGS84)
    n = A_WGS84 / math.sqrt(1 - e2 * math.sin(la) ** 2)
    h = alt_m / 1000.0
    return ((n + h) * math.cos(la) * math.cos(lo), (n + h) * math.cos(la) * math.sin(lo), (n * (1 - e2) + h) * math.sin(la))


def look(sat: Satrec, when: datetime, lat: float, lon: float, obs: tuple[float, float, float]) -> tuple[float, float, float]:
    """(elevation deg, azimuth deg, range km) of the satellite at ``when`` (UTC)."""
    jd, fr = jday(when.year, when.month, when.day, when.hour, when.minute, when.second + when.microsecond / 1e6)
    err, r, _v = sat.sgp4(jd, fr)
    if err:
        raise ValueError(f"SGP4 error {err}")
    g = gmst_rad(jd + fr)
    x = math.cos(g) * r[0] + math.sin(g) * r[1]
    y = -math.sin(g) * r[0] + math.cos(g) * r[1]
    z = r[2]
    dx, dy, dz = x - obs[0], y - obs[1], z - obs[2]
    la, lo = math.radians(lat), math.radians(lon)
    east = -math.sin(lo) * dx + math.cos(lo) * dy
    north = -math.sin(la) * math.cos(lo) * dx - math.sin(la) * math.sin(lo) * dy + math.cos(la) * dz
    up = math.cos(la) * math.cos(lo) * dx + math.cos(la) * math.sin(lo) * dy + math.sin(la) * dz
    rng = math.sqrt(dx * dx + dy * dy + dz * dz)
    return math.degrees(math.asin(up / rng)), math.degrees(math.atan2(east, north)) % 360.0, rng


def compass(az: float) -> str:
    return _COMPASS[int((az % 360) / 22.5 + 0.5) % 16]


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def passes(line1: str, line2: str, lat: float, lon: float, alt_m: float, start: datetime, end: datetime,
           min_elevation: float = 10.0) -> list[dict]:
    sat = Satrec.twoline2rv(line1, line2)
    obs = observer_ecef(lat, lon, alt_m)
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    out: list[dict] = []
    current = None
    t = start
    prev_el = None
    while t <= end:
        el, az, rng = look(sat, t, lat, lon, obs)
        if el >= min_elevation:
            if current is None:
                current = {"rise_t": t, "rise_az": az, "peak_t": t, "peak_el": el, "peak_az": az, "min_range": rng}
                if prev_el is None:                         # already up at the start: say so instead of inventing a rise
                    current["rising_before_start"] = True
            elif el > current["peak_el"]:
                current.update(peak_t=t, peak_el=el, peak_az=az)
            current["min_range"] = min(current["min_range"], rng)
            current["set_t"], current["set_az"] = t, az
        elif current is not None:
            out.append(_row(_refine(current, sat, lat, lon, obs)))
            current = None
        prev_el = el
        t += timedelta(seconds=STEP_S)
    if current is not None:
        current["still_up_at_end"] = True
        out.append(_row(_refine(current, sat, lat, lon, obs)))
    return out


def _refine(p: dict, sat: Satrec, lat: float, lon: float, obs) -> dict:
    """The peak to the second: the coarse scan steps 20 s, so look again second by second around it."""
    best_t, best_el, best_az = p["peak_t"], p["peak_el"], p["peak_az"]
    for s in range(-STEP_S, STEP_S + 1):
        t = p["peak_t"] + timedelta(seconds=s)
        el, az, _ = look(sat, t, lat, lon, obs)
        if el > best_el:
            best_t, best_el, best_az = t, el, az
    p.update(peak_t=best_t, peak_el=best_el, peak_az=best_az)
    return p


def _row(p: dict) -> dict:
    row = {"rise": _iso(p["rise_t"]), "rise_azimuth_deg": round(p["rise_az"]), "rise_compass": compass(p["rise_az"]),
           "peak": _iso(p["peak_t"]), "max_elevation_deg": round(p["peak_el"], 1), "peak_compass": compass(p["peak_az"]),
           "set": _iso(p["set_t"]), "set_compass": compass(p["set_az"]),
           "duration_s": int((p["set_t"] - p["rise_t"]).total_seconds()), "closest_km": round(p["min_range"])}
    if p.get("rising_before_start"):
        row["up_at_start"] = True
    if p.get("still_up_at_end"):
        row["up_at_end"] = True
    return row
