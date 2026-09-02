"""Chat routes — /api/chat, /api/chat_stream, /api/inject_context, /api/search."""

import asyncio
import json
import os
import re
import time
import logging
from datetime import datetime
from typing import Dict, Any, AsyncGenerator, List, Optional

from fastapi import APIRouter, Request, HTTPException, Form, Query
from fastapi.responses import StreamingResponse
from pydantic import ValidationError

from core.models import ChatMessage
from src.request_models import ChatRequest
from src.llm_core import (
    _normalize_http_status,
    llm_call_async,
    llm_call_async_with_route_fallback,
    stream_llm,
    stream_llm_with_fallback,
)
from src.agent_loop import stream_agent_loop, _looks_like_workspace_coding_request
from src import agent_runs
from src.model_context import estimate_tokens
from src.context_compactor import (
    apply_compaction_state,
    maybe_compact,
    message_is_truncation_of,
    truncated_text_matches,
    trim_for_context,
)
from src.chat_helpers import coerce_message_and_session
from src.endpoint_resolver import normalize_base as _normalize_base, build_chat_url
from src.foreground_model_routing import (
    build_foreground_model_candidates,
    build_foreground_route_descriptors,
    resolve_foreground_model_policy,
)
from src.session_search import search_session_messages
from src.prompt_security import untrusted_context_message
from core.exceptions import SessionNotFoundError
from src.auth_helpers import effective_user, get_current_user
from routes.session_routes import _verify_session_owner
from routes.document_helpers import _owner_session_filter
from core.database import SessionLocal, get_session_mode, set_session_mode
from core.database import Session as DBSession, ChatMessage as DBChatMessage
from core.database import Document as DBDocument, ModelEndpoint
from core.log_safety import redact_url
from routes.research_routes import _resolve_research_endpoint
from routes.model_routes import _visible_models
from routes.chat_helpers import (
    resolve_session_auth,
    build_chat_context,
    save_assistant_response,
    run_post_response_tasks,
    accumulate_token_usage,
    clean_thinking_for_save,
    _allowed_models_for_request,
    _enforce_chat_privileges,
)
from src.action_intents import ToolIntent, classify_tool_intent as _classify_tool_intent
from src.image_model_ids import looks_like_image_generation_model
from src.tool_policy import (
    WEB_TOOL_NAMES,
    build_effective_tool_policy,
    is_web_search_explicitly_denied,
    web_search_enabled_for_turn,
)
from src.tool_approvals import tool_approval_store
from src.tool_capabilities import BROWSER_MCP_ALL_TOOLS, browser_tool_denials
from src.tool_utils import get_mcp_manager

logger = logging.getLogger(__name__)

# Track active streams for partial-save safety net
_active_streams: Dict[str, dict] = {}


def _stream_failure_status(chunk: str) -> Optional[int]:
    """Extract a provider status without retaining provider-supplied detail."""

    try:
        for line in str(chunk or "").splitlines():
            if not line.startswith("data: "):
                continue
            status = json.loads(line[6:]).get("status")
            return _normalize_http_status(status)
    except json.JSONDecodeError:
        return None
    return None


def _mark_tool_approval_resolved(sess, approval_id: Any, decision: Any) -> bool:
    """Persist a consumed approval decision on its existing tool event."""

    approval_key = str(approval_id or "")
    normalized_decision = str(decision or "").strip().lower()
    if not approval_key or normalized_decision not in {"approve", "approve_task", "deny"}:
        return False

    message_id = None
    resolved_metadata = None
    for item in reversed(getattr(sess, "history", []) or []):
        metadata = getattr(item, "metadata", None)
        if not isinstance(metadata, dict):
            continue
        tool_events = metadata.get("tool_events")
        if not isinstance(tool_events, list):
            continue
        for event in reversed(tool_events):
            ask_user = event.get("ask_user") if isinstance(event, dict) else None
            if not isinstance(ask_user, dict):
                continue
            if str(ask_user.get("approval_id") or "") != approval_key:
                continue
            ask_user["resolved"] = normalized_decision
            message_id = metadata.get("_db_id")
            resolved_metadata = {
                key: value for key, value in metadata.items() if key != "_db_id"
            }
            break
        if resolved_metadata is not None:
            break

    if resolved_metadata is None or not message_id:
        return False

    db = SessionLocal()
    try:
        db_message = db.query(DBChatMessage).filter(
            DBChatMessage.id == message_id,
            DBChatMessage.session_id == str(getattr(sess, "id", "")),
        ).first()
        if db_message is None:
            return False
        db_message.meta_data = json.dumps(resolved_metadata)
        db.commit()
        return True
    except Exception:
        db.rollback()
        logger.exception("Failed to persist tool approval resolution")
        return False
    finally:
        db.close()


async def _tool_approval_resolution_stream(decision: str) -> AsyncGenerator[str, None]:
    yield f"data: {json.dumps({'type': 'tool_approval_resolved', 'decision': decision})}\n\n"
    yield "data: [DONE]\n\n"


def _chat_candidate_request_factory(
    messages,
    fallback_context_length: int = 0,
    *,
    session=None,
    owner: Optional[str] = None,
):
    """Shape one route-neutral Chat prompt for each candidate window."""

    state = {
        "requests": {},
        "context_lengths": {},
        "trim_stats": {},
        "compactions": {},
        "was_compacted": {},
    }

    async def factory(index, candidate_url, candidate_model, candidate_headers):
        compaction_state = {}
        candidate_messages, context_length, was_compacted = await maybe_compact(
            session,
            candidate_url,
            candidate_model,
            list(messages),
            candidate_headers,
            owner=owner,
            persist=False,
            compaction_state=compaction_state,
        )
        if not context_length:
            context_length = fallback_context_length
        request_messages = trim_for_context(candidate_messages, context_length)
        state["requests"][index] = request_messages
        state["context_lengths"][index] = context_length
        state["compactions"][index] = compaction_state
        state["was_compacted"][index] = was_compacted
        state["trim_stats"][index] = {
            "messages_before": len(messages),
            "messages_after": len(request_messages),
            "tokens_before": estimate_tokens(messages),
            "tokens_after": estimate_tokens(request_messages),
        }
        return {"messages": request_messages}

    return factory, state


def _candidate_index(candidates, actual_candidate) -> int:
    for index, candidate in enumerate(candidates):
        if candidate == actual_candidate:
            return index
    return 0


def _stream_set(session_id: str, **fields) -> None:
    """Update fields on the active-stream entry for `session_id`, or
    no-op if the entry has already been popped. Using .get() avoids a
    KeyError race between `if x in d` and `d[x]["k"] = v` if a sibling
    finally pops the key in between (which becomes possible the moment
    a coroutine cancellation reaches an inner cleanup before the
    outermost cleanup runs)."""
    rec = _active_streams.get(session_id)
    if rec is None:
        return
    rec.update(fields)


def _message_plain_text(content: Any) -> str:
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(block, str):
                parts.append(block)
        return " ".join(parts)
    return str(content or "")


