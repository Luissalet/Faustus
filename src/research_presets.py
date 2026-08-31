"""Deep Research settings that match the machine it runs on (FAUSTUS).

Roadmap, high priority: *"Deep Research model presets by hardware. Recommend
approved model/parameter profiles for small, medium, and large local setups so
people with different hardware can use Deep Research without guessing."*

Deep Research has seven numbers behind it — token budget, extraction
concurrency, three timeouts, run ceiling — and their defaults are tuned for a
fast hosted model. On a local card the same defaults fail in a specific,
confusing way: several extractions fight over one GPU, each one blows the 90s
timeout, and the run ends with "no content" as if the web were empty. Nothing
in the app connects "12 GB card" to "use 3 concurrent extractions, not 6".

This maps detected VRAM to a tier, produces the exact settings patch, and —
just as important — reports the *blockers* that make Deep Research return
nothing no matter how well it is tuned: no search provider that actually
answers, or no model chosen.

Pure functions with injected hardware/probe values, so the whole thing tests
without a GPU, a network or a search engine.
"""

import logging
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# Keys this module owns. Anything else in settings is left alone.
RESEARCH_KEYS = (
    "research_max_tokens",
    "research_extraction_concurrency",
    "research_extraction_timeout_seconds",
    "research_planning_timeout_seconds",
    "research_query_timeout_seconds",
    "research_run_timeout_seconds",
)

# (name, min VRAM GB inclusive, max VRAM GB exclusive, patch, note)
TIERS = (
    ("tight", 0.0, 10.0, {
        "research_max_tokens": 8192,
        "research_extraction_concurrency": 2,
        "research_extraction_timeout_seconds": 180,
        "research_planning_timeout_seconds": 180,
        "research_query_timeout_seconds": 180,
        "research_run_timeout_seconds": 2700,
    }, "Under 10 GB: one 7-9B model at a short context. Two extractions at a "
       "time and generous timeouts — the card is the bottleneck, not the web."),
    ("mid", 10.0, 17.0, {
        "research_max_tokens": 16384,
        "research_extraction_concurrency": 3,
        "research_extraction_timeout_seconds": 120,
        "research_planning_timeout_seconds": 120,
        "research_query_timeout_seconds": 120,
        "research_run_timeout_seconds": 2400,
    }, "10-16 GB: a 9-14B model at 16-32k context fits with room for KV cache. "
       "Three extractions keep the GPU busy without queueing behind itself."),
    ("roomy", 17.0, 33.0, {
        "research_max_tokens": 32768,
        "research_extraction_concurrency": 4,
        "research_extraction_timeout_seconds": 90,
        "research_planning_timeout_seconds": 90,
        "research_query_timeout_seconds": 90,
        "research_run_timeout_seconds": 1800,
    }, "17-32 GB: a 27-32B model at a long context, or a smaller one with lots "
       "of parallel extraction."),
    ("big", 33.0, float("inf"), {
        "research_max_tokens": 65536,
        "research_extraction_concurrency": 6,
        "research_extraction_timeout_seconds": 90,
        "research_planning_timeout_seconds": 90,
        "research_query_timeout_seconds": 90,
        "research_run_timeout_seconds": 1800,
    }, "33 GB and up: the defaults stop being the limit; raise concurrency "
       "until the search provider, not the GPU, is the bottleneck."),
)

# Providers that need a key, and the setting the key lives in.
_KEYED_PROVIDERS = {
    "brave": "brave_api_key",
    "tavily": "tavily_api_key",
    "serper": "serper_api_key",
    "google_pse": "google_pse_key",
}


def detect_hardware() -> Dict[str, Optional[float]]:
    """Best-effort VRAM/RAM detection. Never raises; unknown values are None."""
    out: Dict[str, Optional[float]] = {"vram_gb": None, "ram_gb": None,
                                       "gpu_name": None}
    try:
        from services.hwfit.hardware import detect_system
        system = detect_system() or {}
        if isinstance(system, dict):
            vram = system.get("gpu_vram_gb")
            out["vram_gb"] = float(vram) if vram else None
            out["gpu_name"] = system.get("gpu_name")
            for key in ("ram_gb", "total_ram_gb", "system_ram_gb"):
                if system.get(key):
                    out["ram_gb"] = float(system[key])
                    break
    except Exception as e:
        logger.debug("research_presets: hardware detection unavailable: %s",
                     type(e).__name__)
    return out


