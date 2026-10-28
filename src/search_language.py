"""Empty searches caused by the language of the query.

A search tool of a Hoard (or of Faustus) that returns nothing is often right
about the data and wrong about the query: the user writes "Shaymin Forma Cielo",
the catalogue holds "Shaymin Sky Forme". The harness already corrects the
language of the REPLY (``reply_language``); this module covers the language of
the tool ARGUMENTS.

* Each plugin manifest may declare ``data_language`` (``plugins.py``); a tool
  without a declaration uses Spanish and English.
* When a search tool comes back empty and the query is not (only) in the
  language of the data, the tool result gets a short note with the data
  languages and, when the alias service of Hoard Link exists, the equivalent
  names (``alias_equivalents``).
* After a hint, a second empty search in a row (or the same query twice) ends
  the loop: the tool is hidden for the rest of the turn and the note tells the
  model to answer with what it has.
* The number of empty searches of the turn is kept (``empty_searches``) and
  counted in the observations activity.

Runs as ``post_tool`` / ``turn_start`` listeners of ``lifecycle_hooks``: it only
reads and counts, never blocks a call, and never raises into the agent loop.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from collections import OrderedDict
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

LISTENER_ID = "search_language"
MAX_TRACKED_TURNS = 256
QUERY_CHARS = 120

#: Argument names that carry the text being looked for, most specific first.
QUERY_KEYS = (
    "query", "q", "search", "search_term", "term", "text", "keyword", "keywords",
    "name", "title", "pattern", "prompt",
)
_NAME_SEARCH = re.compile(r"search|find|lookup|look_up|query|buscar|suggest|autocomplete|resolve_name", re.I)
_NAME_LIST_LIKE = re.compile(r"^(list|get|browse|resolve)_", re.I)
#: Searches whose results do not depend on the language the data is written in
#: (the web, code, files, mail, the chat itself, the tool catalogue).
_NOT_DATA = re.compile(
    r"web|grep|glob|fetch|browse|internet|google|bing|duck|tavily|brave|serp|wiki|"
    r"code|file|symbol|tool|session|chat|history|email|mail|calendar|slack|git|repo|"
    r"deep_research|screen|desktop|ocr|image|video|audio|voice",
    re.I,
)
_EMPTY_TEXT = re.compile(
    r"^\W*(no (results?|matches|hits|entries|items|records)( found)?|nothing found|not found|0 (results?|matches)|"
    r"sin resultados|no se (han )?encontr|no hay (resultados|coincidencias)|ning[uú]n resultado|empty|vac[ií]o|none|null)\b",
    re.I,
)
_LIST_KEYS = ("results", "items", "matches", "hits", "rows", "entries", "data", "records", "found", "names")
_COUNT_KEYS = ("count", "total", "n", "num_results", "total_results", "found_count")

_LANGUAGE_NAMES = {
    "es": ("español", "Spanish"), "en": ("inglés", "English"), "fr": ("francés", "French"),
    "de": ("alemán", "German"), "pt": ("portugués", "Portuguese"), "it": ("italiano", "Italian"),
    "ja": ("japonés", "Japanese"), "ca": ("catalán", "Catalan"),
}


# -- what a search is, and whether it came back empty -------------------------

def short_name(tool: Any) -> str:
    name = str(tool or "")
    return name.rsplit("__", 1)[-1] if name.startswith("mcp__") else name


def _args(content: Any) -> Dict[str, Any]:
    if isinstance(content, dict):
        return content
    if isinstance(content, str) and content.strip().startswith("{"):
        try:
            value = json.loads(content)
            return value if isinstance(value, dict) else {}
        except (ValueError, TypeError):
            return {}
    return {}


def query_of(content: Any) -> str:
    """The text a search call looks for, or an empty string."""
    args = _args(content)
    for key in QUERY_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())[:QUERY_CHARS]
        if isinstance(value, list) and value and all(isinstance(v, str) for v in value):
            return " ".join(" ".join(value).split())[:QUERY_CHARS]
    return ""


def is_data_search(tool: Any, content: Any = None) -> bool:
    """A search over a catalogue of data (a Hoard's or Faustus' own)."""
    name = short_name(tool)
    if not name or _NOT_DATA.search(name):
        return False
    if _NAME_SEARCH.search(name):
        return True
    return bool(_NAME_LIST_LIKE.match(name) and query_of(content))


def _empty_value(value: Any) -> bool:
    if value is None or value is False:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, set)):
        return len(value) == 0
    if isinstance(value, dict):
        if not value:
            return True
        lists = [v for k, v in value.items() if k in _LIST_KEYS and isinstance(v, (list, tuple))]
        counts = [v for k, v in value.items() if k in _COUNT_KEYS and isinstance(v, (int, float))]
        if lists and all(len(v) == 0 for v in lists) and not any(counts and c > 0 for c in counts):
            return True
        if not lists and counts and all(c == 0 for c in counts):
            return True
    return False


def is_empty_result(result: Any) -> bool:
    """True for a successful call that found nothing; False for errors."""
    if isinstance(result, dict):
        if result.get("error") or result.get("approval_required") or result.get("blocked"):
            return False
        if result.get("exit_code") not in (0, None):
            return False
    try:
        from src.observations.capture import _result_text
        text = _result_text(result).strip()
    except Exception:  # noqa: BLE001
        text = result.strip() if isinstance(result, str) else ""
    if not text:
        return True
    if isinstance(result, dict):
        for key in _LIST_KEYS:
            if key in result and isinstance(result[key], (list, tuple)) and not result[key]:
                if not any(isinstance(v, (list, tuple)) and v for k, v in result.items() if k in _LIST_KEYS):
                    return True
    if text[:1] in "[{":
        try:
            return _empty_value(json.loads(text))
        except (ValueError, TypeError):
            pass
    return len(text) <= 160 and bool(_EMPTY_TEXT.match(text))


def normalize_query(query: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", str(query or "").lower()).split())


# -- languages ----------------------------------------------------------------

def data_languages(tool: Any) -> Tuple[List[str], str]:
    """(languages of the data behind this tool, name of the plugin or "")."""
    from src.plugins import DEFAULT_DATA_LANGUAGE
    default = list(DEFAULT_DATA_LANGUAGE)
    name = str(tool or "")
    if not name.startswith("mcp__"):
        return default, ""
    try:
        server_id = name.split("__", 2)[1]
        from src import connector_sidecar, plugins
        connector = connector_sidecar.get_connector_for_server(server_id)
        plugin = plugins.get(str((connector or {}).get("preset_id") or "")) if connector else None
        if plugin is not None:
            return list(plugin.data_language) or default, plugin.name
    except Exception:  # noqa: BLE001 - an unknown server just uses the default
        logger.debug("search_language: plugin lookup failed for %s", name, exc_info=True)
    return default, ""


def query_language(query: str, fallback: Optional[str] = None) -> Optional[str]:
    """The language of the query text; a bare name settles none, so the user's
    own language of the turn stands in for it."""
    try:
        from src.reply_language import language_of
        return language_of(query) or fallback
    except Exception:  # noqa: BLE001
        return fallback


def alias_equivalents(text: str, langs: Iterable[str]) -> List[str]:
    """Equivalent names of `text` in `langs`, from the alias service of Hoard Link.

    The service (Hoard Link task "Servicio de alias es<->en") is optional: when
    its module is not there this returns nothing and the hint goes out without
    names. Expected contract: ``equivalents(text, target_langs) -> list[str]``.
    """
    langs = [str(l) for l in langs]
    for module in ("hoard_link.aliases", "src.hoard_link.aliases"):
        try:
            mod = __import__(module, fromlist=["equivalents"])
            found = mod.equivalents(text, langs)
        except Exception:  # noqa: BLE001 - missing module, missing function, service down
            continue
        names = [str(x).strip() for x in (found or []) if str(x).strip() and str(x).strip().lower() != text.strip().lower()]
        if names:
            return list(dict.fromkeys(names))[:6]
    return []


# -- the turn -----------------------------------------------------------------

_TURNS: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
_LOCK = threading.Lock()


def _state(session_id: str, *, fresh: bool = False) -> Dict[str, Any]:
    with _LOCK:
        state = _TURNS.get(session_id)
        if state is None or fresh:
            state = {"lang": None, "empty": 0, "reported": 0, "streak": 0, "same": 0, "last_sig": None,
                     "hinted": set(), "cut": set(), "cut_pending": set()}
            _TURNS[session_id] = state
            while len(_TURNS) > MAX_TRACKED_TURNS:
                _TURNS.popitem(last=False)
        else:
            _TURNS.move_to_end(session_id)
        return state


def reset_for_tests() -> None:
    with _LOCK:
        _TURNS.clear()


def empty_searches(session_id: str) -> int:
    """Empty searches so far in the current turn of this chat."""
    with _LOCK:
        state = _TURNS.get(str(session_id or ""))
        return int(state["empty"]) if state else 0


def take_empty_event(session_id: str) -> int:
    """The turn's empty-search count when it changed since the last call, else 0."""
    with _LOCK:
        state = _TURNS.get(str(session_id or ""))
        if not state or state["empty"] == state["reported"]:
            return 0
        state["reported"] = state["empty"]
        return int(state["empty"])