def _last_user_message(messages: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    for msg in reversed(messages or []):
        if msg.get("role") == "user":
            return msg
    return None


def _last_user_plain_text(messages: List[Dict[str, Any]]) -> str:
    latest = _last_user_message(messages)
    return _message_plain_text(latest.get("content")) if latest else ""


def _ensure_current_request_is_latest_user(
    messages: List[Dict[str, Any]],
    current_message: str,
    context_length: int = 0,
) -> List[Dict[str, Any]]:
    """Defensively keep detached streams grounded on the request that created them.

    The trimmer can hand the current turn back *shortened*, with a notice
    spliced into the middle of it. That is still the same message, but no
    substring comparison can see it: ``latest == current``, ``current in
    latest`` and ``latest in current`` all fail, and the old repair pasted the
    whole original message back — duplicating it and pushing the prompt far
    past the context window it had just been trimmed to. So recognise a
    shortened rendering by the marker the trimmer stamps on it (identity, not
    text), and if a repair really is needed, re-trim afterwards: appending must
    never leave the prompt over budget.
    """
    current = str(current_message or "").strip()
    if not current:
        return messages
    latest_msg = _last_user_message(messages)
    latest = _message_plain_text(latest_msg.get("content")).strip() if latest_msg else ""
    if latest == current or current in latest or latest in current:
        return messages
    if message_is_truncation_of(latest_msg, current_message) or truncated_text_matches(latest, current):
        return messages
    logger.warning(
        "[chat_stream] latest user context mismatch; appending current request for model call. latest=%r current=%r",
        latest[:120],
        current[:120],
    )
    repaired = list(messages or [])
    repaired.append({"role": "user", "content": current})
    if context_length:
        repaired = trim_for_context(repaired, context_length)
    return repaired


_WEB_FOLLOWUP_RE = re.compile(
    r"^\s*(?:(?:can|could|would|will)\s+you\s+)?"
    r"(?:check|try\s+again|look(?:\s+now|\s+it\s+up)?|search(?:\s+now|\s+online|\s+it)?|"
    r"do\s+it|again|approved|approve(?:d)?|yes|ok(?:ay)?|proceed|go\s+ahead|"
    r"send(?:\s+it)?|submit(?:\s+it)?|email(?:\s+them|\s+it)?)\??\s*$",
    re.I,
)
_RECENT_WEB_CONTEXT_RE = re.compile(
    r"\b(?:weather|forecast|rain|raining|hourly|news|headlines|rate|exchange|currency|"
    r"price|current|latest|search|look\s+up|online)\b",
    re.I,
)
# Browser wording, English + Spanish. Every alternative is a whole word or a
# fixed phrase (\b on both sides) so "navega" never fires inside "navegar"
# or "navegación", "clic" never inside "clicked", "pestaña" never inside
# "pestañear". Bare nouns that coding requests use figuratively — "captura"
# (catch an exception), "formulario" (a form component) — only count inside
# the browser phrase ("captura de pantalla", "rellena el formulario",
# "formulario de contacto"); a rule that classified those as browsing would
# strip file tools from a plain code request.
_BROWSER_INTENT_WORDS = (
    # English
    r"browser|browse|open\s+(?:the\s+)?(?:site|page|url|link)|click|"
    r"fill(?:\s+out)?|submit|send\s+(?:the\s+)?form|contact\s+form|web\s*form|"
    r"form\s+submission|"
    # Spanish
    r"navegador|navega|"
    r"abre\s+(?:la\s+|el\s+|una\s+|un\s+)?(?:web|p[aá]gina|url|enlace|sitio)|"
    r"captura\s+de\s+pantalla|pantallazo|"
    r"pincha|pulsa|haz\s+clic|clic|"
    r"rellena(?:r)?|"
    r"formulario\s+(?:de\s+contacto|web)|env[ií]a(?:r)?\s+el\s+formulario|"
    r"pesta[nñ]as?|despl[aá]zate|haz\s+scroll|scroll\s+(?:down|up|hacia)|"
    r"inicia(?:r)?\s+sesi[oó]n"
)
_EXPLICIT_BROWSER_INTENT_RE = re.compile(r"\b(?:" + _BROWSER_INTENT_WORDS + r")\b", re.I)
_RECENT_BROWSER_CONTEXT_RE = re.compile(
    r"\b(?:" + _BROWSER_INTENT_WORDS + r"|playwright|automation)\b",
    re.I,
)


def _explicit_browser_intent_for_message(message) -> bool:
    """Whether one user message explicitly asks for the browser (EN/ES)."""
    if not isinstance(message, str) or not message:
        return False
    return bool(_EXPLICIT_BROWSER_INTENT_RE.search(message.lower()))


# Every tool the built-in Playwright server can expose (static, 30 names as of
# @playwright/mcp 0.0.80). Used to FORCE the browser set into a turn on
# explicit intent and, via `_browser_mcp_denylist`, to withhold it. A
# hand-picked subset here silently left 18 tools (evaluate, run_code_unsafe,
# console/network, hover, tabs, resize, the mouse_*_xy set…) outside
# `can_use_browser=False`.
_BROWSER_MCP_TOOLS = set(BROWSER_MCP_ALL_TOOLS)


def _browser_mcp_denylist() -> set:
    """Qualified names to deny when the browser is off for this request:
    the static set plus whatever the connected server actually exposes, so a
    tool renamed or added in a newer Playwright release is covered too."""
    live = set()
    try:
        mgr = get_mcp_manager()
        if mgr is not None and hasattr(mgr, "browser_tool_names"):
            live = set(mgr.browser_tool_names() or ())
    except Exception:
        live = set()
    return set(browser_tool_denials({"builtin_browser"}, live_tool_names=live))


def _recent_session_text(sess, limit: int = 8, max_chars: int = 2000) -> str:
    history = getattr(sess, "history", None) or getattr(sess, "_history", None) or []
    chunks: List[str] = []
    for msg in history[-limit:]:
        content = getattr(msg, "content", None)
        if content is None and isinstance(msg, dict):
            content = msg.get("content")
        text = _message_plain_text(content).strip()
        if text:
            chunks.append(text)
    return " ".join(chunks)[-max_chars:]


def _is_contextual_web_followup(message: str, sess) -> bool:
    """Treat short retry/check replies as web lookups when recent context was web."""
    if not message or not _WEB_FOLLOWUP_RE.search(message):
        return False
    return bool(_RECENT_WEB_CONTEXT_RE.search(_recent_session_text(sess)))


def _is_contextual_browser_followup(message: str, sess) -> bool:
    """Treat short retry replies as browser tasks when recent context was forms/browser automation."""
    if not message or not _WEB_FOLLOWUP_RE.search(message):
        return False
    return bool(_RECENT_BROWSER_CONTEXT_RE.search(_recent_session_text(sess, limit=12, max_chars=4000)))


def _resolve_request_workspace(request, raw_value) -> tuple:
    """Resolve the posted workspace for this request: (workspace, rejected).

    Privilege is checked BEFORE the path ever touches the filesystem. Only
    admin/single-user callers can use the workspace-backed file/shell tools,
    so only they get vet_workspace() and the workspace_rejected signal. For
    any other caller the submitted value is dropped uniformly, with no vetting
    and no event: otherwise the presence/absence of workspace_rejected would
    let a non-admin chat caller probe which host paths exist.

    vet_workspace rejects non-directories, sensitive roots (.ssh, .gnupg,
    ...), and filesystem roots; on rejection there is no confinement and the
    default tool-path allowlist applies. The rejected value is surfaced so the
    stream can tell an admin client (which believes a workspace is active)
    that it was dropped.
    """
    requested = (raw_value or "").strip()
    if not requested:
        return "", ""
    from src.tool_security import owner_is_admin_or_single_user
    if not owner_is_admin_or_single_user(get_current_user(request)):
        return "", ""
    from src.tool_execution import vet_workspace
    workspace = vet_workspace(requested) or ""
    return workspace, (requested if not workspace else "")


def _parse_delegate_tasks(raw) -> Optional[Dict[str, Any]]:
    """`delegate_tasks` form field from the /agents slash command: a JSON object
    {"tasks": [{"name", "instruction", "files"?, "model"?}...], "parallel": bool,
    "reviewer"?, "reviewer_model"?, "max_rounds"?, "timeout_s"?}. Returns None
    when absent or malformed (the request then behaves as a normal message).

    Every field the delegate_agents tool understands is kept: this used to
    drop files/model/reviewer/max_rounds/timeout_s, so `/agents --review` and
    the per-task file ownership never reached the tool."""
    if not raw:
        return None
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    tasks = data.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        return None

    def _bool(v, default):
        if v is None:
            return default
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return bool(v)
        s = str(v).strip().lower()
        return default if s not in ("1", "true", "yes", "on", "0", "false", "no", "off", "") \
            else s in ("1", "true", "yes", "on")

    clean = []
    for t in tasks[:4]:
        if isinstance(t, dict) and str(t.get("instruction") or "").strip():
            task = {
                "name": str(t.get("name") or t["instruction"])[:60],
                "instruction": str(t["instruction"]).strip()[:4000],
            }
            files = t.get("files") or t.get("owns") or []
            if isinstance(files, str):
                files = [p for p in re.split(r"[,\s]+", files) if p]
            if isinstance(files, list):
                files = [str(p).strip() for p in files if str(p).strip()][:40]
                if files:
                    task["files"] = files
            model = str(t.get("model") or "").strip()
            if model:
                task["model"] = model[:120]
            clean.append(task)
        elif isinstance(t, str) and t.strip():
            clean.append({"name": t.strip()[:60], "instruction": t.strip()[:4000]})
    if not clean:
        return None
    out: Dict[str, Any] = {"tasks": clean, "parallel": _bool(data.get("parallel"), True)}
    reviewer = data.get("reviewer", data.get("review"))
    if reviewer is not None:
        out["reviewer"] = _bool(reviewer, False)
    reviewer_model = str(data.get("reviewer_model") or "").strip()
    if reviewer_model:
        out["reviewer_model"] = reviewer_model[:120]
    for key in ("max_rounds", "timeout_s"):
        try:
            val = int(data.get(key)) if data.get(key) not in (None, "") else None
        except (TypeError, ValueError):
            val = None
        if val is not None and val > 0:
            out[key] = val
    return out


def _delegation_instruction(payload: Dict[str, Any]) -> str:
    """Model-facing text for a /agents send. The persisted user message stays
    the compact line the user saw; only the model gets this."""
    return (
        "Delegate this work to parallel sub-agents by calling the delegate_agents tool "
        "EXACTLY ONCE with these arguments (do not do the tasks yourself, do not rewrite them):\n"
        + json.dumps(payload, ensure_ascii=False)
        + "\nWhen the tool returns, report to the user only what its evidence says: which files "
        "each worker changed, failures, and what still needs doing."
    )


def _parse_gen_overrides(raw) -> Dict[str, Any]:
    """Validate the per-session generation overrides sent by the chat UI.

    Accepts a JSON string (FormData) or a dict (JSON body). Unknown keys and
    out-of-range values are dropped silently; a malformed payload yields {}.
    """
    if not raw:
        return {}
    data = raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
    if not isinstance(data, dict):
        return {}
    out: Dict[str, Any] = {}
    try:
        if data.get("temperature") not in (None, ""):
            t = float(data["temperature"])
            if 0.0 <= t <= 2.0:
                out["temperature"] = t
        if data.get("max_tokens") not in (None, ""):
            mt = int(data["max_tokens"])
            if 0 <= mt <= 262144:
                out["max_tokens"] = mt
        if data.get("top_p") not in (None, ""):
            tp = float(data["top_p"])
            if 0.0 < tp <= 1.0:
                out["top_p"] = tp
        if data.get("top_k") not in (None, ""):
            tk = int(data["top_k"])
            if 0 <= tk <= 1000:
                out["top_k"] = tk
        if data.get("num_ctx") not in (None, ""):
            nc = int(data["num_ctx"])
            if 512 <= nc <= 1048576:
                out["num_ctx"] = nc
        # Layers to keep on the GPU. 0 is meaningful (run it all on the CPU),
        # and the fit advisor is the thing that normally sets it.
        if data.get("num_gpu") not in (None, ""):
            ng = int(data["num_gpu"])
            if 0 <= ng <= 1024:
                out["num_gpu"] = ng
        if data.get("seed") not in (None, ""):
            out["seed"] = int(data["seed"])
        if data.get("repeat_penalty") not in (None, ""):
            rp = float(data["repeat_penalty"])
            if 0.5 <= rp <= 2.0:
                out["repeat_penalty"] = rp
        if "think" in data and data.get("think") not in (None, ""):
            v = data["think"]
            out["think"] = bool(v) if not isinstance(v, str) else v.strip().lower() in ("1", "true", "on", "yes")
        if data.get("reasoning_effort") in ("low", "medium", "high", "none"):
            out["reasoning_effort"] = data["reasoning_effort"]
    except (TypeError, ValueError):
        pass
    return out


def _project_workspace(request, session_id) -> str:
    """The workspace bound by this chat's project, if it has one.

    A project's folder beats whatever the browser posted. The workspace pill is
    global localStorage state, so before projects existed, switching chats left
    the agent confined to the previous folder until you remembered to change it
    by hand. Resolving it from the session server-side makes confinement follow
    the conversation instead of the tab.

    Gated exactly like the posted value: a caller who may not use the
    workspace-backed tools gets nothing here either.
    """
    session_id = str(session_id or "").strip()
    if not session_id:
        return ""
    from src.tool_security import owner_is_admin_or_single_user
    owner = get_current_user(request)
    if not owner_is_admin_or_single_user(owner):
        return ""
    from services.projects import workspace_for_session
    from src.tool_execution import vet_workspace
    # Re-vet at use time rather than trusting the stored path: it was vetted
    # when the project was saved, but the folder can be deleted, or swapped for
    # a symlink, at any point afterwards.
    return vet_workspace(workspace_for_session(session_id, owner)) or ""


def _project_harness_options(request, session_id, workspace: str) -> Dict[str, Any]:
    """`harness_options` for stream_agent_loop from the chat's project (see
    services.projects.AGENT_OPTION_FIELDS). Never raises; no project → {}."""
    session_id = str(session_id or "").strip()
    out: Dict[str, Any] = {}
    if not session_id:
        return out
    try:
        from src.tool_security import owner_is_admin_or_single_user
        owner = get_current_user(request)
        if not owner_is_admin_or_single_user(owner):
            return out
        from services.projects import agent_options, project_for_session
        project = project_for_session(session_id, owner)
        opts = agent_options(project)
        if not opts:
            return out
        out = {
            "project_id": opts.get("project_id"),
            "checkpoints": opts.get("checkpoints", True),
            "run_tests": opts.get("run_tests", True),
            "review_mode": bool(opts.get("review_mode")),
            "trusted_agents": bool(opts.get("trusted_agents")),
        }
        if opts.get("test_command"):
            out["test_command"] = opts["test_command"]
        if opts.get("review_model"):
            out["review_model"] = opts["review_model"]
        # The trusted folder is the project's vetted workspace, and only when
        # this turn actually runs inside it.
        if opts.get("trusted") and workspace and (project or {}).get("workspace"):
            import os as _os
            if _os.path.realpath(workspace) == _os.path.realpath(project["workspace"]):
                out["trusted_workspace"] = _os.path.realpath(workspace)
    except Exception as e:  # noqa: BLE001 - hot path
        logger.debug("harness options unavailable for %s: %s", session_id, e)
    return out


def _record_turn_side_effects(session_id: str, message_id: Any, metrics: Dict[str, Any],
                              user_text: str, harness_options: Dict[str, Any]) -> None:
    """After an assistant message is saved: append the turn to the project's
    audit trail (src/project_audit.py) and, in review mode, register its files
    as pending (services/review_state.py). Only turns that changed files."""
    hz = (metrics or {}).get("harness") if isinstance(metrics, dict) else None
    if not isinstance(hz, dict):
        return
    files = [str(f) for f in (hz.get("mutations") or []) if f]
    if not files:
        return
    workspace = str(hz.get("workspace") or "")
    project_id = str(harness_options.get("project_id") or hz.get("project_id") or "")
    tests = hz.get("tests") if isinstance(hz.get("tests"), dict) else None
    review = hz.get("review") if isinstance(hz.get("review"), dict) else None
    try:
        from src import project_audit
        key = project_id or (project_audit.workspace_key(workspace) if workspace else "")
        if key:
            project_audit.record(
                key, session_id=session_id, message_id=message_id, model=str(metrics.get("model") or ""),
                files=files, workspace=workspace or None, stop_reason=hz.get("stop_reason"),
                checkpoint=hz.get("checkpoint"), user_text=user_text or "",
                tests=("inconclusive" if tests.get("inconclusive") else ("pass" if tests.get("ok") else "fail")) if tests and tests.get("ran") else None,
                review=review.get("verdict") if review else None,
                project_id=project_id or None,
            )
    except Exception as e:  # noqa: BLE001
        logger.debug("audit record failed: %s", e)
    if hz.get("review_mode") and workspace:
        try:
            from services import review_state
            review_state.init(message_id, session_id=session_id, workspace=workspace, files=files,
                              checkpoint=hz.get("checkpoint"))
        except Exception as e:  # noqa: BLE001
            logger.debug("review state init failed: %s", e)


def _project_work_roots(request, session_id) -> list[str]:
    """All current file/folder roots attached to this chat's project."""
    session_id = str(session_id or "").strip()
    if not session_id:
        return []
    from src.tool_security import owner_is_admin_or_single_user
    owner = get_current_user(request)
    if not owner_is_admin_or_single_user(owner):
        return []
    from services.projects import work_roots_for_session
    return work_roots_for_session(session_id, owner)


_ABS_PATH_RE = re.compile(r"(?<!\S)(~?/[^\"'\s`<>]+)")
_LOCAL_FILE_TASK_RE = re.compile(
    r"\b(?:file|folder|directory|path|workspace|repo|project|movie|video|"
    r"subtitle|subtitles|srt|vtt|ass|download|save|rename|move|copy|extract|"
    r"convert|ffmpeg|run|execute|open|read|inspect|fix|debug|test|build)\b",
    re.IGNORECASE,
)


def _resolve_workspace_from_message_path(request, message: str) -> tuple[str, str]:
    """Auto-bind a workspace only when the user names an explicit safe path.

    This is intentionally deterministic rather than LLM/RAG-driven: RAG can
    choose the tool family, but filesystem binding must not let a prompt infer
    or probe arbitrary host paths. For a file path, bind its parent directory.
    For a directory path, bind that directory.
    """
    text = str(message or "")
    if not text or not _LOCAL_FILE_TASK_RE.search(text):
        return "", ""

    from src.tool_security import owner_is_admin_or_single_user
    if not owner_is_admin_or_single_user(get_current_user(request)):
        return "", ""

    from src.tool_execution import vet_workspace

    for match in _ABS_PATH_RE.finditer(text):
        raw = match.group(1).rstrip(".,;:)]}")
        expanded = os.path.realpath(os.path.expanduser(raw))
        candidates = [expanded]
        if os.path.isfile(expanded):
            candidates.insert(0, os.path.dirname(expanded))
        for candidate in candidates:
            workspace = vet_workspace(candidate) or ""
            if workspace:
                return workspace, ""
    return "", ""


def _session_url_matches_endpoint(session_url: str, endpoint_base: str) -> bool:
    if not session_url or not endpoint_base:
        return False
    sess = session_url.rstrip("/")
    base = _normalize_base(endpoint_base).rstrip("/")
    variants = {
        base,
        base + "/chat/completions",
        build_chat_url(base).rstrip("/"),
    }
    return sess in variants or sess.startswith(base + "/")


def _clear_orphaned_session_endpoint(sess, owner: str | None = None) -> bool:
    """Clear a session model if its endpoint was deleted from ModelEndpoint."""
    if not getattr(sess, "endpoint_url", ""):
        return False
    db = SessionLocal()
    try:
        q = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True)
        if owner:
            from src.auth_helpers import owner_filter
            q = owner_filter(q, ModelEndpoint, owner)
        endpoints = q.all()
        for ep in endpoints:
            if _session_url_matches_endpoint(sess.endpoint_url or "", ep.base_url or ""):
                return False
        db_session = db.query(DBSession).filter(DBSession.id == sess.id).first()
        if db_session:
            db_session.endpoint_url = ""
            db_session.model = ""
            db_session.updated_at = datetime.utcnow()
            db.commit()
        sess.endpoint_url = ""
        sess.model = ""
        sess.headers = {}
        return True
    except Exception as e:
        logger.warning("Failed to clear orphaned session endpoint", exc_info=e)
        db.rollback()
        return False
    finally:
        db.close()


def _endpoint_cache_contains_model(endpoint, model: str) -> bool:
    """Return True when a populated endpoint model cache includes ``model``.

    Empty/malformed caches are treated as unknown rather than a negative match
    so older image endpoints without cached models still work.
    """
    raw = getattr(endpoint, "cached_models", None)
    if not raw:
        return True
    try:
        models = json.loads(raw) if isinstance(raw, str) else raw
    except Exception as e:
        logger.warning("Failed to parse cached models list, treating as containing model", exc_info=e)
        return True
    if not isinstance(models, list) or not models:
        return True
    wanted = (model or "").strip()
    return wanted in {str(item).strip() for item in models}


def _is_image_generation_session(sess, owner: str | None = None) -> bool:
    """Whether this chat session should bypass text chat and generate images.

    Model-name prefixes are explicit image models. Endpoint type is only used
    when the current session endpoint actually matches that image endpoint, and
    when a populated endpoint model cache includes the selected model. This
    prevents an image endpoint on the same host from misrouting ordinary text
    models into the image-generation path.
    """
    model = (getattr(sess, "model", "") or "").strip()
    if looks_like_image_generation_model(model):
        return True

    endpoint_url = (getattr(sess, "endpoint_url", "") or "").strip()
    if not endpoint_url:
        return False

    db = SessionLocal()
    try:
        q = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True)
        if owner:
            from src.auth_helpers import owner_filter
            q = owner_filter(q, ModelEndpoint, owner)
        endpoints = q.all()
        for endpoint in endpoints:
            if (getattr(endpoint, "model_type", None) or "llm") != "image":
                continue
            if not _session_url_matches_endpoint(endpoint_url, getattr(endpoint, "base_url", "") or ""):
                continue
            if _endpoint_cache_contains_model(endpoint, model):
                return True
    except Exception:
        return False
    finally:
        db.close()
    return False


def _first_image_attachment(chat_handler, att_ids: List[str], owner: str | None = None) -> Optional[Dict[str, Any]]:
    """Return the first attached image file that this owner can read."""
    upload_handler = getattr(chat_handler, "upload_handler", None)
    if not upload_handler:
        return None
    for att_id in att_ids or []:
        try:
            info = upload_handler.resolve_upload(att_id, owner=owner)
        except Exception as e:
            logger.warning("Failed to resolve image edit upload %s", att_id, exc_info=e)
            continue
        if not info:
            continue
        name = info.get("name") or info.get("original_name") or info.get("id") or ""
        mime = info.get("mime", "")
        try:
            if upload_handler.is_image_file(name, mime):
                return info
        except Exception:
            continue
    return None


def _recover_empty_session_model(sess, session_id: str, owner: str | None = None) -> bool:
    """Re-populate sess.model from the matching endpoint's cached models.

    Covers the window between endpoint setup and the first chat send: the
    picker showed a model in the dropdown but the session record never got
    written (Issue #587 — UI uses the cached endpoint list, not s.model).
    For ChatGPT Subscription, also repairs stale OpenAI API model names such as
    ``gpt-5`` that are not accepted by the Codex-backed ChatGPT account route.
    """
    current_model = (getattr(sess, "model", "") or "").strip()
    endpoint_url = (getattr(sess, "endpoint_url", "") or "").strip()
    is_chatgpt_subscription = False
    if current_model:
        try:
            from src.chatgpt_subscription import is_chatgpt_subscription_base
            is_chatgpt_subscription = is_chatgpt_subscription_base(endpoint_url)
            if not is_chatgpt_subscription:
                return False
        except Exception:
            return False
    db = SessionLocal()
    try:
        # Prefer the endpoint whose base URL matches the session — we know the
        # user already pointed this session at that endpoint, so its first
        # cached model is the most defensible default.
        ep = None
        if getattr(sess, "endpoint_url", ""):
            q = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True)
            if owner:
                from src.auth_helpers import owner_filter
                q = owner_filter(q, ModelEndpoint, owner)
            endpoints = q.all()
            for cand in endpoints:
                if _session_url_matches_endpoint(sess.endpoint_url or "", cand.base_url or ""):
                    ep = cand
                    break
        if not ep:
            return False
        if not is_chatgpt_subscription:
            try:
                from src.chatgpt_subscription import is_chatgpt_subscription_base
                is_chatgpt_subscription = is_chatgpt_subscription_base(getattr(ep, "base_url", "") or endpoint_url)
            except Exception:
                is_chatgpt_subscription = False
        try:
            cached = json.loads(ep.cached_models) if isinstance(ep.cached_models, str) else (ep.cached_models or [])
        except Exception as e:
            logger.warning("Failed to parse cached_models for endpoint %r", getattr(ep, "id", "?"), exc_info=e)
            cached = []
        if not cached:
            visible = []
        else:
            try:
                visible = _visible_models(cached, getattr(ep, "hidden_models", None))
            except Exception:
                visible = cached
        if current_model and current_model in {str(item).strip() for item in visible}:
            return False
        if is_chatgpt_subscription:
            live_models = []
            if getattr(ep, "provider_auth_id", None):
                try:
                    from src.chatgpt_subscription import fetch_available_models
                    from src.endpoint_resolver import resolve_endpoint_runtime
                    _base, api_key = resolve_endpoint_runtime(ep, owner=owner)
                    if api_key:
                        live_models = fetch_available_models(api_key)
                        if live_models:
                            ep.cached_models = json.dumps(live_models)
                            db.commit()
                except Exception:
                    live_models = []
            # ChatGPT Subscription recovery must use the live Codex catalog.
            # Cached rows are only trusted above to avoid revalidating a model
            # that is already present in the visible picker list.
            cached = live_models
            if not cached:
                return False
            try:
                visible = _visible_models(cached, getattr(ep, "hidden_models", None))
            except Exception:
                visible = cached
            if current_model and current_model in {str(item).strip() for item in visible}:
                return False
        if not visible:
            return False
        model = visible[0]
        if not isinstance(model, str) or not model.strip():
            return False
        model = model.strip()
        # Persist so the next request, websocket reconnect, or page reload
        # picks up the same model (we'd otherwise re-pick on every send
        # and silently switch on the user if the cached order shifts).
        db_session_q = db.query(DBSession).filter(DBSession.id == session_id)
        if owner:
            db_session_q = db_session_q.filter(DBSession.owner == owner)
        db_session = db_session_q.first()
        if db_session:
            db_session.model = model
            db_session.updated_at = datetime.utcnow()
            db.commit()
        sess.model = model
        logger.info(
            "Recovered session model for %s — picked %r from endpoint %s",
            session_id, model, ep.id,
        )
        return True
    except Exception as e:
        db.rollback()
        logger.warning("Failed to recover empty session model for %s: %s", session_id, e)
    return False