def tier_for(vram_gb: Optional[float]) -> Dict[str, Any]:
    """Pick the tier for a card. Unknown VRAM gets the conservative one."""
    if not vram_gb or vram_gb <= 0:
        name, _lo, _hi, patch, note = TIERS[0]
        return {"name": name, "patch": dict(patch),
                "note": note + " (VRAM unknown — assuming the small profile.)"}
    for name, lo, hi, patch, note in TIERS:
        if lo <= float(vram_gb) < hi:
            return {"name": name, "patch": dict(patch), "note": note}
    name, _lo, _hi, patch, note = TIERS[-1]
    return {"name": name, "patch": dict(patch), "note": note}


def blockers(settings: Dict[str, Any], *,
             searxng_ok: Optional[bool] = None) -> List[Dict[str, Any]]:
    """What stops Deep Research returning anything, regardless of tuning."""
    found: List[Dict[str, Any]] = []
    provider = str(settings.get("search_provider") or "searxng").strip()

    if provider == "disabled":
        found.append({
            "key": "search_disabled",
            "text": "Search is disabled, so Deep Research has nothing to read.",
            "fix": {"search_provider": "duckduckgo"},
            "fix_label": "Switch to DuckDuckGo (no key, no server)",
        })
    elif provider == "searxng" and searxng_ok is False:
        found.append({
            "key": "searxng_unreachable",
            "text": "The search provider is SearXNG but no instance answers, so "
                    "every query comes back empty.",
            "fix": {"search_provider": "duckduckgo"},
            "fix_label": "Switch to DuckDuckGo (no key, no server)",
        })
    elif provider in _KEYED_PROVIDERS and not str(
            settings.get(_KEYED_PROVIDERS[provider]) or "").strip():
        found.append({
            "key": "missing_api_key",
            "text": f"The {provider} provider is selected but its API key is empty.",
            "fix": {"search_provider": "duckduckgo"},
            "fix_label": "Switch to DuckDuckGo (no key, no server)",
        })

    if not str(settings.get("research_model") or "").strip() and not str(
            settings.get("default_model") or "").strip():
        found.append({
            "key": "no_model",
            "text": "No research model and no default model are set, so a run "
                    "has nothing to think with.",
            "fix": None,
            "fix_label": "Pick a model in Settings",
        })
    return found


def recommend(settings: Dict[str, Any], *,
              vram_gb: Optional[float] = None,
              ram_gb: Optional[float] = None,
              gpu_name: Optional[str] = None,
              searxng_ok: Optional[bool] = None) -> Dict[str, Any]:
    """Full recommendation: tier, the patch, what would change, what is broken."""
    settings = settings or {}
    tier = tier_for(vram_gb)
    changes = []
    for key, value in tier["patch"].items():
        current = settings.get(key)
        if current != value:
            changes.append({"key": key, "from": current, "to": value})
    return {
        "tier": tier["name"],
        "note": tier["note"],
        "vram_gb": vram_gb,
        "ram_gb": ram_gb,
        "gpu_name": gpu_name,
        "patch": tier["patch"],
        "changes": changes,
        "already_applied": not changes,
        "blockers": blockers(settings, searxng_ok=searxng_ok),
    }


def apply_patch(patch: Dict[str, Any], *,
                load: Optional[Callable] = None,
                save: Optional[Callable] = None,
                allow: Optional[tuple] = None) -> Dict[str, Any]:
    """Write only the keys this module owns; ignore anything else in `patch`.

    An "apply preset" button that can set arbitrary settings is an arbitrary
    settings-write endpoint wearing a friendly label.
    """
    if load is None or save is None:
        from src.settings import load_settings, save_settings
        load = load or load_settings
        save = save or save_settings
    allowed = set(allow or (RESEARCH_KEYS + ("search_provider",)))
    settings = load() or {}
    written = {}
    for key, value in (patch or {}).items():
        if key in allowed:
            settings[key] = value
            written[key] = value
    if written:
        save(settings)
    return {"ok": True, "written": written,
            "ignored": sorted(set(patch or {}) - set(written))}
