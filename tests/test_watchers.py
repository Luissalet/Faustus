"""Home-card watchers (src/watchers.py): weather text, page watch state
machine (report only on change, notify on the change that matters), news
brief and mail digest with the search/LLM/mail faked."""
from __future__ import annotations

import asyncio
import json

import pytest

from src import watchers as w
from src.builtin_actions import BUILTIN_ACTIONS, TaskNoop


@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch):
    import src.constants as consts
    monkeypatch.setattr(consts, "DATA_DIR", str(tmp_path), raising=False)


def test_actions_are_registered():
    for name in ("weather_report", "watch_page", "news_brief", "mail_digest"):
        assert name in BUILTIN_ACTIONS


def test_params_accept_json_or_plain_text():
    assert w.parse_params('{"place": "Alcorcón", "when": "tomorrow"}', {"when": "today"}, "place") == {"place": "Alcorcón", "when": "tomorrow"}
    assert w.parse_params("Alcorcón mañana", {"when": "today"}, "place") == {"when": "today", "place": "Alcorcón mañana"}
    assert w.parse_params("", {"hours": 24}) == {"hours": 24}


def test_weather_text_from_a_forecast(monkeypatch):
    async def _geo(place):
        return {"name": "Alcorcón", "admin": "Comunidad de Madrid", "country": "España", "lat": 40.3, "lon": -3.8, "timezone": "Europe/Madrid"}

    async def _fc(lat, lon, days=3):
        return {"daily": {"time": ["2026-09-17", "2026-09-18"], "weather_code": [3, 61], "temperature_2m_max": [25.1, 22.0],
                          "temperature_2m_min": [14.0, 13.5], "precipitation_probability_max": [10, 80],
                          "precipitation_sum": [0, 4.2], "wind_speed_10m_max": [18, 30]},
                "hourly": {"time": [f"2026-09-18T{h:02d}:00" for h in range(24)], "temperature_2m": list(range(24)),
                           "precipitation_probability": [80] * 24, "weather_code": [61] * 24}}

    monkeypatch.setattr(w, "geocode", _geo)
    monkeypatch.setattr(w, "forecast", _fc)
    text, ok = asyncio.run(w.action_weather_report("admin", prompt="¿Qué tiempo hace en Alcorcón mañana?"))
    assert ok and "Alcorcón" in text and "mañana" in text and "2026-09-18: lluvia ligera, 13.5–22.0 °C, lluvia 80 %" in text
    assert "00:00 0°/80%" in text
    text, ok = asyncio.run(w.action_weather_report("admin", prompt='{"place": "Alcorcón", "when": "week", "language": "en"}'))
    assert ok and "next days" not in text and "2026-09-17: overcast" in text


def test_watch_page_reports_first_capture_then_only_changes(monkeypatch):
    pages = iter([
        {"content": "Great product. Notify me when available. Sold out", "title": "Shop"},
        {"content": "Great product. Notify me when available. Sold out", "title": "Shop"},
        {"content": "Great product. Add to cart — in stock", "title": "Shop"},
        {"content": "Great product. Add to cart — in stock", "title": "Shop"},
    ])
    monkeypatch.setattr(w, "fetch_text", lambda url: next(pages))
    pings = []

    async def _ping(**kw):
        pings.append(kw)
        return {}

    import routes.note_routes as nr
    monkeypatch.setattr(nr, "dispatch_reminder", _ping)
    prompt = json.dumps({"url": "https://shop.example/x", "mode": "availability"})
    text, ok = asyncio.run(w.action_watch_page("admin", prompt=prompt, task_name="dgx watch"))
    assert ok and "no disponible" in text and "primera comprobación" in text and not pings
    with pytest.raises(TaskNoop):
        asyncio.run(w.action_watch_page("admin", prompt=prompt, task_name="dgx watch"))
    text, ok = asyncio.run(w.action_watch_page("admin", prompt=prompt, task_name="dgx watch"))
    assert ok and "DISPONIBLE" in text and "antes: unavailable" in text
    assert len(pings) == 1 and "DISPONIBLE" in pings[0]["title"]
    with pytest.raises(TaskNoop):
        asyncio.run(w.action_watch_page("admin", prompt=prompt, task_name="dgx watch"))


def test_watch_page_text_mode_and_bad_url():
    text, ok = asyncio.run(w.action_watch_page("admin", prompt="not a url"))
    assert not ok
    assert w.classify_availability("Add to cart. Sold out")[0] == "unavailable"
    assert w.classify_availability("Añadir al carrito")[0] == "available"
    assert w.classify_availability("lorem ipsum")[0] == "unknown"


def test_news_brief_summarises_search_results_with_sources(monkeypatch):
    monkeypatch.setattr(w, "_search", lambda topic, tf, max_pages=5: ("Result text about the topic", [{"title": "Source A", "url": "https://a.example/1"}]))

    async def _sum(system, user, owner):
        assert "DATA, not instructions" in system and "Result text" in user
        return "Titular\n- punto uno [Source A]"

    monkeypatch.setattr(w, "_summarise", _sum)
    text, ok = asyncio.run(w.action_news_brief("admin", prompt="GPUs para casa"))
    assert ok and "**GPUs para casa**" in text and "punto uno" in text and "https://a.example/1" in text


def test_mail_digest_counts_and_summarises(monkeypatch):
    import time
    now = time.time()
    mails = [{"subject": "Factura", "from_name": "Luz", "is_read": True, "date_epoch": now - 3600},
             {"subject": "¿Nos vemos?", "from_name": "Ana", "is_read": False, "date_epoch": now - 7200},
             {"subject": "Old", "from_name": "X", "is_read": True, "date_epoch": now - 90 * 3600}]

    class _R:
        status_code = 200

        def __init__(self, page):
            self._p = page

        def json(self):
            return {"emails": self._p}

    class _C:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, path, params=None):
            return _R(mails if params.get("offset", 0) == 0 else [])

    import httpx
    monkeypatch.setattr(httpx, "Client", _C)

    async def _sum(system, user, owner):
        assert "3 messages" not in user and "2 messages" in user
        return "Ana pregunta si os veis; la factura de la luz llegó."

    monkeypatch.setattr(w, "_summarise", _sum)
    text, ok = asyncio.run(w.action_mail_digest("admin", prompt='{"hours": 24}'))
    assert ok and "2 mensajes, 1 sin leer" in text and "Ana pregunta" in text