def _reconcile_selected_route_from_request(
    request: Request,
    sess,
    session_id: str,
    form_data,
    owner: str | None = None,
) -> bool:
    """Apply the model route the browser selected before streaming.

    The frontend creates a pending chat first and only materializes it on first
    send. Startup/default-model refreshes can race with that UI state, so the
    stream request includes the route that was selected at click/send time.
    Trust only registered endpoint ids, or the session's existing endpoint URL.
    """
    selected_model = str(form_data.get("selected_model") or "").strip()
    selected_endpoint_id = str(form_data.get("selected_endpoint_id") or "").strip()
    selected_endpoint_url = str(form_data.get("selected_endpoint_url") or "").strip()
    if not selected_model:
        return False

    endpoint_url = ""
    headers = None
    if selected_endpoint_id or selected_endpoint_url:
        try:
            from src.auth_helpers import owner_filter
            from src.endpoint_resolver import build_headers, normalize_base
            db = SessionLocal()
            try:
                q = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True)
                if selected_endpoint_id:
                    q = q.filter(ModelEndpoint.id == selected_endpoint_id)
                if owner:
                    q = owner_filter(q, ModelEndpoint, owner)
                candidates = q.all() if selected_endpoint_url and not selected_endpoint_id else [q.first()]
                ep = None
                for cand in candidates:
                    if not cand:
                        continue
                    if selected_endpoint_id or _session_url_matches_endpoint(selected_endpoint_url, cand.base_url or ""):
                        ep = cand
                        break
                if not ep:
                    return False
                endpoint_url = build_chat_url(normalize_base(ep.base_url or ""))
                headers = build_headers(ep.api_key or "", ep.base_url or "") if ep.api_key else {}
            finally:
                db.close()
        except Exception as e:
            logger.warning("Failed to resolve selected endpoint %s/%s for %s: %s", selected_endpoint_id, selected_endpoint_url, session_id, e)
            return False

    if not endpoint_url:
        return False

    if (
        selected_model == (getattr(sess, "model", "") or "")
        and endpoint_url == (getattr(sess, "endpoint_url", "") or "")
    ):
        return False

    sess.model = selected_model
    sess.endpoint_url = endpoint_url
    sess.headers = headers or {}
    db = SessionLocal()
    try:
        db_session = db.query(DBSession).filter(DBSession.id == session_id).first()
        if db_session:
            db_session.model = selected_model
            db_session.endpoint_url = endpoint_url
            db_session.headers = sess.headers or {}
            db_session.updated_at = datetime.utcnow()
            db.commit()
    finally:
        db.close()
    logger.info("Reconciled selected route for %s: model=%r endpoint=%s", session_id, selected_model, redact_url(endpoint_url))
    return True


def _set_user_time_from_request(request: Request) -> None:
    """Copy browser timezone headers into the per-request context.

    This is intentionally ephemeral: it is used only while building prompts
    and running tools for this request. It is not persisted or logged.
    """
    try:
        tz_offset = request.headers.get("x-tz-offset")
        tz_name = request.headers.get("x-tz-name")
        from src.user_time import clear_user_time_context, set_user_tz_name, set_user_tz_offset

        clear_user_time_context()
        if tz_offset is not None:
            set_user_tz_offset(tz_offset)
        if tz_name:
            set_user_tz_name(tz_name)
    except Exception:
        pass


