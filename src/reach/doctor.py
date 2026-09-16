"""`reach doctor`: per-channel/backend health without spending quota,
modeled on Agent-Reach's `agent-reach doctor` (contract §1) but going one
step further -- `live=True` makes one real, cheap, cached request per
backend instead of only checking presence of config/deps.

Never includes a token/cookie value in its output; only "configured" /
"missing" derived from `src.reach.credentials`.
"""
from __future__ import annotations

from typing import Any

from src.reach.router import CHANNELS


async def doctor(live: bool = False) -> dict[str, Any]:
    channels_out: dict[str, Any] = {}
    ready_count = 0
    for name, channel in CHANNELS.items():
        backends_out = []
        channel_ready = False
        for backend in channel.backends:
            availability = await backend.available(live=live)
            backends_out.append({
                "name": backend.name,
                "status": availability.status,
                "reason": availability.reason,
                "checked_live": availability.checked_live,
            })
            if availability.status == "ready" and not channel_ready:
                channel_ready = True
        if channel_ready:
            ready_count += 1
        active_backend = next((b["name"] for b in backends_out if b["status"] == "ready"), None)
        channels_out[name] = {
            "status": "ready" if channel_ready else (
                "needs_config" if any(b["status"] == "needs_config" for b in backends_out) else "unavailable"
            ),
            "active_backend": active_backend,
            "backends": backends_out,
        }
    return {
        "channels": channels_out,
        "ready_count": ready_count,
        "total_count": len(CHANNELS),
        "summary": f"{ready_count}/{len(CHANNELS)} channels ready",
        "live": live,
    }