def take_cut_tools(session_id: str) -> Set[str]:
    """Tools whose loop was just cut: the agent loop hides them for the rest of
    the turn. Each tool is returned once."""
    with _LOCK:
        state = _TURNS.get(str(session_id or ""))
        if not state:
            return set()
        tools, state["cut_pending"] = set(state["cut_pending"]), set()
        return tools


def _names(codes: Iterable[str], es: bool) -> str:
    return ", ".join(_LANGUAGE_NAMES.get(c, (c, c))[0 if es else 1] for c in codes)


def _and(codes: Iterable[str], es: bool) -> str:
    items = [_LANGUAGE_NAMES.get(c, (c, c))[0 if es else 1] for c in codes]
    if len(items) < 2:
        return "".join(items)
    return ", ".join(items[:-1]) + (" y " if es else " and ") + items[-1]


def hint_text(*, query: str, qlang: Optional[str], data_langs: List[str], plugin: str,
              others: List[str], aliases: List[str], user_lang: Optional[str]) -> str:
    es = user_lang != "en"
    where = f" de {plugin}" if (plugin and es) else (f" of {plugin}" if plugin else "")
    names = f" Equivalentes: {', '.join(aliases)}." if (aliases and es) else (
        f" Equivalent names: {', '.join(aliases)}." if aliases else "")
    if es:
        seen = f"la consulta parece estar en {_names([qlang], True)}" if qlang else "el idioma de la consulta no está claro"
        return (f"[Búsqueda vacía] «{query}» no devolvió nada. Los datos{where} están en {_and(data_langs, True)}; {seen}. "
                f"Repite UNA vez con el nombre en {_and(others, True)}.{names}")
    seen = f"the query looks like {_names([qlang], False)}" if qlang else "the query language is unclear"
    return (f"[Empty search] \"{query}\" returned nothing. The data{where} is in {_and(data_langs, False)}; {seen}. "
            f"Retry ONCE with the name in {_and(others, False)}.{names}")