def setup_chat_routes(
    session_manager,
    chat_handler,
    chat_processor,
    memory_manager,
    research_handler,
    upload_handler,
    memory_vector=None,
    webhook_manager=None,
    skills_manager=None,
) -> APIRouter:
    router = APIRouter(tags=["chat"])

    # ------------------------------------------------------------------ #
    # POST /api/chat (non-streaming)
    # ------------------------------------------------------------------ #
    @router.post("/api/chat", response_model=Dict[str, Any])
    async def chat_endpoint(request: Request, chat_request: ChatRequest) -> Dict[str, Any]:
        _set_user_time_from_request(request)

        message = chat_request.message
        session = chat_request.session
        att_ids = chat_request.attachments or []
        use_web = chat_request.use_web
        use_research = chat_request.use_research
        time_filter = chat_request.time_filter
        preset_id = chat_request.preset_id

        # Verify the caller owns this session before loading it.
        # Without this, any authenticated user can post into another user's chat.
        _verify_session_owner(request, session)

        try:
            sess = session_manager.get_session(session)
        except KeyError:
            raise HTTPException(404, f"Session '{session}' not found")
        owner = effective_user(request)
        if _clear_orphaned_session_endpoint(sess, owner=owner):
            raise HTTPException(400, "Selected model endpoint was removed. Pick another model in Settings.")

        # Empty model + live endpoint = setup race (Issue #587). Repair from
        # the endpoint's cached model list before privilege checks, which
        # otherwise see "" and behave inconsistently with the allowlist.
        _recover_empty_session_model(sess, session, owner=owner)
        if not getattr(sess, "model", "").strip():
            raise HTTPException(
                400,
                "No model selected for this chat. Open the model picker and choose one before sending.",
            )
        if not (getattr(sess, "endpoint_url", "") or "").strip():
            raise HTTPException(400, "Selected model endpoint is not configured")

        # Same allowed_models + daily-cap gate as chat_stream (mirror so the
        # non-streaming path can't be used to bypass).
        _enforce_chat_privileges(request, sess)

        tool_policy = build_effective_tool_policy(last_user_message=message)
        allow_tool_preprocessing = not tool_policy.block_all_tool_calls

        # Inline memory command
        memory_response = None
        if not tool_policy.blocks("manage_memory"):
            memory_response = await chat_handler.handle_memory_command(sess, message)
        if memory_response:
            return {"response": memory_response}

        foreground_policy = resolve_foreground_model_policy(
            owner=owner,
            allowed_models=_allowed_models_for_request(request),
        )

        # Build shared context (preset, preprocess, preface, compact)
        ctx = await build_chat_context(
            sess, request, chat_handler, chat_processor,
            message=message,
            session_id=session,
            preset_id=preset_id,
            att_ids=att_ids,
            use_web=use_web,
            time_filter=time_filter,
            webhook_manager=webhook_manager,
            allow_tool_preprocessing=allow_tool_preprocessing,
            defer_context_shaping=foreground_policy.enabled,
        )

        # Research injection
        research_blocked_by_policy = (
            tool_policy.blocks("trigger_research")
            or tool_policy.blocks("manage_research")
        )
        if use_research and not research_blocked_by_policy:
            try:
                _r_ep, _r_model, _r_headers = _resolve_research_endpoint(sess)
                research_ctx = await research_handler.call_research_service(
                    message, _r_ep, _r_model, llm_headers=_r_headers
                )
                research_message = untrusted_context_message("research context", research_ctx)
                ctx.messages.insert(len(ctx.preface), research_message)
                if foreground_policy.enabled:
                    getattr(ctx, "route_messages", ctx.messages).insert(
                        len(ctx.preface),
                        research_message,
                    )
            except Exception as e:
                logger.error(f"Research failed: {e}")

        foreground_candidates = build_foreground_model_candidates(
            sess.endpoint_url,
            sess.model,
            sess.headers,
            owner=owner,
            policy=foreground_policy,
        )
        route_descriptors = build_foreground_route_descriptors(
            sess.endpoint_url,
            sess.model,
            sess.headers,
            owner=owner,
            policy=foreground_policy,
            selected_endpoint_id=chat_request.selected_endpoint_id,
        )
        candidate_request_factory = None
        selected_context_length = getattr(ctx, "context_length", 0)
        candidate_request_state = {
            "context_lengths": {0: selected_context_length},
            "requests": {0: ctx.messages},
            "trim_stats": {},
        }
        request_messages = ctx.messages
        if foreground_policy.enabled:
            request_messages = getattr(ctx, "route_messages", ctx.messages)
            candidate_request_factory, candidate_request_state = _chat_candidate_request_factory(
                request_messages,
                selected_context_length,
                session=sess,
                owner=owner,
            )
        requested_model = sess.model
        reply, actual_candidate, actual_model = await llm_call_async_with_route_fallback(
            foreground_candidates,
            request_messages,
            fallback_statuses=foreground_policy.eligible_statuses,
            candidate_request_factory=candidate_request_factory,
            temperature=ctx.preset.temperature,
            max_tokens=ctx.preset.max_tokens,
            prompt_type=preset_id,
            session_id=session,
        )
        actual_index = _candidate_index(foreground_candidates, actual_candidate)
        apply_compaction_state(
            sess,
            candidate_request_state.get("compactions", {}).get(actual_index),
        )
        requested_route = route_descriptors[0]
        actual_route = route_descriptors[actual_index]
        actual_trim = candidate_request_state.get("trim_stats", {}).get(actual_index, {})
        _clean_reply, _clean_md = clean_thinking_for_save(
            reply,
            {
                "model": actual_model,
                "requested_model": requested_model,
                "endpoint_id": actual_route.get("endpoint_id"),
                "endpoint_label": actual_route.get("endpoint_label"),
                "requested_endpoint_id": requested_route.get("endpoint_id"),
                "requested_endpoint_label": requested_route.get("endpoint_label"),
                "context_length": candidate_request_state["context_lengths"].get(
                    actual_index,
                    selected_context_length,
                ),
                "context_trimmed": bool(
                    actual_trim
                    and (
                        actual_trim.get("messages_after") < actual_trim.get("messages_before")
                        or actual_trim.get("tokens_after") < actual_trim.get("tokens_before")
                    )
                ),
            },
        )
        sess.add_message(ChatMessage("assistant", _clean_reply, metadata=_clean_md))

        from core.database import update_session_last_accessed
        update_session_last_accessed(session)
        session_manager.save_sessions()

        # Background tasks (memory, webhook, auto-name)
        run_post_response_tasks(
            sess, session_manager, session, message, reply, None,
            ctx.uprefs, memory_manager, memory_vector, webhook_manager,
            character_name=ctx.preset.character_name,
            owner=ctx.user,
            allow_background_extraction=not tool_policy.block_all_tool_calls,
        )

        return {
            "response": reply,
            "requested_model": requested_model,
            "model": actual_model,
            "requested_endpoint_id": requested_route.get("endpoint_id"),
            "requested_endpoint_label": requested_route.get("endpoint_label"),
            "endpoint_id": actual_route.get("endpoint_id"),
            "endpoint_label": actual_route.get("endpoint_label"),
        }

    # ------------------------------------------------------------------ #
    # POST /api/chat_stream
    # ------------------------------------------------------------------ #
    @router.post("/api/chat_stream")
    async def chat_stream(request: Request) -> StreamingResponse:
        body = None
        try:
            if request.headers.get("content-type", "").startswith("application/json"):
                try:
                    body = await request.json()
                except json.JSONDecodeError as e:
                    raise HTTPException(400, f"Invalid JSON: {e}")
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(400, f"Request parsing error: {e}")

        _set_user_time_from_request(request)

        form_data = await request.form()
        message = form_data.get("message")
        session = form_data.get("session")
        attachments = form_data.get("attachments")
        use_web = form_data.get("use_web")
        use_research = form_data.get("use_research")
        time_filter = form_data.get("time_filter")
        preset_id = form_data.get("preset_id")
        selected_endpoint_id = str(
            form_data.get("selected_endpoint_id")
            or (body or {}).get("selected_endpoint_id")
            or ""
        ).strip()
        # Issue #3229: API callers send JSON, not FormData.  Read from the
        # JSON body as fallback so callers who send {"allow_bash": true}
        # actually get bash enabled.
        allow_bash = form_data.get("allow_bash") or (body or {}).get("allow_bash")
        allow_web_search = form_data.get("allow_web_search") or (body or {}).get("allow_web_search")
        use_rag = form_data.get("use_rag")
        search_context = form_data.get("search_context")  # pre-fetched web search results (compare mode)
        compare_mode = str(form_data.get("compare_mode", "")).lower() == "true"
        incognito = str(form_data.get("incognito", "")).lower() == "true"
        plan_mode = str(form_data.get("plan_mode") or (body or {}).get("plan_mode") or "").lower() == "true"
        chat_mode = str(form_data.get("mode", "")).lower()  # 'chat' or 'agent'
        tool_approval_id = (
            form_data.get("tool_approval_id")
            or (body or {}).get("tool_approval_id")
        )
        tool_approval_decision = (
            form_data.get("tool_approval_decision")
            or (body or {}).get("tool_approval_decision")
        )
        exact_tool_approval = None
        pending_tool_approval = None
        retired_tool_approval_taint = False
        external_untrusted_context_seen = False
        tool_approval_continuation = False
        # Workspace: confine the agent's file/shell tools to this folder.
        workspace, workspace_rejected = _resolve_request_workspace(
            request, form_data.get("workspace")
        )
        # A project bound to this chat's sidebar folder owns the workspace.
        # Chats that belong to no project fall through to the posted value, so
        # everything outside projects behaves exactly as it did before.
        _project_ws = _project_workspace(request, session)
        _project_roots = _project_work_roots(request, session)
        if _project_ws:
            workspace, workspace_rejected = _project_ws, ""
        # Per-project agent knobs → harness_options (checkpoints, tests,
        # review, trusted workspace…). Non-project chats get the defaults.
        _harness_options = _project_harness_options(request, session, workspace)
        # Plan mode is a modifier on agent mode — it only makes sense with tools.
        if plan_mode:
            chat_mode = "agent"
        # An approved plan being EXECUTED: the frontend sends the checklist back
        # on each turn so we can pin it in context. This way a long plan on a
        # weak model survives history truncation — the agent can always re-read
        # the plan. Ignored while still proposing (plan_mode on). Capped so a
        # huge plan can't blow the prompt.
        approved_plan = ""
        if not plan_mode:
            approved_plan = (form_data.get("approved_plan") or "").strip()[:8192]
        # Did the USER explicitly pick agent mode? (vs. us auto-escalating
        # below). Skill extraction should only learn from real agent sessions,
        # not chats we quietly promoted for a notes/calendar intent.
        user_requested_agent = (chat_mode == "agent")
        _search_enabled = web_search_enabled_for_turn(allow_web_search, use_web)
        _explicit_web_intent = False
        _explicit_browser_intent = False
        if isinstance(message, str):
            _msg_l = message.lower()
            _explicit_web_intent = bool(re.search(
                r"\b(search|look\s*up|lookup|google|browse|web|online|latest|current|today|news|weather|forecast|rate|exchange\s+rate)\b",
                _msg_l,
            ))
            # English + Spanish browser wording — see _BROWSER_INTENT_WORDS.
            _explicit_browser_intent = _explicit_browser_intent_for_message(message)
        _allow_browser_for_web_turn = bool(
            _explicit_browser_intent
            or _explicit_web_intent
            or _search_enabled
        )
        # Intent auto-escalation: if the user is clearly asking the assistant
        # to create a todo, reminder, or calendar event, promote chat → agent
        # for this turn so the LLM has access to manage_notes / manage_calendar.
        # This is a LIGHT promotion — see the disabled_tools block below, which
        # withholds shell/code/file tools so the model doesn't try to `bash`
        # its way through a plain chat request (and fail, especially with the
        # shell disabled).
        auto_escalated = False
        _tool_intent = _classify_tool_intent(message) if isinstance(message, str) else None
        _workspace_agent_intent = False
        if chat_mode == "chat" and _tool_intent and _tool_intent.needs_tools:
            chat_mode = "agent"
            auto_escalated = True
            _workspace_agent_intent = _tool_intent.category in {"shell", "workspace"}
            if _workspace_agent_intent:
                allow_bash = "true"
            logger.info(
                "chat→agent auto-escalation: category=%s reason=%s",
                _tool_intent.category,
                _tool_intent.reason,
            )
        elif chat_mode == "chat" and _search_enabled:
            chat_mode = "agent"
            auto_escalated = True
            logger.info("chat→agent auto-escalation: search enabled")
        elif chat_mode == "chat" and _explicit_web_intent:
            chat_mode = "agent"
            auto_escalated = True
            logger.info("chat→agent auto-escalation: explicit web intent")
        active_doc_id = form_data.get("active_doc_id", "").strip()
        logger.info(f"[doc-inject] chat_mode={chat_mode}, active_doc_id={active_doc_id!r}")

        # Active email reader — when the user has an email open in the UI, the
        # frontend passes its uid/folder/account so "reply", "summarize this",
        # etc. resolve to the real email instead of the agent inventing a
        # fake markdown draft.
        active_email_uid = form_data.get("active_email_uid", "").strip()
        active_email_folder = form_data.get("active_email_folder", "INBOX").strip() or "INBOX"
        active_email_account = form_data.get("active_email_account", "").strip()
        active_email_ctx: Optional[Dict[str, str]] = None
        # Always reset between requests so a stale active-email pointer from
        # a previous turn (different reader closed, different account, etc.)
        # can't leak in when the user has no email open this turn.
        try:
            from src.tool_implementations import clear_active_email
            clear_active_email()
        except Exception:
            pass
        if active_email_uid:
            active_email_ctx = {
                "uid": active_email_uid,
                "folder": active_email_folder,
                "account": active_email_account,
            }
            # Try to enrich with subject + from so the agent's system prompt
            # block can quote them. Best-effort: a stale cache is fine, a
            # missing email just means we pass uid/folder/account only.
            try:
                from routes.email_routes import _read_cache_get, _read_cache_key
                _ck = _read_cache_key(active_email_account or None, active_email_folder, active_email_uid, owner=get_current_user(request))
                _cached_email = _read_cache_get(_ck)
                if _cached_email and isinstance(_cached_email, dict):
                    active_email_ctx["subject"] = str(_cached_email.get("subject") or "")
                    active_email_ctx["from"] = str(
                        _cached_email.get("from_address")
                        or _cached_email.get("from")
                        or _cached_email.get("from_name")
                        or ""
                    )
                    _body_preview = (_cached_email.get("body") or "")[:2000]
                    if _body_preview:
                        active_email_ctx["body_preview"] = _body_preview
            except Exception as _e:
                logger.debug(f"[email-inject] cache enrich skipped: {_e}")
            # Stash so email tools can resolve "this email" without UID guessing.
            try:
                from src.tool_implementations import set_active_email
                set_active_email(
                    uid=active_email_uid,
                    folder=active_email_folder,
                    account=active_email_account or None,
                    subject=active_email_ctx.get("subject"),
                    sender=active_email_ctx.get("from"),
                )
            except Exception as _e:
                logger.debug(f"[email-inject] set_active_email failed: {_e}")
            logger.info(
                "[email-inject] active_email uid=%s folder=%s account=%s subject=%r",
                active_email_uid, active_email_folder, active_email_account or "(default)",
                active_email_ctx.get("subject", ""),
            )

        try:
            # Attachment-only sends and approval controls may omit message text.
            _has_atts = (
                bool(body and isinstance(body.get("attachments"), list) and body["attachments"])
                or bool(form_data.get("attachments"))
            )
            message, session = coerce_message_and_session(
                body, message, session, session_manager,
                allow_empty=(_has_atts or bool(tool_approval_id)),
            )
            # Verify ownership AFTER coerce (which may resolve a default session)
            # but BEFORE loading. Prevents cross-user session hijack.
            _verify_session_owner(request, session)
            sess = session_manager.get_session(session)
            owner = effective_user(request)
            if tool_approval_id:
                # Codes, not prose: the browser must branch on the machine
                # field and render the message as-is (src/tool_security.py).
                from src.tool_security import (
                    TOOL_APPROVAL_EXPIRED_CODE,
                    TOOL_APPROVAL_INVALID_CODE,
                    TOOL_APPROVAL_PLAN_MODE_CODE,
                    TOOL_APPROVAL_UNAVAILABLE_CODE,
                    tool_error_detail,
                )

                pending_tool_approval = tool_approval_store.peek(tool_approval_id)
                normalized_owner = str(owner or "").strip().casefold()
                if (
                    pending_tool_approval is None
                    or pending_tool_approval.owner != normalized_owner
                    or pending_tool_approval.session_id != str(session)
                ):
                    # Separate "you took too long" from "this was never yours".
                    # They look identical to the store but not to the user:
                    # an expired card has an obvious way out (rerun the turn),
                    # so the client needs to be able to tell them apart without
                    # reading the prose.
                    if tool_approval_store.was_expired(
                        tool_approval_id, owner=owner
                    ):
                        raise HTTPException(
                            409,
                            tool_error_detail(
                                TOOL_APPROVAL_EXPIRED_CODE,
                                "This approval expired before it was answered. "
                                "Nothing was executed — rerun the turn to get a "
                                "fresh approval card.",
                            ),
                        )
                    raise HTTPException(
                        409,
                        tool_error_detail(
                            TOOL_APPROVAL_INVALID_CODE,
                            "This tool approval is invalid, expired, or belongs to another thread.",
                        ),
                    )
                pending_taint = bool(
                    pending_tool_approval.external_untrusted_context_seen
                )
                external_untrusted_context_seen = (
                    external_untrusted_context_seen or pending_taint
                )
                decision = str(tool_approval_decision or "").strip().lower()
                if decision not in {"approve", "approve_task", "deny"}:
                    raise HTTPException(400, "Invalid tool approval decision.")
                if plan_mode:
                    raise HTTPException(
                        409,
                        tool_error_detail(
                            TOOL_APPROVAL_PLAN_MODE_CODE,
                            "Tool approvals cannot be consumed while plan mode is active.",
                        ),
                    )
                exact_tool_approval = tool_approval_store.consume(
                    tool_approval_id,
                    decision=decision,
                    owner=owner,
                    session_id=session,
                )
                tool_approval_continuation = True
                if (
                    decision in {"approve", "approve_task"}
                    and exact_tool_approval is None
                ):
                    raise HTTPException(
                        409,
                        tool_error_detail(
                            TOOL_APPROVAL_UNAVAILABLE_CODE,
                            "This tool approval could not be consumed.",
                        ),
                    )
                if not _mark_tool_approval_resolved(
                    sess,
                    tool_approval_id,
                    decision,
                ):
                    logger.warning(
                        "Tool approval %s was consumed but its persisted card could not be marked resolved",
                        tool_approval_id,
                    )
                if decision == "deny":
                    return StreamingResponse(
                        _tool_approval_resolution_stream(decision),
                        media_type="text/event-stream",
                    )
                # Approval is a control-plane continuation, not a new user turn.
                # Reuse the sealed interrupted request only for internal context,
                # retrieval, and policy reconstruction; never persist or display it.
                message = pending_tool_approval.continuation_query
                # The sealed server record, not mutable composer state,
                # restores the original action workspace.
                workspace = pending_tool_approval.workspace or None
                workspace_rejected = None
                if pending_tool_approval.document_id:
                    active_doc_id = pending_tool_approval.document_id
                # Restore only the coarse request toggle needed by the exact
                # sealed action. Current privilege, global-disable, incognito,
                # compare, and tool-policy gates still run.
                if pending_tool_approval.tool_name == "bash":
                    allow_bash = "true"
                if pending_tool_approval.tool_name in WEB_TOOL_NAMES:
                    allow_web_search = "true"
                    _search_enabled = True
                chat_mode = "agent"
            else:
                # A normal user message supersedes the card that was waiting
                # in this thread. Retire its opaque grant, but preserve the
                # originating provenance for this turn so dismissing a card
                # cannot make the same model-requested action authoritative.
                retired_tool_approval_taint = tool_approval_store.retire_for_session(
                    owner=owner,
                    session_id=session,
                )
                external_untrusted_context_seen = (
                    external_untrusted_context_seen or retired_tool_approval_taint
                )
            _reconcile_selected_route_from_request(request, sess, session, form_data, owner=owner)
            if _clear_orphaned_session_endpoint(sess, owner=owner):
                raise HTTPException(400, "Selected model endpoint was removed. Pick another model in Settings.")
            # Issue #587: picker shows a model from the endpoint cache but
            # s.model never made it onto the DB row (first-send race after
            # endpoint setup, or a previous endpoint delete/recreate). Pull
            # the first cached model off the matching endpoint so the
            # upstream isn't called with model="" (which surfaces as a
            # generic 401/503).
            _recover_empty_session_model(sess, session, owner=owner)
            if not getattr(sess, "model", "").strip():
                raise HTTPException(
                    400,
                    "No model selected for this chat. Open the model picker and choose one before sending.",
                )
            if not (getattr(sess, "endpoint_url", "") or "").strip():
                raise HTTPException(400, "Selected model endpoint is not configured")
            if (
                chat_mode == "chat"
                and isinstance(message, str)
                and (not _tool_intent or not _tool_intent.needs_tools)
                and _is_contextual_web_followup(message, sess)
            ):
                _tool_intent = ToolIntent(True, "web", "contextual web lookup follow-up")
                chat_mode = "agent"
                auto_escalated = True
                _workspace_agent_intent = False
                logger.info(
                    "chat→agent auto-escalation: category=%s reason=%s",
                    _tool_intent.category,
                    _tool_intent.reason,
                )
            if isinstance(message, str) and _is_contextual_browser_followup(message, sess):
                _explicit_browser_intent = True
                if chat_mode == "chat":
                    chat_mode = "agent"
                    auto_escalated = True
                    _workspace_agent_intent = False
                    logger.info("chat→agent auto-escalation: contextual browser/form follow-up")
            if not workspace and isinstance(message, str):
                _auto_workspace, _ = _resolve_workspace_from_message_path(request, message)
                if _auto_workspace:
                    workspace = _auto_workspace
                    chat_mode = "agent"
                    auto_escalated = True
                    _workspace_agent_intent = True
                    allow_bash = "true"
                    logger.info("chat→agent auto-escalation: explicit path workspace=%s", workspace)
            # A bound workspace plus a coding request (any language the
            # heuristic knows — the intent classifier above is English-only)
            # means "work in this repo": in plain chat the model has no file
            # tools and can only narrate edits it never made.
            if (
                chat_mode == "chat"
                and workspace
                and isinstance(message, str)
                and not tool_approval_id
                and _looks_like_workspace_coding_request(message)
            ):
                chat_mode = "agent"
                auto_escalated = True
                _workspace_agent_intent = True
                allow_bash = "true"
                logger.info("chat→agent auto-escalation: workspace-bound coding request")
        except SessionNotFoundError as e:
            raise HTTPException(404, str(e))
        except (ValueError, ValidationError):
            raise HTTPException(400, "Invalid request parameters")

        # ------------------------------------------------------------------ #
        # Privilege gates that must fire BEFORE any LLM work / token spend.
        #   1. allowed_models — reject if session.model isn't in the user's
        #      configured allowlist (empty list = "no restriction").
        #   2. max_messages_per_day — count user-role ChatMessage rows owned
        #      by this user in the last UTC day; 429 if at/over the cap.
        # Admins always have full privileges via get_privileges (returns
        # ADMIN_PRIVILEGES wholesale) so this is a no-op for them.
        _enforce_chat_privileges(request, sess)

        # Ensure session has auth headers
        resolve_session_auth(sess, session, owner=effective_user(request))

        # Check for research_pending BEFORE mode persist overwrites it
        # An approval response resumes the sealed agent action.  Do not let
        # mutable form fields, or a stale research_pending session marker,
        # consume the one-use grant on the unrelated research path.
        do_research = (
            not tool_approval_continuation
            and str(use_research).lower() == "true"
        )
        if not do_research and not tool_approval_continuation:
            if get_session_mode(session) == 'research_pending':
                do_research = True
                logger.info(f"Session {session} in research_pending — auto-triggering research")

        att_ids = []
        if tool_approval_continuation:
            # Browser composer state is unrelated to the action that was
            # reviewed.  The original turn remains in session history.
            att_ids = []
        elif body and isinstance(body.get("attachments"), list):
            att_ids = [str(x) for x in body["attachments"]]
        elif attachments:
            try:
                att_ids = [str(x) for x in json.loads(attachments)]
            except Exception as e:
                logger.warning("Failed to parse attachments JSON, ignoring attachments", exc_info=e)

        image_generation_session = _is_image_generation_session(sess, owner=effective_user(request))
        no_memory = str(form_data.get("no_memory", "")).lower() == "true"
        if image_generation_session:
            no_memory = True
            use_rag = "false"
            search_context = None
        # Workspace coding turns: personal memories are noise here and local
        # models weave them into the work (a facts blob about the user's other
        # projects made qwen3-coder call the demo app by another project's
        # name). Skip memory retrieval for those turns; setting-controlled.
        if (
            not no_memory
            and workspace
            and chat_mode == "agent"
            and isinstance(message, str)
        ):
            try:
                from src.settings import get_setting as _gs_mem
                from src.agent_loop import _looks_like_workspace_coding_request as _is_coding
                if _gs_mem("agent_workspace_no_memory", True) and _is_coding(message):
                    no_memory = True
                    logger.info("[gen-overrides] workspace coding turn: memory retrieval skipped")
            except Exception:
                pass
        pre_context_tool_policy = build_effective_tool_policy(
            last_user_message=message,
        )
        allow_tool_preprocessing = not pre_context_tool_policy.block_all_tool_calls
        foreground_policy = resolve_foreground_model_policy(
            owner=owner,
            allowed_models=_allowed_models_for_request(request),
        )

        # Build shared context (stream path uses enhanced_message for context preface)
        ctx = await build_chat_context(
            sess, request, chat_handler, chat_processor,
            message=message,
            session_id=session,
            preset_id=preset_id,
            att_ids=att_ids,
            use_web=use_web,
            use_rag=use_rag,
            time_filter=time_filter,
            incognito=incognito,
            no_memory=no_memory,
            search_context=search_context,
            compare_mode=compare_mode,
            webhook_manager=webhook_manager,
            use_enhanced_message=True,
            # Skills index only ships when the model can actually call
            # manage_skills (agent mode). In plain chat or incognito the
            # index would be useless / unwanted noise.
            agent_mode=(chat_mode == "agent"),
            allow_tool_preprocessing=allow_tool_preprocessing,
            defer_context_shaping=foreground_policy.enabled,
            continuation_context_message=(
                pending_tool_approval.continuation_query
                if exact_tool_approval
                and pending_tool_approval
                and pending_tool_approval.continuation_query
                else None
            ),
            persist_user_message=not tool_approval_continuation,
        )

        # Per-session generation overrides from the chat model controls
        # (temperature, max_tokens, top_p, think, ...). Validated here; the
        # sampling extras are forwarded to llm_core as gen_overrides.
        _delegate_tasks = _parse_delegate_tasks(
            form_data.get("delegate_tasks") or (body or {}).get("delegate_tasks")
        )
        _gen_overrides = _parse_gen_overrides(
            form_data.get("gen_overrides") or (body or {}).get("gen_overrides")
        )
        _temperature_explicit = False
        if _gen_overrides:
            if "temperature" in _gen_overrides:
                ctx.preset.temperature = _gen_overrides.pop("temperature")
                _temperature_explicit = True
            if "max_tokens" in _gen_overrides:
                ctx.preset.max_tokens = _gen_overrides.pop("max_tokens")
            logger.info("[gen-overrides] session=%s temperature=%s max_tokens=%s extra=%s",
                        session, ctx.preset.temperature, ctx.preset.max_tokens, _gen_overrides)

        _research_flags = {"do": do_research}  # Mutable container for generator scope

        # Query active document — prefer explicit ID from frontend, fall back to session lookup
        active_doc = None
        _doc_db = SessionLocal()
        try:
            if active_doc_id:
                logger.info(f"[doc-inject] active_doc_id from frontend: {active_doc_id}")
                # Scope to the caller's documents. The session and in-memory
                # fallbacks below are already owner/session-bound; this
                # explicit-id path looked up by id alone, so a user could
                # inject another user's document by passing its id.
                _doc_q = _doc_db.query(DBDocument).filter(DBDocument.id == active_doc_id)
                active_doc = _owner_session_filter(_doc_q, ctx.user).first()
                if active_doc:
                    doc_session = active_doc.session_id
                    doc_owner = getattr(active_doc, "owner", None)
                    if doc_owner and ctx.user and doc_owner != ctx.user:
                        logger.warning(
                            "[doc-inject] ignoring active_doc_id %s owned by another user",
                            active_doc_id,
                        )
                        active_doc = None
                    else:
                        # NOTE: previously dropped the doc when doc.session_id
                        # != current chat session — but that broke the common
                        # case of "open an email draft from one chat, ask a
                        # different chat to write into it". The frontend only
                        # sends active_doc_id for docs currently visible in
                        # the UI, and we already owner-checked above, so trust
                        # the explicit signal. We just log the mismatch and
                        # re-bind the doc to the current session so future
                        # turns find it via the session-fallback path too.
                        if doc_session and doc_session != session:
                            logger.info(
                                "[doc-inject] cross-session active_doc_id %s (was session %s, now %s) — accepting and rebinding",
                                active_doc_id, doc_session, session,
                            )
                            try:
                                active_doc.session_id = session
                                _doc_db.commit()
                            except Exception as _e:
                                _doc_db.rollback()
                                logger.warning(f"[doc-inject] session rebind failed: {_e}")
                        logger.info(f"[doc-inject] found by ID: title={active_doc.title!r}, lang={active_doc.language!r}, is_active={active_doc.is_active}, content_len={len(active_doc.current_content or '')}")
                else:
                    logger.warning(f"[doc-inject] NOT FOUND by ID {active_doc_id}")
            if not active_doc:
                _email_doc_q = _doc_db.query(DBDocument).filter(
                    DBDocument.session_id == session,
                    DBDocument.is_active == True,
                    DBDocument.language == "email",
                )
                active_doc = _owner_session_filter(_email_doc_q, ctx.user).order_by(DBDocument.updated_at.desc()).first()
                if active_doc:
                    logger.info(f"[doc-inject] found email draft by session fallback: title={active_doc.title!r}")
            if not active_doc:
                _session_doc_q = _doc_db.query(DBDocument).filter(
                    DBDocument.session_id == session,
                    DBDocument.is_active == True
                )
                active_doc = _owner_session_filter(_session_doc_q, ctx.user).order_by(DBDocument.updated_at.desc()).first()
                if active_doc:
                    logger.info(f"[doc-inject] found by session fallback: title={active_doc.title!r}")
            # Last resort: the document the agent itself just created/edited
            # (tracked in-memory by the tool layer). This rescues docs that
            # got orphaned from their session (session_id NULL) — otherwise
            # neither lookup above can associate them with this conversation,
            # so the agent never sees what it just wrote. Guarded so we never
            # leak a doc that belongs to a DIFFERENT session.
            if not active_doc:
                try:
                    from src.agent_tools.document_tools import get_active_document
                    _mem_id = get_active_document()
                    if _mem_id:
                        _mem_q = _doc_db.query(DBDocument).filter(DBDocument.id == _mem_id)
                        cand = _owner_session_filter(_mem_q, ctx.user).first()
                        if cand and (not cand.session_id or cand.session_id == session):
                            active_doc = cand
                            logger.info(f"[doc-inject] found by in-memory active id: title={active_doc.title!r} (session_id={cand.session_id!r})")
                except Exception as _e:
                    logger.debug(f"[doc-inject] in-memory fallback failed: {_e}")
            if not active_doc:
                logger.info(f"[doc-inject] no active doc for session {session}")
            if active_doc:
                _doc_db.expunge(active_doc)
        except Exception as e:
            logger.warning(f"Failed to query active document: {e}")
        finally:
            _doc_db.close()

        # Build disabled-tools set from frontend toggles + user privileges
        disabled_tools = set()
        # Only disable bash when the caller *explicitly* set it to a falsy
        # value. When unset (None), defer to per-user privilege checks below.
        # Web search is per-turn opt-in: either the chat pre-search setting
        # (`use_web=true`) or agent web toggle (`allow_web_search=true`) must
        # explicitly enable it.
        if allow_bash is not None and str(allow_bash).lower() != "true":
            disabled_tools.add("bash")
        _explicit_web_intent = _explicit_web_intent or bool(_tool_intent and _tool_intent.category == "web")
        if is_web_search_explicitly_denied(allow_web_search) or not _search_enabled:
            disabled_tools.update(WEB_TOOL_NAMES)
        if _explicit_web_intent:
            # A direct lookup/search request should not drift into personal
            # tools or shell fallbacks. It can only use web_search/web_fetch
            # when the request's explicit web setting enabled them.
            disabled_tools.update({
                "bash", "python",
                "search_chats", "manage_skills", "manage_memory",
                "read_file", "write_file", "edit_file",
                "create_document", "edit_document", "update_document",
                "send_email", "reply_to_email",
                "manage_notes", "manage_calendar", "manage_tasks",
                "api_call",
            })
            if _search_enabled:
                disabled_tools.difference_update(WEB_TOOL_NAMES)
            else:
                disabled_tools.update(WEB_TOOL_NAMES)
        elif _search_enabled:
            disabled_tools.difference_update(WEB_TOOL_NAMES)

        # Nobody/incognito mode: deny tools that would expose the user's
        # persistent memory, past chats, or other identity-linked data.
        if incognito:
            disabled_tools.update({
                "manage_memory",      # persistent memory store
                "search_chats",       # past chat history
                "manage_skills",      # skill presets tied to user
                "create_session",
                "list_sessions",
                "manage_session",
                "send_to_session",
                "chat_with_model",
            })

        # Active email reader open → strip the tools that let the agent drift
        # away from the visible email or skip review. The only allowed compose
        # path is ui_control open_email_reply, which opens the same draft editor
        # as the Reply button with the generated body pre-filled. This prevents
        # the model from falling back to direct SMTP when it botches a draft
        # call, and prevents fake email-shaped documents.
        if active_email_ctx and active_email_ctx.get("uid"):
            disabled_tools.update({
                "create_document",
                "send_email",
                "reply_to_email",
                "mcp__email__send_email",
                "mcp__email__reply_to_email",
            })

        # Enforce per-user privileges
        _privs = {}
        _user = ctx.user
        if _user and hasattr(request.app.state, 'auth_manager') and request.app.state.auth_manager:
            _privs = request.app.state.auth_manager.get_privileges(_user)
        if _privs:
            if not _privs.get("can_use_bash", True):
                disabled_tools.update({"bash", "python", "read_file", "write_file"})
            if not _privs.get("can_use_browser", True):
                disabled_tools.update(_browser_mcp_denylist())
            if not _privs.get("can_use_documents", True):
                disabled_tools.update({"create_document", "edit_document", "update_document", "suggest_document"})
            if not _privs.get("can_generate_images", True):
                disabled_tools.add("generate_image")
            if not _privs.get("can_manage_memory", True):
                disabled_tools.update({"manage_memory", "manage_skills"})
            if not _privs.get("can_use_research", True):
                _research_flags["do"] = False
            if not _privs.get("can_use_agent", True):
                _effective_mode = 'chat'
                chat_mode = 'chat'
        # Global admin disabled tools
        from src.settings import get_setting
        _global_disabled = get_setting("disabled_tools", [])
        if _global_disabled and isinstance(_global_disabled, list):
            disabled_tools.update(_global_disabled)

        # Light auto-escalation: the user is in chat mode and just expressed a
        # notes/calendar/email intent. Grant the relevant managers but withhold
        # the heavy "do things on the computer" tools — otherwise the model
        # tries to shell out for a request that never needed it, then fails
        # (and looks broken when the shell is disabled).
        if auto_escalated and not _workspace_agent_intent:
            disabled_tools.update({
                "bash", "python", "read_file", "write_file",
            })
            if not _allow_browser_for_web_turn:
                disabled_tools.update(_BROWSER_MCP_TOOLS)

        # Disable document tools in compare sessions — they break the pane UI
        if sess.name and sess.name.startswith("[CMP]"):
            disabled_tools.update({"create_document", "edit_document", "update_document"})

        # Compare mode: disable tools based on compare type
        if compare_mode:
            _compare_strip = {
                "create_document", "edit_document", "update_document",
                "chat_with_model", "create_session", "list_sessions",
                "send_to_session",
                "pipeline", "manage_session", "manage_memory", "list_models",
                "generate_image", "ui_control",
            }
            disabled_tools.update(_compare_strip)
            # In chat mode compare, disable ALL agent tools (no bash, python, file ops)
            if chat_mode == 'chat':
                disabled_tools.update({"bash", "python", "read_file", "write_file", "web_search", "web_fetch", "search_chats", "manage_tasks"})

        # Plan mode: investigate read-only, propose a plan, don't mutate. Block
        # every tool not on the read-only allowlist. (stream_agent_loop enforces
        # this again + drops MCP, so this is belt-and-suspenders.)
        if plan_mode:
            from src.tool_security import plan_mode_disabled_tools
            disabled_tools.update(plan_mode_disabled_tools())

        tool_policy = build_effective_tool_policy(
            disabled_tools=disabled_tools,
            last_user_message=message,
        )
        disabled_tools = tool_policy.all_disabled_names()
        research_blocked_by_policy = bool(
            tool_policy.blocks("trigger_research")
            or tool_policy.blocks("manage_research")
        )
        effective_do_research = bool(
            do_research and _research_flags["do"] and not research_blocked_by_policy
        )

        # Persist session mode after policy/privilege gates so blocked research
        # turns remain ordinary chat/agent streams and saved messages.
        _effective_mode = 'research' if effective_do_research else (chat_mode or 'chat')
        if _effective_mode in ('agent', 'research', 'chat'):
            set_session_mode(session, _effective_mode)

        async def stream_with_save() -> AsyncGenerator[str, None]:
            # _effective_mode is read-only here; closure captures it from
            # the outer scope. (Was `nonlocal` but never reassigned.)
            research_sources = None
            web_sources = ctx.web_sources

            # Register active stream for partial-save safety net
            _active_streams[session] = {"status": "streaming", "partial": "", "query": message, "is_research": effective_do_research, "mode": _effective_mode}

            # The client sent a workspace the server refused to bind (deleted
            # folder, file path, sensitive dir, filesystem root). Tell it up
            # front so the UI can clear the pill instead of displaying a
            # confinement that is not actually in effect.
            if workspace_rejected:
                yield f"data: {json.dumps({'type': 'workspace_rejected', 'data': {'path': workspace_rejected}})}\n\n"

            if ctx.preprocessed.attachment_meta:
                yield f"data: {json.dumps({'type': 'attachments', 'data': ctx.preprocessed.attachment_meta})}\n\n"

            # Announce any docs auto-created during preprocess (e.g. fillable
            # PDF → editable markdown) so the editor pane switches to them
            # before the model starts streaming.
            for _opened in ctx.auto_opened_docs:
                yield (
                    f'data: {json.dumps({"type": "doc_update", **_opened})}\n\n'
                )

            if ctx.rag_sources:
                yield f"data: {json.dumps({'type': 'rag_sources', 'data': ctx.rag_sources})}\n\n"

            if web_sources:
                yield f"data: {json.dumps({'type': 'web_sources', 'data': web_sources})}\n\n"

            # Emit which memories were injected into context (captured before stream)
            if ctx.used_memories:
                yield f"data: {json.dumps({'type': 'memories_used', 'data': ctx.used_memories})}\n\n"

            # Run research as a background task (survives page refresh)
            if effective_do_research:
                _r_ep, _r_model, _r_headers = _resolve_research_endpoint(sess)
                _auth_keys = list(_r_headers.keys()) if _r_headers else []
                logger.info(f"Research endpoint resolved: model={_r_model}, endpoint={redact_url(_r_ep)}, auth_keys={_auth_keys}, sess_headers_keys={list(sess.headers.keys()) if isinstance(sess.headers, dict) else type(sess.headers)}")

                # Clarification round: only for very short/vague queries on first research message.
                # Skip in compare mode — each pane is a fresh session, so every one would
                # ask clarifying questions and the user would have to answer each pane
                # separately, breaking the parallel comparison.
                _prior_json = research_handler._get_session_json(session)
                _history_len = len(sess.history) if hasattr(sess, 'history') else 0
                _is_first_research = not _prior_json and _history_len <= 2 and not compare_mode

                if _is_first_research:
                    logger.info(f"First research message — asking clarifying questions for: {message[:60]}")
                    yield f'data: {json.dumps({"type": "model_info", "model": sess.model, "suffix": "Research"})}\n\n'
                    # Set DB mode to research_pending so the NEXT message auto-triggers research
                    set_session_mode(session, "research_pending")
                    ctx.messages.insert(0, {"role": "system", "content":
                        "The user wants to start deep web research. Before searching, ask 2-3 brief "
                        "clarifying questions to understand exactly what they want to know. For example: "
                        "what aspects matter most, are they comparing to something, what's their context "
                        "(moving, traveling, curiosity). Be conversational. Keep it short."
                    })
                    if foreground_policy.enabled:
                        getattr(ctx, "route_messages", ctx.messages).insert(0, dict(ctx.messages[0]))
                    _skip_research = True
                else:
                    _skip_research = False

                if not _skip_research:
                    # Phase 2: Start actual research
                    def _on_research_done(_sid, _result, _sources, _findings):
                        """Persist research to DB when background task finishes."""
                        if incognito:
                            return
                        try:
                            _s = session_manager.get_session(_sid)
                            if not _s:
                                logger.warning(f"Session {_sid} expired before research completed")
                                return
                            _md = {"research": True, "model": _s.model}
                            if _sources:
                                _md["research_sources"] = _sources
                            if _findings:
                                _md["research_findings"] = _findings
                            _clean_res, _md = clean_thinking_for_save(_result, _md)
                            _s.add_message(ChatMessage("assistant", _clean_res, metadata=_md))
                            session_manager.save_sessions()
                            logger.info(f"Research result persisted to DB for session {_sid}")
                        except Exception as _e:
                            logger.error(f"Failed to persist research to DB: {_e}")

                    # Check for prior research to continue from
                    _prior_report = ""
                    _prior_findings = None
                    _prior_urls = None
                    _prior_json = research_handler._get_session_json(session)
                    if _prior_json:
                        _prior_report = _prior_json.get("raw_report", "")
                        _prior_findings = _prior_json.get("raw_findings")
                        _src_urls = {s.get("url", "") for s in (_prior_json.get("sources") or []) if s.get("url")}
                        _prior_urls = _src_urls if _src_urls else None
                        if _prior_report:
                            logger.info(f"Continuing research for session {session} with {len(_src_urls)} prior URLs")

                    # Synthesize conversation into a focused research query
                    _research_query = await research_handler.synthesize_query(
                        sess, message, _r_ep, _r_model, _r_headers,
                    )
                    logger.info(f"Research query: {_research_query[:120]}")

                    research_handler.start_research(
                        session, _research_query, _r_ep, _r_model,
                        llm_headers=_r_headers,
                        prior_report=_prior_report,
                        prior_findings=_prior_findings,
                        prior_urls=_prior_urls,
                        on_complete=_on_research_done,
                        owner=_user,
                    )

                    _heartbeat_counter = 0
                    _last_progress = {}
                    _sent_avg = False
                    while True:
                        status = research_handler.get_status(session)
                        if not status or status["status"] != "running":
                            break
                        progress = status.get("progress", {})
                        if progress and progress != _last_progress:
                            _last_progress = progress
                            if not _sent_avg:
                                _sent_avg = True
                                progress = dict(progress)
                                progress["started_at"] = status.get("started_at")
                                avg = status.get("avg_duration")
                                if avg:
                                    progress["avg_duration"] = avg
                            yield f"data: {json.dumps({'type': 'research_progress', 'data': progress})}\n\n"
                            _heartbeat_counter = 0
                        else:
                            _heartbeat_counter += 1
                            yield f": heartbeat {_heartbeat_counter}\n\n"
                        await asyncio.sleep(1.0)

                    research_sources = research_handler.get_sources(session)
                    if research_sources:
                        yield f"data: {json.dumps({'type': 'research_sources', 'data': research_sources})}\n\n"

                    research_findings = research_handler.get_raw_findings(session)
                    if research_findings:
                        yield f"data: {json.dumps({'type': 'research_findings', 'data': research_findings})}\n\n"

                    # Signal frontend to fetch and render the research result
                    yield f"data: {json.dumps({'type': 'research_done', 'data': {'session_id': session}})}\n\n"
                    yield "data: [DONE]\n\n"
                    research_handler.clear_result(session)
                    _stream_set(session, status="done")
                    _active_streams.pop(session, None)
                    return

            context_source = (
                getattr(ctx, "route_messages", ctx.messages)
                if foreground_policy.enabled
                else ctx.messages
            )
            messages = (
                list(context_source)
                if tool_approval_continuation
                else _ensure_current_request_is_latest_user(
                    context_source, message, getattr(ctx, "context_length", 0) or 0
                )
            )

            # Auto-compact notification
            if ctx.was_compacted:
                yield f"data: {json.dumps({'type': 'compacted', 'context_length': ctx.context_length})}\n\n"
            if ctx.context_trimmed and not ctx.was_compacted:
                yield f"data: {json.dumps({'type': 'context_trimmed', 'data': {'context_length': ctx.context_length, 'messages_before': ctx.context_messages_before_trim, 'messages_after': ctx.context_messages_after_trim, 'tokens_before': ctx.context_tokens_before_trim, 'tokens_after': ctx.context_tokens_after_trim}})}\n\n"

            full_response = ""
            thinking_response = ""
            last_metrics = None

            # Foreground Chat and Agent requests share one explicit owner-aware
            # policy. Strict mode is the default; legacy values are unrelated.
            _foreground_policy = foreground_policy
            _foreground_candidates = build_foreground_model_candidates(
                sess.endpoint_url,
                sess.model,
                sess.headers,
                owner=_user,
                policy=_foreground_policy,
            )
            _foreground_route_descriptors = build_foreground_route_descriptors(
                sess.endpoint_url,
                sess.model,
                sess.headers,
                owner=_user,
                policy=_foreground_policy,
                selected_endpoint_id=selected_endpoint_id,
            )
            _chat_request_factory = None
            _selected_context_length = getattr(ctx, "context_length", 0)
            _chat_request_state = {
                "context_lengths": {0: _selected_context_length},
                "requests": {0: messages},
                "trim_stats": {},
            }
            if _foreground_policy.enabled:
                _chat_request_factory, _chat_request_state = _chat_candidate_request_factory(
                    messages,
                    _selected_context_length,
                    session=sess,
                    owner=_user,
                )

            # Send model name early so the frontend can show it during streaming
            _model_suffix = "Research" if effective_do_research else None
            _selected_route = _foreground_route_descriptors[0]
            _model_info = {
                "type": "model_info",
                "model": sess.model,
                "endpoint_id": _selected_route.get("endpoint_id"),
                "endpoint_label": _selected_route.get("endpoint_label"),
            }
            if _model_suffix:
                _model_info["suffix"] = _model_suffix
            if ctx.preset.character_name:
                _model_info["character_name"] = ctx.preset.character_name
            yield f'data: {json.dumps(_model_info)}\n\n'

            _terminal_saved = False
            if _is_image_generation_session(sess, owner=_user):
                from src.settings import get_setting
                if tool_policy.blocks("generate_image"):
                    _blocked_msg = tool_policy.reason_for("generate_image")
                    yield f'data: {json.dumps({"delta": _blocked_msg})}\n\n'
                    yield "data: [DONE]\n\n"
                    _active_streams.pop(session, None)
                    return
                if not get_setting("image_gen_enabled", True):
                    yield f'data: {json.dumps({"delta": "Image generation is disabled by the administrator."})}\n\n'
                    yield "data: [DONE]\n\n"
                    _active_streams.pop(session, None)
                    return
                from src.ai_interaction import do_edit_image, do_generate_image
                _user_msg = message or ""
                _image_upload = _first_image_attachment(chat_handler, att_ids, owner=_user)
                _image_tool_name = "edit_image" if _image_upload else "generate_image"
                yield f'data: {json.dumps({"type": "tool_start", "tool": _image_tool_name, "command": _user_msg[:100]})}\n\n'
                yield ": heartbeat\n\n"
                _progress_queue: asyncio.Queue = asyncio.Queue()

                async def _image_progress_callback(progress: Dict[str, Any]):
                    try:
                        _progress_queue.put_nowait(progress)
                    except Exception:
                        pass

                if _image_upload:
                    _img_task = asyncio.create_task(do_edit_image(
                        _user_msg,
                        _image_upload.get("path", ""),
                        model_spec=sess.model,
                        session_id=session,
                        owner=_user,
                        size="1024x1024",
                        progress_callback=_image_progress_callback,
                    ))
                else:
                    _img_task = asyncio.create_task(do_generate_image(f"{_user_msg}\n{sess.model}\n512x512", session, owner=_user))
                _img_started = time.time()
                _img_tick = 0
                while not _img_task.done():
                    try:
                        _progress = await asyncio.wait_for(_progress_queue.get(), timeout=2.0)
                    except asyncio.TimeoutError:
                        _progress = None
                    _img_tick += 1
                    _elapsed = int(time.time() - _img_started)
                    _label = "Editing image" if _image_upload else "Generating image"
                    yield ": image generation still running\n\n"
                    _progress_data = {"type": "tool_progress", "tool": _image_tool_name, "message": f"{_label}… {_elapsed}s", "elapsed": _elapsed, "tick": _img_tick}
                    if isinstance(_progress, dict) and _progress.get("total"):
                        _step = int(_progress.get("step") or 0)
                        _total = int(_progress.get("total") or 0)
                        _percent = _progress.get("percent")
                        _progress_data.update({
                            "step": _step,
                            "total": _total,
                            "percent": _percent,
                            "message": f"{_label}… {_step}/{_total}",
                        })
                    yield f'data: {json.dumps(_progress_data)}\n\n'
                _img_result = await _img_task
                _img_output = _img_result.get("results", _img_result.get("error", ""))
                _img_tool_data = {"type": "tool_output", "tool": _image_tool_name, "command": _user_msg[:100], "output": _img_output, "exit_code": 0 if "error" not in _img_result else 1}
                for _k in ("image_url", "image_id", "image_prompt", "image_model", "image_size", "image_quality"):
                    if _k in _img_result:
                        _img_tool_data[_k] = _img_result[_k]
                if _image_upload:
                    _img_tool_data["source_image"] = {
                        "id": _image_upload.get("id"),
                        "name": _image_upload.get("name") or _image_upload.get("original_name"),
                    }
                yield f'data: {json.dumps(_img_tool_data)}\n\n'
                if _img_result.get("image_url"):
                    _img_event = {"type": "generated_image", "url": _img_result.get("image_url")}
                    for _k in ("image_url", "image_id", "image_prompt", "image_model", "image_size", "image_quality"):
                        if _img_result.get(_k):
                            _img_event[_k] = _img_result[_k]
                    yield f'data: {json.dumps(_img_event)}\n\n'
                _desc = _img_result.get("results", _img_result.get("error", "Image generation complete"))
                full_response = _desc
                yield f'data: {json.dumps({"delta": _desc})}\n\n'
                # Save to session history
                if not incognito:
                    _ev = {"round": 1, "tool": _image_tool_name, "command": _user_msg[:100], "output": _img_output, "exit_code": 0 if "error" not in _img_result else 1}
                    for _ek in ("image_url", "image_id", "image_prompt", "image_model", "image_size", "image_quality"):
                        if _img_result.get(_ek):
                            _ev[_ek] = _img_result[_ek]
                    if _image_upload:
                        _ev["source_image_id"] = _image_upload.get("id")
                        _ev["source_image_name"] = _image_upload.get("name") or _image_upload.get("original_name")
                    sess.add_message(ChatMessage("assistant", full_response, metadata={"tool_events": [_ev], "model": sess.model}))
                    session_manager.save_sessions()
                yield f'data: {json.dumps({"type": "metrics", "data": {"total_time": 0}})}\n\n'
                yield "data: [DONE]\n\n"
                _active_streams.pop(session, None)
                return
            elif chat_mode == "chat":
                _chat_start = time.time()
                _answered_by = None  # set if the selected model failed and a fallback answered
                _requested_model = sess.model
                _actual_model = None
                _requested_route = _foreground_route_descriptors[0]
                _actual_route = _requested_route
                _actual_candidate_index = 0
                _chat_terminal_saved = False
                def _commit_chat_compaction(candidate_index: int) -> bool:
                    return apply_compaction_state(
                        sess,
                        _chat_request_state.get("compactions", {}).get(candidate_index),
                    )

                # ── Chat mode: call stream_llm directly, NO tools, NO document access ──
                try:
                    async for chunk in stream_llm_with_fallback(
                        _foreground_candidates,
                        messages,
                        temperature=ctx.preset.temperature,
                        # Respect the preset; 0/unset = let the server decide (no
                        # cap), matching agent mode. The old hard 4096 fallback
                        # truncated reasoning models mid-<think> — they'd burn the
                        # whole budget thinking and never emit the answer (seen in
                        # Compare on heavy generation prompts).
                        max_tokens=ctx.preset.max_tokens,
                        prompt_type=preset_id,
                        tools=None,
                        session_id=session,
                        fallback_statuses=_foreground_policy.eligible_statuses,
                        fallback_on_empty=_foreground_policy.fallback_on_empty,
                        candidate_request_factory=_chat_request_factory,
                        candidate_route_descriptors=_foreground_route_descriptors,
                        gen_overrides=_gen_overrides or None,
                    ):
                        if chunk.startswith("data: ") and not chunk.startswith("data: [DONE]"):
                            try:
                                data = json.loads(chunk[6:])
                                if "delta" in data:
                                    if _commit_chat_compaction(_actual_candidate_index):
                                        _compacted_length = _chat_request_state["context_lengths"].get(
                                            _actual_candidate_index,
                                            _selected_context_length,
                                        )
                                        yield f'data: {json.dumps({"type": "compacted", "context_length": _compacted_length})}\n\n'
                                    # Reasoning tokens arrive flagged thinking:true.
                                    # Forward them so the client can show a thinking
                                    # indicator, but don't fold them into the saved
                                    # reply (mirrors the rewrite path below).
                                    if data.get("thinking"):
                                        thinking_response += data["delta"]
                                    else:
                                        full_response += data["delta"]
                                        _stream_set(session, partial=full_response)
                                    yield chunk
                                elif data.get("type") == "fallback":
                                    # Selected model failed; a fallback answered.
                                    # Forward the notice and remember the real model.
                                    _answered_by = data.get("answered_by") or _answered_by
                                    _actual_model = _actual_model or _answered_by
                                    _actual_candidate_index = data.get("candidate_index", 0)
                                    if not isinstance(_actual_candidate_index, int):
                                        _actual_candidate_index = 0
                                    if 0 <= _actual_candidate_index < len(_foreground_route_descriptors):
                                        _actual_route = _foreground_route_descriptors[_actual_candidate_index]
                                    if _commit_chat_compaction(_actual_candidate_index):
                                        _compacted_length = _chat_request_state["context_lengths"].get(
                                            _actual_candidate_index,
                                            _selected_context_length,
                                        )
                                        yield f'data: {json.dumps({"type": "compacted", "context_length": _compacted_length})}\n\n'
                                    data["selected_model"] = data.get("selected_model") or _requested_model
                                    yield f'data: {json.dumps(data)}\n\n'
                                elif data.get("type") == "model_actual":
                                    if _commit_chat_compaction(_actual_candidate_index):
                                        _compacted_length = _chat_request_state["context_lengths"].get(
                                            _actual_candidate_index,
                                            _selected_context_length,
                                        )
                                        yield f'data: {json.dumps({"type": "compacted", "context_length": _compacted_length})}\n\n'
                                    _actual_model = data.get("model") or _actual_model
                                    data["requested_model"] = _requested_model
                                    data["requested_endpoint_id"] = _requested_route.get("endpoint_id")
                                    data["requested_endpoint_label"] = _requested_route.get("endpoint_label")
                                    data["endpoint_id"] = _actual_route.get("endpoint_id")
                                    data["endpoint_label"] = _actual_route.get("endpoint_label")
                                    yield f'data: {json.dumps(data)}\n\n'
                                elif data.get("type") == "usage":
                                    if _commit_chat_compaction(_actual_candidate_index):
                                        _compacted_length = _chat_request_state["context_lengths"].get(
                                            _actual_candidate_index,
                                            _selected_context_length,
                                        )
                                        yield f'data: {json.dumps({"type": "compacted", "context_length": _compacted_length})}\n\n'
                                    last_metrics = data.get("data", {})
                                    _reported_model = last_metrics.get("model")
                                    last_metrics["requested_model"] = _requested_model
                                    last_metrics["model"] = _reported_model or _actual_model or _answered_by or _requested_model
                                    last_metrics["requested_endpoint_id"] = _requested_route.get("endpoint_id")
                                    last_metrics["requested_endpoint_label"] = _requested_route.get("endpoint_label")
                                    last_metrics["endpoint_id"] = _actual_route.get("endpoint_id")
                                    last_metrics["endpoint_label"] = _actual_route.get("endpoint_label")
                                    if isinstance(
                                        _actual_route.get("endpoint_cost_tracked"),
                                        bool,
                                    ):
                                        last_metrics["endpoint_cost_tracked"] = _actual_route.get(
                                            "endpoint_cost_tracked"
                                        )
                                    _actual_context_length = _chat_request_state["context_lengths"].get(
                                    _actual_candidate_index,
                                        _selected_context_length,
                                    )
                                    _route_trim = _chat_request_state.get("trim_stats", {}).get(
                                        _actual_candidate_index,
                                        {},
                                    )
                                    if _route_trim and (
                                        _route_trim.get("messages_after") < _route_trim.get("messages_before")
                                        or _route_trim.get("tokens_after") < _route_trim.get("tokens_before")
                                    ):
                                        last_metrics["context_trimmed"] = True
                                        last_metrics["context_messages_before_trim"] = _route_trim.get("messages_before")
                                        last_metrics["context_messages_after_trim"] = _route_trim.get("messages_after")
                                        last_metrics["context_tokens_before_trim"] = _route_trim.get("tokens_before")
                                        last_metrics["context_tokens_after_trim"] = _route_trim.get("tokens_after")
                                    elif ctx.context_trimmed:
                                        last_metrics["context_trimmed"] = True
                                        last_metrics["context_messages_before_trim"] = ctx.context_messages_before_trim
                                        last_metrics["context_messages_after_trim"] = ctx.context_messages_after_trim
                                        last_metrics["context_tokens_before_trim"] = ctx.context_tokens_before_trim
                                        last_metrics["context_tokens_after_trim"] = ctx.context_tokens_after_trim
                                    if _actual_context_length and last_metrics.get("input_tokens"):
                                        pct = min(round((last_metrics["input_tokens"] / _actual_context_length) * 100, 1), 100.0)
                                        last_metrics["context_percent"] = pct
                                        last_metrics["context_length"] = _actual_context_length
                                    # The frontend reads `tokens_per_second`; the raw usage event
                                    # carries the backend's true gen speed as `gen_tps` (llama.cpp
                                    # timings). Map it through so this direct-chat path shows real
                                    # t/s instead of "n/a" → falling back to a bare token count.
                                    if last_metrics.get("gen_tps") and not last_metrics.get("tokens_per_second"):
                                        last_metrics["tokens_per_second"] = last_metrics["gen_tps"]
                                        last_metrics["tps_source"] = "backend"
                                    # Wall-clock response time for the stats popup ("Time").
                                    last_metrics.setdefault("response_time", round(time.time() - _chat_start, 2))
                                    yield f'data: {json.dumps({"type": "metrics", "data": last_metrics})}\n\n'
                            except json.JSONDecodeError:
                                yield chunk
                        elif chunk.startswith("event: error"):
                            logger.warning(f"Stream error for {sess.model} on {sess.endpoint_url}: {chunk!r}")
                            if (
                                not _chat_terminal_saved
                                and (full_response.strip() or thinking_response.strip())
                            ):
                                _failure_status = _stream_failure_status(chunk)
                                _failure_message = (
                                    f"Model request failed (HTTP {_failure_status})"
                                    if _failure_status is not None
                                    else "Model request failed"
                                )
                                _terminal_content = full_response.strip()
                                _failure_note = f"[Response stopped: {_failure_message}]"
                                _terminal_content = (
                                    f"{_terminal_content}\n\n{_failure_note}"
                                    if _terminal_content
                                    else _failure_note
                                )
                                _had_terminal_usage = bool(last_metrics)
                                _terminal_metrics = dict(last_metrics or {})
                                if not _had_terminal_usage:
                                    _actual_request_messages = _chat_request_state["requests"].get(
                                        _actual_candidate_index,
                                        messages,
                                    )
                                    _actual_context_length = _chat_request_state["context_lengths"].get(
                                        _actual_candidate_index,
                                        _selected_context_length,
                                    )
                                    _estimated_input = estimate_tokens(_actual_request_messages)
                                    _estimated_output = max(
                                        len(full_response + thinking_response) // 4,
                                        0,
                                    )
                                    _terminal_metrics.update({
                                        "input_tokens": _estimated_input,
                                        "output_tokens": _estimated_output,
                                        "total_tokens": _estimated_input + _estimated_output,
                                        "usage_source": "estimated",
                                        "response_time": round(time.time() - _chat_start, 2),
                                        "context_length": _actual_context_length,
                                        "context_percent": (
                                            min(
                                                round(
                                                    (_estimated_input / _actual_context_length) * 100,
                                                    1,
                                                ),
                                                100.0,
                                            )
                                            if _actual_context_length
                                            else 0
                                        ),
                                    })
                                _terminal_metrics.update({
                                    "failed": True,
                                    "failure": {
                                        "status": _failure_status,
                                        "message": _failure_message,
                                    },
                                    "model": _actual_model or _answered_by or _requested_model,
                                    "requested_model": _requested_model,
                                    "endpoint_id": _actual_route.get("endpoint_id"),
                                    "endpoint_label": _actual_route.get("endpoint_label"),
                                    "requested_endpoint_id": _requested_route.get("endpoint_id"),
                                    "requested_endpoint_label": _requested_route.get("endpoint_label"),
                                })
                                if isinstance(
                                    _actual_route.get("endpoint_cost_tracked"),
                                    bool,
                                ):
                                    _terminal_metrics["endpoint_cost_tracked"] = _actual_route.get(
                                        "endpoint_cost_tracked"
                                    )
                                if thinking_response.strip():
                                    _terminal_metrics["thinking"] = thinking_response.strip()
                                _commit_chat_compaction(_actual_candidate_index)
                                _saved_id = save_assistant_response(
                                    sess,
                                    session_manager,
                                    session,
                                    _terminal_content,
                                    _terminal_metrics,
                                    character_name=ctx.preset.character_name,
                                    incognito=incognito,
                                )
                                accumulate_token_usage(session, _terminal_metrics)
                                _chat_terminal_saved = True
                                _stream_set(session, status="error")
                                if _saved_id:
                                    yield f'data: {json.dumps({"type": "message_saved", "id": _saved_id})}\n\n'
                                yield f'data: {json.dumps({"type": "chat_terminal", "data": _terminal_metrics})}\n\n'
                            yield chunk
                        elif chunk.startswith("event: "):
                            yield chunk
                        elif chunk == "data: [DONE]\n\n":
                            if _chat_terminal_saved:
                                # Some providers append DONE after a terminal
                                # error.  The failed partial is already saved;
                                # never re-save/post-process it as a success or
                                # advertise successful completion to the client.
                                continue
                            # Generate fallback metrics if LLM didn't send usage
                            if not last_metrics and full_response:
                                _elapsed = time.time() - _chat_start
                                _est_out = len(full_response) // 4
                                _tps = round(_est_out / _elapsed, 2) if _elapsed > 0 else 0
                                _actual_context_length = _chat_request_state["context_lengths"].get(
                                    _actual_candidate_index,
                                    _selected_context_length,
                                )
                                _actual_request_messages = _chat_request_state["requests"].get(
                                    _actual_candidate_index,
                                    messages,
                                )
                                _est_in = estimate_tokens(_actual_request_messages)
                                _ctx_pct = min(round((_est_in / _actual_context_length) * 100, 1), 100.0) if _actual_context_length else 0
                                last_metrics = {
                                    "response_time": round(_elapsed, 2),
                                    "input_tokens": _est_in,
                                    "output_tokens": _est_out,
                                    "tokens_per_second": _tps,
                                    "request_context_tokens": _est_in,
                                    "context_percent": _ctx_pct,
                                    "context_length": _actual_context_length,
                                    "model": _actual_model or _answered_by or _requested_model,
                                    "requested_model": _requested_model,
                                    "requested_endpoint_id": _requested_route.get("endpoint_id"),
                                    "requested_endpoint_label": _requested_route.get("endpoint_label"),
                                    "endpoint_id": _actual_route.get("endpoint_id"),
                                    "endpoint_label": _actual_route.get("endpoint_label"),
                                    "usage_source": "estimated",
                                }
                                if isinstance(
                                    _actual_route.get("endpoint_cost_tracked"),
                                    bool,
                                ):
                                    last_metrics["endpoint_cost_tracked"] = _actual_route.get(
                                        "endpoint_cost_tracked"
                                    )
                                yield f'data: {json.dumps({"type": "metrics", "data": last_metrics})}\n\n'
                            if full_response:
                                _commit_chat_compaction(_actual_candidate_index)
                                _metrics_to_save = dict(last_metrics or {})
                                if thinking_response.strip() and not _metrics_to_save.get("thinking"):
                                    _metrics_to_save["thinking"] = thinking_response.strip()
                                _saved_id = save_assistant_response(
                                    sess, session_manager, session, full_response, _metrics_to_save,
                                    character_name=ctx.preset.character_name,
                                    web_sources=web_sources,
                                    rag_sources=ctx.rag_sources,
                                    research_sources=research_sources,
                                    used_memories=ctx.used_memories,
                                    do_research=effective_do_research,
                                    incognito=incognito,
                                )
                                if _saved_id:
                                    yield f'data: {json.dumps({"type": "message_saved", "id": _saved_id})}\n\n'
                                run_post_response_tasks(
                                    sess, session_manager, session, message, full_response,
                                    _metrics_to_save, ctx.uprefs, memory_manager, memory_vector, webhook_manager,
                                    incognito=incognito, compare_mode=compare_mode,
                                    character_name=ctx.preset.character_name,
                                    owner=_user,
                                    allow_background_extraction=(
                                        not tool_policy.block_all_tool_calls
                                        and not tool_approval_continuation
                                    ),
                                )
                            _stream_set(session, status="done")
                            yield chunk
                except (asyncio.CancelledError, GeneratorExit):
                    if full_response and not incognito:
                        logger.info("Client disconnected mid-stream (chat mode) for session %s, saving partial (%d chars)", session, len(full_response))
                        _stopped_content, _stopped_md = clean_thinking_for_save(
                            full_response,
                            {
                                "stopped": True,
                                "model": _actual_model or _answered_by or _requested_model,
                                "requested_model": _requested_model,
                                "endpoint_id": _actual_route.get("endpoint_id"),
                                "endpoint_label": _actual_route.get("endpoint_label"),
                                "requested_endpoint_id": _requested_route.get("endpoint_id"),
                                "requested_endpoint_label": _requested_route.get("endpoint_label"),
                            },
                        )
                        sess.add_message(ChatMessage("assistant", _stopped_content, metadata=_stopped_md))
                        session_manager.save_sessions()
                    raise
                finally:
                    _active_streams.pop(session, None)
            else:
                # ── Agent mode: full agent loop with tools ──
                _agent_rounds = 0
                _agent_tool_calls = 0
                _answered_by = None  # set if the selected model failed and a fallback answered
                _requested_model = sess.model
                _actual_model = None
                _agent_requested_route = _foreground_route_descriptors[0]
                _agent_actual_endpoint_id = _agent_requested_route.get("endpoint_id")
                _agent_actual_endpoint_label = _agent_requested_route.get("endpoint_label")
                _agent_round_models = {1: _requested_model}
                _agent_round_endpoint_ids = {1: _agent_actual_endpoint_id}
                _agent_round_endpoint_labels = {1: _agent_actual_endpoint_label}
                try:
                    from src.settings import get_setting
                    from src.agent_tools import MAX_AGENT_ROUNDS as _DEFAULT_ROUNDS
                    # Per-message tool budget from settings; guard defensively in
                    # case settings.json was hand-edited to a non-numeric value
                    # (the HTTP admin endpoint validates, but direct edits bypass
                    # it). 0 = unlimited, matching auth_routes set_settings().
                    try:
                        _tool_budget = int(get_setting("agent_max_tool_calls", 0))
                    except (TypeError, ValueError):
                        _tool_budget = 0
                    # Per-message round cap from settings; clamp defensively in
                    # case settings.json was hand-edited to a bad value.
                    try:
                        _max_rounds = int(get_setting("agent_max_rounds", _DEFAULT_ROUNDS) or _DEFAULT_ROUNDS)
                    except (TypeError, ValueError):
                        _max_rounds = _DEFAULT_ROUNDS
                    _max_rounds = max(1, min(_max_rounds, 200))

                    _forced_tools = None
                    if _search_enabled:
                        _forced_tools = set(WEB_TOOL_NAMES)
                        if _explicit_browser_intent:
                            _forced_tools |= set(_BROWSER_MCP_TOOLS)
                    elif _explicit_browser_intent:
                        _forced_tools = set(_BROWSER_MCP_TOOLS)

                    try:
                        from services.projects import project_for_session
                        if project_for_session(session, _user):
                            _forced_tools = set(_forced_tools or set())
                            _forced_tools.update({"project_context", "search_project_chats"})
                    except Exception:
                        pass
                    # /agents (multi-agent delegation) names the tool explicitly;
                    # make sure retrieval cannot drop it.
                    if isinstance(message, str) and "delegate_agents" in message:
                        _forced_tools = set(_forced_tools or set())
                        _forced_tools.add("delegate_agents")
                    if _delegate_tasks and not tool_approval_continuation:
                        _forced_tools = set(_forced_tools or set())
                        _forced_tools.add("delegate_agents")
                        # The user saw (and history keeps) the compact line; the
                        # model gets the explicit delegation instruction.
                        _instr = _delegation_instruction(_delegate_tasks)
                        messages = list(messages)
                        for _mi in range(len(messages) - 1, -1, -1):
                            if messages[_mi].get("role") == "user":
                                messages[_mi] = dict(messages[_mi])
                                messages[_mi]["content"] = _instr
                                break

                    async for chunk in stream_agent_loop(
                        sess.endpoint_url,
                        sess.model,
                        messages,
                        headers=sess.headers,
                        temperature=ctx.preset.temperature,
                        max_tokens=ctx.preset.max_tokens,
                        prompt_type=preset_id,
                        max_tool_calls=_tool_budget,
                        max_rounds=_max_rounds,
                        context_length=_selected_context_length,
                        active_document=active_doc,
                        active_email=active_email_ctx,
                        session_id=session,
                        history_session=sess,
                        disabled_tools=disabled_tools if disabled_tools else None,
                        tool_policy=tool_policy,
                        owner=_user,
                        fallbacks=_foreground_candidates[1:],
                        route_descriptors=_foreground_route_descriptors,
                        fallback_statuses=_foreground_policy.eligible_statuses,
                        fallback_on_empty=_foreground_policy.fallback_on_empty,
                        plan_mode=plan_mode,
                        approved_plan=approved_plan or None,
                        workspace=workspace or None,
                        workspace_roots=_project_roots or None,
                        relevant_tools=(
                            set(pending_tool_approval.selected_tools)
                            if exact_tool_approval
                            and pending_tool_approval
                            and pending_tool_approval.selected_tools
                            else None
                        ),
                        forced_tools=_forced_tools,
                        uploaded_files=ctx.uploaded_files,
                        defer_context_shaping=_foreground_policy.enabled,
                        external_untrusted_context_seen=external_untrusted_context_seen,
                        exact_approval=exact_tool_approval,
                        temperature_explicit=_temperature_explicit,
                        gen_overrides=_gen_overrides or None,
                        harness_options=_harness_options or None,
                    ):
                        if chunk.startswith("data: ") and not chunk.startswith("data: [DONE]"):
                            try:
                                data = json.loads(chunk[6:])
                                if "delta" in data:
                                    # Reasoning tokens arrive flagged thinking:true.
                                    # Forward them for the live indicator, but keep
                                    # them out of the saved reply (same as chat mode).
                                    if data.get("thinking"):
                                        thinking_response += data["delta"]
                                    else:
                                        full_response += data["delta"]
                                        _stream_set(session, partial=full_response)
                                    yield chunk
                                elif data.get("type") == "web_sources":
                                    web_sources = data.get("data", [])
                                    yield chunk
                                elif data.get("type") in (
                                    "tool_start", "tool_output", "agent_step",
                                    # live progress of a running tool: bash/python
                                    # stdout tail, image steps and the sub-agent
                                    # worker board (delegate_agents). The agent
                                    # loop emits these while the tool is in
                                    # flight; without this entry they were
                                    # silently dropped here and the UI never saw
                                    # them (board stayed empty, tails stayed blind).
                                    "tool_progress",
                                    # live browser frame after each browser
                                    # action (src/browser_view.py → Browser panel)
                                    "browser_view",
                                    "doc_stream_open", "doc_stream_delta",
                                    "doc_update", "doc_suggestions", "ui_control",
                                    "rounds_exhausted", "budget_exceeded",
                                    "loop_breaker_triggered",
                                    "intent_nudge_exhausted",
                                    "ask_user",
                                    "plan_update",
                                    # reliability harness (src/agent_harness.py)
                                    "round_info", "harness_check", "harness_summary",
                                    "progress_update",
                                    # context ledger (src/context_ledger.py)
                                    "context_ledger",
                                    # multi-agent delegation
                                    "subagent_event",
                                ):
                                    if data.get("type") == "agent_step":
                                        _event_round = data.get("round", 1)
                                        _agent_rounds = max(_agent_rounds, _event_round)
                                        _agent_round_models.setdefault(
                                            _event_round,
                                            _actual_model or _answered_by or _requested_model,
                                        )
                                        _agent_round_endpoint_ids.setdefault(
                                            _event_round,
                                            _agent_actual_endpoint_id,
                                        )
                                        _agent_round_endpoint_labels.setdefault(
                                            _event_round,
                                            _agent_actual_endpoint_label,
                                        )
                                    elif data.get("type") == "tool_start":
                                        _agent_tool_calls += 1
                                    yield chunk
                                elif data.get("type") == "fallback":
                                    # Selected model failed; a fallback answered.
                                    # Forward the notice and remember the real
                                    # model so metrics reflect it, not the masked
                                    # selected model.
                                    _answered_by = data.get("answered_by") or _answered_by
                                    _actual_model = _answered_by or _actual_model
                                    if "answered_by_endpoint_id" in data:
                                        _agent_actual_endpoint_id = data.get("answered_by_endpoint_id")
                                    if data.get("answered_by_endpoint_label"):
                                        _agent_actual_endpoint_label = data.get("answered_by_endpoint_label")
                                    _event_round = data.get("round") or max(_agent_rounds, 1)
                                    _agent_round_models[_event_round] = _answered_by or _requested_model
                                    _agent_round_endpoint_ids[_event_round] = _agent_actual_endpoint_id
                                    _agent_round_endpoint_labels[_event_round] = _agent_actual_endpoint_label
                                    data["selected_model"] = data.get("selected_model") or _requested_model
                                    yield chunk
                                elif data.get("type") == "model_actual":
                                    _actual_model = data.get("model") or _actual_model
                                    if "endpoint_id" in data:
                                        _agent_actual_endpoint_id = data.get("endpoint_id")
                                    if data.get("endpoint_label"):
                                        _agent_actual_endpoint_label = data.get("endpoint_label")
                                    _event_round = data.get("round") or max(_agent_rounds, 1)
                                    _agent_round_models[_event_round] = _actual_model or _requested_model
                                    _agent_round_endpoint_ids[_event_round] = _agent_actual_endpoint_id
                                    _agent_round_endpoint_labels[_event_round] = _agent_actual_endpoint_label
                                    data["requested_model"] = _requested_model
                                    yield f'data: {json.dumps(data)}\n\n'
                                elif data.get("type") == "agent_terminal":
                                    terminal_metadata = dict(data.get("data") or {})
                                    last_metrics = terminal_metadata
                                    failure = terminal_metadata.get("failure") or {}
                                    failure_status = _normalize_http_status(
                                        failure.get("status")
                                    )
                                    failure_message = (
                                        f"Model request failed (HTTP {failure_status})"
                                        if failure_status is not None
                                        else "Model request failed"
                                    )
                                    terminal_metadata["failure"] = {
                                        "status": failure_status,
                                        "message": failure_message,
                                    }
                                    terminal_content = full_response.strip()
                                    failure_note = f"[Agent stopped: {failure_message}]"
                                    if terminal_content:
                                        terminal_content = f"{terminal_content}\n\n{failure_note}"
                                    else:
                                        terminal_content = failure_note
                                    if not _terminal_saved:
                                        _saved_id = save_assistant_response(
                                            sess,
                                            session_manager,
                                            session,
                                            terminal_content,
                                            terminal_metadata,
                                            character_name=ctx.preset.character_name,
                                            web_sources=web_sources,
                                            rag_sources=ctx.rag_sources,
                                            used_memories=ctx.used_memories,
                                            incognito=incognito,
                                        )
                                        _terminal_saved = True
                                        accumulate_token_usage(session, terminal_metadata)
                                        _stream_set(session, status="error")
                                        if _saved_id:
                                            yield f'data: {json.dumps({"type": "message_saved", "id": _saved_id})}\n\n'
                                    yield chunk
                                elif data.get("type") == "metrics":
                                    last_metrics = data.get("data", {})
                                    _reported_model = last_metrics.get("model")
                                    last_metrics["requested_model"] = last_metrics.get("requested_model") or _requested_model
                                    last_metrics["model"] = _reported_model or _actual_model or _answered_by or _requested_model
                                    if ctx.context_trimmed:
                                        last_metrics["context_trimmed"] = True
                                        last_metrics["context_messages_before_trim"] = ctx.context_messages_before_trim
                                        last_metrics["context_messages_after_trim"] = ctx.context_messages_after_trim
                                        last_metrics["context_tokens_before_trim"] = ctx.context_tokens_before_trim
                                        last_metrics["context_tokens_after_trim"] = ctx.context_tokens_after_trim
                                    _metrics_event = {"type": "metrics", "data": last_metrics}
                                    # Inline teacher escalation marks its
                                    # recursively emitted events at the SSE
                                    # envelope. Preserve that non-secret marker
                                    # when normalizing metrics so the browser's
                                    # replay-stable ledger keeps primary and
                                    # teacher segments distinct.
                                    if data.get("teacher") is True:
                                        _metrics_event["teacher"] = True
                                    yield f'data: {json.dumps(_metrics_event)}\n\n'
                            except json.JSONDecodeError:
                                yield chunk
                        elif chunk.startswith("event: "):
                            yield chunk
                        elif chunk == "data: [DONE]\n\n":
                            _has_tool_events = bool((last_metrics or {}).get("tool_events"))
                            if full_response or _has_tool_events:
                                _response_to_save = full_response or "Done."
                                _metrics_to_save = dict(last_metrics or {})
                                if thinking_response.strip() and not _metrics_to_save.get("thinking"):
                                    _metrics_to_save["thinking"] = thinking_response.strip()
                                _saved_id = save_assistant_response(
                                    sess, session_manager, session, _response_to_save, _metrics_to_save,
                                    character_name=ctx.preset.character_name,
                                    web_sources=web_sources,
                                    rag_sources=ctx.rag_sources,
                                    used_memories=ctx.used_memories,
                                    incognito=incognito,
                                )
                                if _saved_id:
                                    yield f'data: {json.dumps({"type": "message_saved", "id": _saved_id})}\n\n'
                                    # Project audit trail + review-mode state for
                                    # a turn that changed files (linked to this
                                    # saved message so the UI can jump to it).
                                    try:
                                        _record_turn_side_effects(
                                            session, _saved_id, _metrics_to_save, message,
                                            _harness_options if isinstance(_harness_options, dict) else {},
                                        )
                                    except Exception as _se_err:
                                        logger.debug("turn side effects failed: %s", _se_err)
                                run_post_response_tasks(
                                    sess, session_manager, session, message, _response_to_save,
                                    _metrics_to_save, ctx.uprefs, memory_manager, memory_vector, webhook_manager,
                                    incognito=incognito, compare_mode=compare_mode,
                                    character_name=ctx.preset.character_name,
                                                            agent_rounds=_agent_rounds,
                                    agent_tool_calls=_agent_tool_calls,
                                    skills_manager=skills_manager,
                                    owner=_user,
                                    extract_skills=(
                                        user_requested_agent
                                        and not tool_approval_continuation
                                    ),
                                    allow_background_extraction=(
                                        not tool_policy.block_all_tool_calls
                                        and not tool_approval_continuation
                                    ),
                                )
                            _stream_set(session, status="done")
                            yield chunk
                except (asyncio.CancelledError, GeneratorExit):
                    # Client disconnected — save partial response. Wrap
                    # the save in its own try so an exception inside
                    # add_message / save_sessions doesn't mask the
                    # original CancelledError (which prevented the
                    # outer finally from running and left _active_streams
                    # with a stale entry).
                    try:
                        if full_response and not incognito:
                            logger.info("Client disconnected mid-stream for session %s, saving partial response (%d chars)", session, len(full_response))
                            _stopped_content2, _stopped_md2 = clean_thinking_for_save(
                                full_response,
                                {
                                    "stopped": True,
                                    "model": _actual_model or _answered_by or _requested_model,
                                    "requested_model": _requested_model,
                                    "endpoint_id": _agent_actual_endpoint_id,
                                    "endpoint_label": _agent_actual_endpoint_label,
                                    "requested_endpoint_id": _agent_requested_route.get("endpoint_id"),
                                    "requested_endpoint_label": _agent_requested_route.get("endpoint_label"),
                                    "round_models": [
                                        _agent_round_models.get(i, _actual_model or _requested_model)
                                        for i in range(1, max(_agent_round_models, default=1) + 1)
                                    ],
                                    "round_endpoint_ids": [
                                        _agent_round_endpoint_ids.get(i)
                                        for i in range(1, max(_agent_round_models, default=1) + 1)
                                    ],
                                    "round_endpoint_labels": [
                                        _agent_round_endpoint_labels.get(i)
                                        for i in range(1, max(_agent_round_models, default=1) + 1)
                                    ],
                                },
                            )
                            sess.add_message(ChatMessage("assistant", _stopped_content2, metadata=_stopped_md2))
                            session_manager.save_sessions()
                    except Exception:
                        logger.exception("Failed to save partial response on disconnect (session %s)", session)
                    raise
                finally:
                    _active_streams.pop(session, None)

        async def _safe_stream() -> AsyncGenerator[str, None]:
            """Wrapper that guarantees _active_streams cleanup even if stream_with_save
            raises before reaching a mode-specific finally block."""
            try:
                async for chunk in stream_with_save():
                    yield chunk
            finally:
                _active_streams.pop(session, None)

        # Compare panes are short-lived, single-shot generations whose sessions
        # exist only to drive that one pane — there's nothing to "resume" and
        # the user expects the pane's Stop button (which aborts the fetch,
        # closing this SSE) to promptly cancel the upstream LLM call. Detaching
        # them would keep burning upstream tokens/compute after the pane is
        # stopped or the comparison is abandoned, and would surface a stale
        # "still streaming" /resume target for a session nobody will revisit.
        #
        # So: stream them directly (no agent_runs wrapping). Starlette cancels
        # the underlying async generator (raising CancelledError/GeneratorExit
        # inside it) as soon as it notices the client disconnected — which the
        # mode-specific except blocks above already handle by saving the
        # partial response exactly once. This stops the upstream call promptly
        # without waiting on the next streamed chunk.
        #
        # Normal chat/agent streams keep the DETACHED behavior below: they
        # survive the client closing the tab / navigating away. The SSE response just subscribes (replay
        # buffered output + live); dropping the SSE only removes a subscriber —
        # the run keeps going and saves the assistant message on completion
        # regardless. Reconnect via /api/chat/resume.
        if compare_mode:
            return StreamingResponse(_safe_stream(), media_type="text/event-stream")

        # Task queue: runs on a local endpoint share one lane (one GPU, one
        # generation at a time by default); API endpoints run immediately
        # unless agent_queue_api_concurrency is set. Queued runs stream a live
        # `queue_status` position and start on their own when the lane frees.
        _lane = None
        try:
            from src.model_context import is_local_endpoint as _is_local_ep
            if _is_local_ep(sess.endpoint_url):
                _lane = "local"
            else:
                from src.settings import get_setting as _gs_queue
                if int(_gs_queue("agent_queue_api_concurrency", 0) or 0) > 0:
                    _lane = "api"
        except Exception:
            _lane = None
        _run_label = (getattr(sess, "name", "") or "").strip() or " ".join(str(message or "").split())[:60]
        _detached_run = agent_runs.start(session, _safe_stream(), lane=_lane, label=_run_label[:80])
        return StreamingResponse(
            agent_runs.subscribe(session, _detached_run),
            media_type="text/event-stream",
            headers={"X-Odysseus-Run-Id": _detached_run.run_id},
        )

    # ------------------------------------------------------------------ #
    # GET /api/chat/resume — reconnect to a detached run that's still going
    # (e.g. after reopening a session whose agent kept running in the background)
    # ------------------------------------------------------------------ #
    @router.get("/api/chat/resume/{session_id}")
    async def chat_resume(request: Request, session_id: str) -> StreamingResponse:
        _verify_session_owner(request, session_id)
        _active_run = agent_runs.get_active_run(session_id)
        if _active_run is None:
            raise HTTPException(404, "No active run for this session")
        return StreamingResponse(
            agent_runs.subscribe(session_id, _active_run),
            media_type="text/event-stream",
            headers={"X-Odysseus-Run-Id": _active_run.run_id},
        )

    # ------------------------------------------------------------------ #
    # POST /api/chat/stop — cancel a detached run (Stop button). Closing the SSE
    # no longer stops it (it's detached), so the Stop button must call this.
    # ------------------------------------------------------------------ #
    @router.post("/api/chat/stop/{session_id}")
    async def chat_stop(request: Request, session_id: str) -> Dict[str, Any]:
        _verify_session_owner(request, session_id)
        _expected_run_id = request.headers.get("X-Odysseus-Run-Id")
        stopped = agent_runs.stop(session_id, _expected_run_id)
        return {"stopped": stopped}

    # ------------------------------------------------------------------ #
    # POST /api/chat/subagent/stop/{child_session_id} — stop ONE worker of a
    # delegate_agents run (the coordinator keeps going with the others).
    # ------------------------------------------------------------------ #
    @router.post("/api/chat/subagent/stop/{child_session_id}")
    async def subagent_stop(request: Request, child_session_id: str) -> Dict[str, Any]:
        _verify_session_owner(request, child_session_id)
        from src.agent_tools.subagent_tools import stop_worker
        return {"stopped": stop_worker(child_session_id)}

    # ------------------------------------------------------------------ #
    # POST /api/chat/subagent/steer/{child_session_id} — send a steering
    # message to ONE running worker. Body: {"text": "..."}. The worker's agent
    # loop injects it as a user message before its next round (the board
    # shows a `steer` event when that happens). 404 when the worker is not
    # active, 400 for an empty text.
    # ------------------------------------------------------------------ #
    @router.post("/api/chat/subagent/steer/{child_session_id}")
    async def subagent_steer(request: Request, child_session_id: str) -> Dict[str, Any]:
        _verify_session_owner(request, child_session_id)
        try:
            body = await request.json()
        except Exception:
            body = None
        text = str((body or {}).get("text") or "").strip() if isinstance(body, dict) else ""
        if not text:
            raise HTTPException(400, "A non-empty 'text' is required")
        from src.agent_tools.subagent_tools import active_worker_ids, steer_worker
        if child_session_id not in active_worker_ids():
            raise HTTPException(404, "No active sub-agent worker for this session")
        if not steer_worker(child_session_id, text[:4000], source="user"):
            raise HTTPException(404, "No active sub-agent worker for this session")
        return {"ok": True}

    # ------------------------------------------------------------------ #
    # GET /api/chat/activity — sidebar status dots in one call: sessions with a
    # detached run still going and sessions parked on an approval card. The
    # client keeps "finished but unread" itself (it knows what was viewed).
    # ------------------------------------------------------------------ #
    @router.get("/api/chat/activity")
    async def chat_activity(request: Request) -> Dict[str, Any]:
        owner = effective_user(request)
        running: List[str] = []
        runs: Dict[str, str] = {}   # session → opaque run id (needed to Stop from the sidebar)
        try:
            for sid in agent_runs.active_session_ids():
                try:
                    _verify_session_owner(request, sid, session_manager)
                except HTTPException:
                    continue  # another user's run (or a vanished session)
                running.append(sid)
                rid = agent_runs.get_run_id(sid)
                if rid and agent_runs.is_active(sid):
                    runs[sid] = rid
        except Exception:
            running = []
        try:
            awaiting = tool_approval_store.pending_session_ids(owner=owner)
        except Exception:
            awaiting = []
        # Queue positions of runs waiting for their lane + runs a restart cut
        # short (until the client acknowledges them).
        queued: Dict[str, int] = {}
        interrupted: List[Dict[str, Any]] = []
        try:
            for sid, pos in agent_runs.queued_positions().items():
                if sid in running:
                    queued[sid] = pos
            for entry in agent_runs.interrupted_runs():
                try:
                    _verify_session_owner(request, entry["session_id"], session_manager)
                except HTTPException:
                    continue
                interrupted.append(entry)
        except Exception:
            pass
        # Live delegate_agents workers (the control board): child session →
        # {parent, name, role, started_at, round, last_event_at, stalled,
        # tool_calls}, from the registry subagent_tools keeps.
        workers: Dict[str, Dict[str, Any]] = {}
        try:
            from src.agent_tools.subagent_tools import worker_board
            for sid, card in worker_board().items():
                if sid not in running:
                    try:
                        _verify_session_owner(request, sid, session_manager)
                    except HTTPException:
                        continue
                workers[sid] = card
        except Exception:
            workers = {}
        return {"running": running, "runs": runs, "awaiting_approval": awaiting, "queued": queued,
                "interrupted": interrupted, "workers": workers, "ts": time.time()}

    @router.post("/api/chat/interrupted/ack")
    async def chat_interrupted_ack(request: Request) -> Dict[str, Any]:
        """The client has shown the "interrupted by a restart" notice."""
        effective_user(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        sid = str((body or {}).get("session_id") or "") or None
        return {"cleared": agent_runs.acknowledge_interrupted(sid)}

    # ------------------------------------------------------------------ #
    # GET /api/chat/stream_status — check if a stream is active for a session
    # ------------------------------------------------------------------ #
    @router.get("/api/chat/stream_status/{session_id}")
    async def chat_stream_status(request: Request, session_id: str) -> Dict[str, Any]:
        _verify_session_owner(request, session_id)
        # A detached run can still be going even if _active_streams was popped;
        # report it as active so the client knows to reconnect via /resume.
        # Read once via .get() to avoid a KeyError race between the membership
        # check and the indexed read if a sibling stream's finally pops the
        # entry in between (same pattern _stream_set already uses).
        rec = _active_streams.get(session_id)
        if rec is None:
            if agent_runs.is_active(session_id):
                return {"status": "streaming", "detached": True}
            raise HTTPException(404, "No active stream for this session")
        return rec

    # ------------------------------------------------------------------ #
    # POST /api/inject_context
    # ------------------------------------------------------------------ #
    @router.post("/api/inject_context/{session_id}")
    async def inject_context(request: Request, session_id: str, context: str = Form(...)) -> Dict[str, str]:
        _verify_session_owner(request, session_id)
        try:
            sess = session_manager.get_session(session_id)
            msg = untrusted_context_message("injected research context", f"Research Context: {context}")
            sess.add_message(ChatMessage(msg["role"], msg["content"], metadata=msg.get("metadata")))
            session_manager.save_sessions()
            return {"status": "context_injected"}
        except KeyError:
            raise HTTPException(404, "Session not found")

    # ------------------------------------------------------------------ #
    # GET /api/search — search across chat messages
    # ------------------------------------------------------------------ #
    @router.get("/api/search")
    async def search_messages(
        request: Request,
        q: str = Query("", min_length=0),
        limit: int = Query(20, ge=1, le=100),
    ) -> List[Dict[str, Any]]:
        if not q or not q.strip():
            return []

        _user = effective_user(request)
        return [
            result.to_dict()
            for result in search_session_messages(
                q,
                limit=limit,
                owner=_user,
                restrict_owner=_user is not None,
                include_legacy_owner=False,
            )
        ]

    # ------------------------------------------------------------------ #
    # POST /api/rewrite — lightweight rewrite of last AI message (no tools)
    # ------------------------------------------------------------------ #
    @router.post("/api/rewrite")
    async def rewrite_message(request: Request) -> StreamingResponse:
        """Rewrite the last AI message with an instruction (shorter/simpler/etc).

        Unlike the full chat pipeline, this does NOT run the agent loop or tools.
        It just asks the LLM to rewrite the given text.
        """
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "Invalid JSON")

        session_id = body.get("session_id")
        original_text = body.get("original_text", "")
        instruction = body.get("instruction", "")

        if not session_id or not original_text or not instruction:
            raise HTTPException(400, "session_id, original_text, and instruction are required")

        _verify_session_owner(request, session_id)

        try:
            sess = session_manager.get_session(session_id)
        except (KeyError, SessionNotFoundError):
            raise HTTPException(404, "Session not found")

        messages = [
            {"role": "system", "content": (
                "You are rewriting a previous response. Follow the instruction exactly. "
                "Output ONLY the rewritten text — no preamble, no explanation, no meta-commentary. "
                "Preserve any formatting (markdown, code blocks, lists) from the original."
            )},
            {"role": "user", "content": (
                f"Here is the original response:\n\n{original_text}\n\n"
                f"Instruction: {instruction}"
            )},
        ]

        async def stream_rewrite() -> AsyncGenerator[str, None]:
            full_response = ""
            try:
                async for chunk in stream_llm(
                    sess.endpoint_url,
                    sess.model,
                    messages,
                    headers=sess.headers,
                    temperature=0.7,
                    # 0 = let the server decide (no cap). A hardcoded 4096 made
                    # local reasoning models (Qwen3 / R1) burn the whole budget
                    # inside <think> and emit no rewrite — the bubble just hung
                    # on "Rewriting...". Same fix as the chat max_tokens cap.
                    max_tokens=0,
                    tools=None,
                ):
                    if chunk.startswith("data: ") and not chunk.startswith("data: [DONE]"):
                        try:
                            data = json.loads(chunk[6:])
                            if "delta" in data:
                                # Forward the chunk (so the client can show a
                                # thinking indicator) but DON'T fold reasoning
                                # tokens into the saved rewrite — only real
                                # content. reasoning_content arrives flagged
                                # with thinking:true.
                                if not data.get("thinking"):
                                    full_response += data["delta"]
                                yield chunk
                        except json.JSONDecodeError:
                            yield chunk
                    elif chunk.startswith("event: "):
                        yield chunk
                    elif chunk == "data: [DONE]\n\n":
                        # Update the last assistant message in session history.
                        # Strip reasoning-model <think> blocks so the persisted
                        # rewrite is just the rewritten text, not its scratchpad.
                        from src.research_utils import strip_thinking
                        full_response = strip_thinking(full_response).strip() or full_response
                        if full_response:
                            for msg in reversed(sess.history):
                                if (isinstance(msg, ChatMessage) and msg.role == 'assistant') or \
                                   (isinstance(msg, dict) and msg.get('role') == 'assistant'):
                                    if isinstance(msg, ChatMessage):
                                        msg.content = full_response
                                    else:
                                        msg['content'] = full_response
                                    break
                            # Update in DB too
                            db = SessionLocal()
                            try:
                                db_msg = (
                                    db.query(DBChatMessage)
                                    .filter(DBChatMessage.session_id == session_id, DBChatMessage.role == 'assistant')
                                    .order_by(DBChatMessage.timestamp.desc())
                                    .first()
                                )
                                if db_msg:
                                    db_msg.content = full_response
                                    db.commit()
                            except Exception as e:
                                logger.warning("Failed to update rewritten message in DB: %s", e)
                                db.rollback()
                            finally:
                                db.close()
                            session_manager.save_sessions()
                        yield chunk
            except Exception as e:
                logger.error("Rewrite stream error: %s", e)
                yield f'event: error\ndata: {json.dumps({"error": str(e), "status": 500})}\n\n'

        return StreamingResponse(stream_rewrite(), media_type="text/event-stream")

    return router
