"""Keep the WhatsApp bridge running wherever this Faustus runs.

The bridge is a detached child (it survives a Faustus restart) but nothing brought it back after a reboot, a crash or
the Hoard Hub moving Faustus to a Spark, so the account went silent for days without anyone noticing. This loop:

* starts the bridge when a session is paired here, the person has not pressed Stop (``whatsapp_bridge.wanted``) and
  the ``whatsapp_keep_running`` setting is on (default);
* never starts it on a copy the Hub has moved elsewhere (``data/runtime/placement.json``): the session must run in one
  place only, or both copies break their encryption keys — and it stops one that is still running here then;
* publishes what it saw (``last``) for the status screen; a session whose keys broke (``health.needs_pairing``) is
  reported, not restarted in a loop, because only a new QR fixes it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Dict, Optional

from src import whatsapp_bridge as wa

logger = logging.getLogger(__name__)

INTERVAL_S = 60.0
FIRST_DELAY_S = 20.0
last: Dict[str, Any] = {"at": None, "action": "none", "detail": ""}


def placed_elsewhere() -> Optional[Dict[str, Any]]:
    """The Hub's marker when this Faustus copy is not the live one (it runs on a Spark), else None."""
    try:
        from src.constants import DATA_DIR
        path = os.path.join(DATA_DIR, "runtime", "placement.json")
        with open(path, encoding="utf-8") as fh:
            where = json.load(fh)
        return where if isinstance(where, dict) and where.get("location") not in (None, "", "pc") else None
    except (OSError, ValueError):
        return None


def enabled() -> bool:
    try:
        from src.settings import get_setting
        return bool(get_setting("whatsapp_keep_running", True))
    except Exception:  # noqa: BLE001
        return True


def _note(action: str, detail: str = "") -> Dict[str, Any]:
    last.update(at=time.time(), action=action, detail=detail)
    return dict(last)


def tick() -> Dict[str, Any]:
    """One look: start, stop or leave the bridge. Synchronous (runs in a thread)."""
    elsewhere = placed_elsewhere()
    st = wa.probe()
    if elsewhere:
        if st is not None:
            res = wa.stop()
            return _note("stopped_here", f"Faustus runs on {elsewhere.get('node') or elsewhere.get('location')}: {res}")
        return _note("elsewhere", str(elsewhere.get("node") or elsewhere.get("location")))
    if not enabled():
        return _note("disabled")
    if st is not None:
        h = wa.health(st)
        return _note("needs_pairing" if h.get("needs_pairing") else "running", h.get("state", ""))
    if not wa.paired():
        return _note("not_paired")
    if not wa.wanted():
        return _note("stopped_by_person")
    res = wa.start(owner="whatsapp-keeper")
    if res.get("started") or res.get("already_running"):
        logger.info("WhatsApp bridge started by the keeper (pid %s)", res.get("pid"))
        return _note("started", str(res.get("pid") or ""))
    logger.warning("WhatsApp keeper could not start the bridge: %s", res.get("error"))
    return _note("start_failed", str(res.get("error") or ""))


async def keep_loop() -> None:
    await asyncio.sleep(FIRST_DELAY_S)
    while True:
        try:
            await asyncio.to_thread(tick)
        except Exception as exc:  # noqa: BLE001 - the loop must outlive one bad look
            logger.warning("WhatsApp keeper: %s", exc)
            _note("error", str(exc)[:300])
        await asyncio.sleep(INTERVAL_S)