def cut_text(*, query: str, same_query: bool, user_lang: Optional[str]) -> str:
    if user_lang != "en":
        why = "con la misma consulta" if same_query else "tras traducir la consulta"
        return (f"[Búsqueda vacía repetida] Dos búsquedas vacías seguidas {why}. No la repitas: la herramienta queda "
                f"oculta el resto del turno. Responde con lo que ya tienes, di que no hay resultados para «{query}» "
                "y qué probaste, o pide otro nombre al usuario.")
    why = "with the same query" if same_query else "after translating the query"
    return (f"[Repeated empty search] Two empty searches in a row {why}. Do not repeat it: the tool is hidden for the "
            f"rest of the turn. Answer with what you have, say there are no results for \"{query}\" and what you "
            "tried, or ask the user for another name.")


def on_turn_start(ctx: Dict[str, Any]) -> None:
    session_id = str(ctx.get("session_id") or "")
    if not session_id:
        return None
    state = _state(session_id, fresh=True)
    try:
        from src.reply_language import language_of
        state["lang"] = language_of(ctx.get("user_message"))
    except Exception:  # noqa: BLE001
        pass
    return None


def on_post_tool(ctx: Dict[str, Any]) -> Optional[str]:
    """Count an empty search and return the hint (or the cut) for the model."""
    try:
        return _after_tool(ctx)
    except Exception:  # noqa: BLE001 - a hint is never worth a turn
        logger.debug("search_language: post_tool failed", exc_info=True)
        return None


def _after_tool(ctx: Dict[str, Any]) -> Optional[str]:
    tool, content = ctx.get("tool"), ctx.get("content")
    session_id = str(ctx.get("session_id") or "")
    if not session_id or not is_data_search(tool, content):
        return None
    state = _state(session_id)
    if not is_empty_result(ctx.get("result")):
        state["streak"] = state["same"] = 0
        state["last_sig"] = None
        return None
    query = query_of(content)
    sig = (str(tool), normalize_query(query))
    state["empty"] += 1
    state["streak"] += 1
    state["same"] = state["same"] + 1 if sig == state["last_sig"] else 1
    state["last_sig"] = sig
    try:
        from src.observations import worker
        worker.count_stat("empty_searches")
    except Exception:  # noqa: BLE001
        pass

    user_lang = state.get("lang")
    if (str(tool) in state["hinted"] and state["streak"] >= 2) or state["same"] >= 2:
        state["cut"].add(str(tool))
        state["cut_pending"].add(str(tool))
        return cut_text(query=query or "…", same_query=state["same"] >= 2, user_lang=user_lang)
    if str(tool) in state["hinted"]:
        return None
    data_langs, plugin = data_languages(tool)
    qlang = query_language(query, user_lang) if query else user_lang
    others = [l for l in data_langs if l != qlang]
    if not others:
        return None
    state["hinted"].add(str(tool))
    aliases = alias_equivalents(query, others) if query else []
    return hint_text(query=query or "…", qlang=qlang, data_langs=data_langs, plugin=plugin,
                     others=others, aliases=aliases, user_lang=user_lang)


def install() -> None:
    """Register the listeners. Idempotent."""
    from src import lifecycle_hooks as hooks
    hooks.register_listener("turn_start", on_turn_start, listener_id=LISTENER_ID, name="Empty search language")
    hooks.register_listener("post_tool", on_post_tool, listener_id=LISTENER_ID, name="Empty search language")


def uninstall() -> None:
    from src import lifecycle_hooks as hooks
    hooks.unregister_listener(LISTENER_ID)
