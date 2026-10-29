"""Design check in two levels, using the design workshop's rule registry.

When a project turns it on, the agent's edits to UI sources (.tsx, .jsx, .css,
.html, .vue) are checked by the workshop's rules:

* after each edit, only the unambiguous mechanical rules (engine `static`,
  tier Floor), read from the file alone, with no browser. The agent gets one
  short note in the tool result;
* when the turn is about to close, the full pass once: every static rule plus
  a real render and the browser rules for each page the turn touched. A
  blocking result becomes one fix round, never more than one per turn, and the
  same set of files and exceptions is never checked twice.

The workshop is only ever asked, never started: when it is stopped both levels
stay silent. Exceptions are kept per project, each with a reason.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence

from . import client, source
from .source import DESIGN_EXTS, is_design_file

logger = logging.getLogger(__name__)

LISTENER_ID = "design_hook"
MIN_REASON = 24
MAX_FILES = 12
MAX_PAGES = 4
MAX_NOTE_FINDINGS = 4
MAX_PASS_FINDINGS = 8
# Rules that speak about a whole page, not about the file that holds a piece of it: one needs what a browser
# measured (the language of <html>), the other the styles of the whole document. On a source file read on its
# own they would fire whatever the file says, so they are left to the render of a page.
_PAGE_ONLY_RULES = frozenset({"a11y.no-lang", "layout.no-max-width"})
_PAGE_SKIP = {"node_modules", ".git", "dist", "build", ".next", "venv", ".venv", "__pycache__"}

_LOCK = threading.Lock()
_SESSIONS: Dict[str, Dict[str, Any]] = {}

__all__ = ["DESIGN_EXTS", "is_design_file", "install", "uninstall", "check", "closing_pass", "options_for", "start_module",
           "fix_rounds", "note_fix_round", "sanitize_waivers", "status", "LISTENER_ID"]


# ---------------------------------------------------------------------------
# Per-project settings
# ---------------------------------------------------------------------------

def sanitize_waivers(raw: Any) -> List[Dict[str, str]]:
    """A project's exceptions: [{rule, reason}], each with a reason of at least 24 characters.

    The workshop refuses a shorter reason and a reason repeated for another rule, so both are refused here too.
    Raises ValueError with the first problem; the project settings turn it into a 400.
    """
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        raise ValueError("design_waivers must be a list of {rule, reason}")
    out: List[Dict[str, str]] = []
    seen_rules, seen_reasons = set(), set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("every exception needs a rule and a reason")
        rule = str(item.get("rule") or "").strip()
        reason = re.sub(r"\s+", " ", str(item.get("reason") or "")).strip()
        if not rule or len(rule) > 80:
            raise ValueError("an exception needs the id of a rule")
        if len(reason) < MIN_REASON:
            raise ValueError(f"the reason for {rule} needs at least {MIN_REASON} characters")
        if len(reason) > 400:
            raise ValueError(f"the reason for {rule} is too long (max 400 characters)")
        key = re.sub(r"[^a-z0-9\u00e1\u00e9\u00ed\u00f3\u00fa\u00f1\u00fc]+", " ", reason.lower()).strip()
        if rule in seen_rules:
            raise ValueError(f"{rule} appears twice")
        if key in seen_reasons:
            raise ValueError("each exception needs its own reason; one is repeated")
        seen_rules.add(rule)
        seen_reasons.add(key)
        out.append({"rule": rule, "reason": reason})
        if len(out) > 60:
            raise ValueError("at most 60 exceptions per project")
    return out


def options_for(session_id: str = "", owner: Optional[str] = None, project_id: str = "",
                options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """{enabled, waivers, project_id} for a chat's project. Never raises."""
    if options is not None:
        return {"enabled": bool(options.get("design_hook")), "waivers": list(options.get("design_waivers") or []),
                "project_id": options.get("project_id") or project_id}
    try:
        from services import projects
        project = None
        if project_id:
            project = projects.get_store().get(project_id, owner)
        if project is None and session_id:
            project = projects.project_for_session(session_id, owner)
        if not project:
            return {"enabled": False, "waivers": [], "project_id": project_id}
        return {"enabled": bool(project.get("design_hook")), "waivers": list(project.get("design_waivers") or []),
                "project_id": project.get("id") or project_id}
    except Exception:  # noqa: BLE001 - a missing project means "off"
        logger.debug("design_hook: options lookup failed", exc_info=True)
        return {"enabled": False, "waivers": [], "project_id": project_id}


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

def _resolve(workspace: str, path: str) -> Optional[str]:
    """Absolute path of `path` when it is a UI source inside the workspace, else None."""
    if not workspace or not path or not is_design_file(path):
        return None
    root = os.path.realpath(workspace)
    full = os.path.realpath(path if os.path.isabs(path) else os.path.join(root, path))
    if not (full == root or full.startswith(root + os.sep)) or not os.path.isfile(full):
        return None
    return full


def _rel(full: str, root: str) -> str:
    """Path shown to the agent: relative to the project, always with forward slashes."""
    return (os.path.relpath(full, root) if root else full).replace(os.sep, "/")


def _read(full: str) -> str:
    try:
        with open(full, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def _design_files(workspace: str, paths: Iterable[str]) -> List[str]:
    out: List[str] = []
    for p in paths or []:
        full = _resolve(workspace, str(p))
        if full and full not in out:
            out.append(full)
        if len(out) >= MAX_FILES:
            break
    return out


_CSS_LINK = re.compile(r"<link\b[^>]*>", re.I)


def _inline_css(html: str, page_dir: str, root: str) -> str:
    """Replace local `<link rel=stylesheet>` elements with the stylesheet's text so a stored page renders as it looks."""
    budget = [600_000]

    def swap(m: "re.Match[str]") -> str:
        tag = m.group(0)
        if not re.search(r"rel\s*=\s*[\"']?stylesheet", tag, re.I):
            return tag
        href = re.search(r"href\s*=\s*[\"']([^\"']+)[\"']", tag, re.I)
        if not href or re.match(r"^(?:[a-z][a-z0-9+.-]*:|//)", href.group(1), re.I):
            return tag
        target = os.path.realpath(os.path.join(page_dir, href.group(1).split("?")[0].split("#")[0]))
        if not (target.startswith(root + os.sep)) or not os.path.isfile(target):
            return tag
        css = _read(target)
        if not css or len(css) > budget[0]:
            return tag
        budget[0] -= len(css)
        return f"<style>\n{css}\n</style>"

    return _CSS_LINK.sub(swap, html)


def _pages(workspace: str, files: Sequence[str]) -> List[str]:
    """HTML pages to render: the touched ones, plus pages near the workspace root that link a touched stylesheet."""
    root = os.path.realpath(workspace)
    pages = [f for f in files if f.lower().endswith((".html", ".htm"))]
    sheets = {os.path.basename(f).lower() for f in files if f.lower().endswith(".css")}
    if sheets:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _PAGE_SKIP]
            if dirpath[len(root):].count(os.sep) > 2:
                dirnames[:] = []
            for name in sorted(filenames):
                if not name.lower().endswith((".html", ".htm")):
                    continue
                page = os.path.join(dirpath, name)
                if page in pages:
                    continue
                text = _read(page).lower()
                if any(s in text for s in sheets):
                    pages.append(page)
            if len(pages) >= MAX_PAGES:
                break
    return pages[:MAX_PAGES]


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------

def _waived_rules(waivers: Sequence[Dict[str, str]]) -> set:
    return {str(w.get("rule") or "") for w in waivers or []}


def _blocking(f: Dict[str, Any], waived: set) -> bool:
    tier = f.get("tier")
    if tier == "floor":
        return True
    return tier == "reflex" and not f.get("waived") and str(f.get("check_id") or "") not in waived


def _brief(f: Dict[str, Any], rel: str) -> Dict[str, Any]:
    return {"file": rel, "rule": f.get("check_id"), "tier": f.get("tier"), "engine": f.get("engine"),
            "title": f.get("title"), "detail": str(f.get("detail") or "")[:300],
            "fix": str(f.get("fix") or "")[:300], "evidence": f.get("evidence")}


def check(workspace: str, paths: Iterable[str], *, level: str = "static", waivers: Sequence[Dict[str, str]] = (),
          base: Optional[str] = None) -> Dict[str, Any]:
    """Run the design rules over UI sources. Blocking: call from a thread.

    `level="static"`: Floor rules of the static engine, per file, no browser.
    `level="full"`: every static rule per file, plus a render and the browser rules for each page the files make up.
    Returns {ok, running, level, findings, gate, counts, files, pages, ms}; `ok` is False with `reason` when the
    workshop is stopped or nothing could be read. Never starts the workshop.
    """
    t0 = time.monotonic()
    base = base or client.base_if_running()
    if not base:
        return {"ok": False, "running": False, "reason": "workshop_stopped", "level": level, "findings": [], "gate": "unknown",
                "counts": {}, "files": [], "pages": [], "ms": 0}
    root = os.path.realpath(workspace) if workspace else ""
    files = _design_files(workspace, paths)
    if not files:
        return {"ok": False, "running": True, "reason": "no_design_files", "level": level, "findings": [], "gate": "unknown",
                "counts": {}, "files": [], "pages": [], "ms": 0}
    waived = _waived_rules(waivers)
    pages = _pages(workspace, files) if level == "full" else []
    rendered: List[Dict[str, Any]] = []
    findings: List[Dict[str, Any]] = []
    seen = set()

    def add(rel: str, found: List[Dict[str, Any]]) -> None:
        for f in found:
            key = (rel, f.get("check_id"), str(f.get("evidence"))[:120])
            if key in seen:
                continue
            seen.add(key)
            findings.append(_brief(f, rel))

    file_rows = []
    for full in files:
        rel = _rel(full, root)
        if full in pages:
            continue  # the page's critique runs the static rules too: one result, not two
        html = source.to_html(full, _read(full))
        if html is None:
            file_rows.append({"path": rel, "checked": False, "reason": "unreadable_or_too_large"})
            continue
        found = client.lint_static(base, html)
        if found is None:
            file_rows.append({"path": rel, "checked": False, "reason": "workshop_error"})
            continue
        found = [f for f in found if f.get("check_id") not in _PAGE_ONLY_RULES]
        keep = [f for f in found if (f.get("tier") == "floor" if level == "static" else _blocking(f, waived))]
        add(rel, keep)
        file_rows.append({"path": rel, "checked": True, "rules": len(found)})

    page_rows = []
    for full in pages:
        rel = _rel(full, root)
        html = _inline_css(source.to_html(full, _read(full)) or "", os.path.dirname(full), root) if root else ""
        if not html:
            page_rows.append({"path": rel, "rendered": False, "reason": "unreadable_or_too_large"})
            continue
        render = client.render_page(base, html, title=rel)
        if not render or render.get("error"):
            page_rows.append({"path": rel, "rendered": False, "reason": (render or {}).get("error") or "render_failed"})
            continue
        result = client.critique_render(base, render["id"], [dict(w) for w in waivers])
        if not isinstance(result, dict):
            page_rows.append({"path": rel, "rendered": True, "render_id": render["id"], "critiqued": False})
            continue
        add(rel, [f for f in result.get("findings") or [] if _blocking(f, waived)])
        page_rows.append({"path": rel, "rendered": True, "render_id": render["id"], "gate": (result.get("gate") or {}).get("state"),
                          "rules_run": result.get("rules_run")})
        rendered.append(result)

    counts = {"floor": sum(1 for f in findings if f["tier"] == "floor"), "reflex": sum(1 for f in findings if f["tier"] == "reflex")}
    return {"ok": True, "running": True, "level": level, "findings": findings,
            "gate": "fix" if findings else "pass", "counts": counts, "files": file_rows, "pages": page_rows,
            "ms": int((time.monotonic() - t0) * 1000)}


# ---------------------------------------------------------------------------
# Messages for the agent
# ---------------------------------------------------------------------------

def _lines(findings: List[Dict[str, Any]], limit: int) -> List[str]:
    out = []
    for f in findings[:limit]:
        fix = f" Fix: {f['fix']}" if f.get("fix") else ""
        out.append(f"- {f['file']}: {f['rule']} ({f['tier']}): {f['detail'] or f['title']}{fix}"[:420])
    if len(findings) > limit:
        out.append(f"- ... and {len(findings) - limit} more")
    return out


def note_text(rel: str, findings: List[Dict[str, Any]]) -> str:
    """The short note after an edit."""
    return (f"[Design check] The unambiguous design rules found {len(findings)} problem(s) in {rel}:\n"
            + "\n".join(_lines(findings, MAX_NOTE_FINDINGS))
            + "\nFix them with your next edit of the file; the check runs again after it.")


def pass_message(result: Dict[str, Any]) -> str:
    """The message of the closing pass (a harness note: it is not from the user)."""
    c = result.get("counts") or {}
    pages = [p for p in result.get("pages") or [] if p.get("rendered")]
    where = f" and rendered {len(pages)} page(s)" if pages else " (no page to render: the static rules only)"
    return ("[Design check - automatic message from the runtime, not from the user] Before you finish, the design rules "
            f"checked the UI files you changed{where}. {c.get('floor', 0)} blocking (Floor) and "
            f"{c.get('reflex', 0)} open (Reflex) finding(s):\n" + "\n".join(_lines(result["findings"], MAX_PASS_FINDINGS))
            + "\nFix the Floor ones. A Reflex finding is either fixed or a deliberate choice; if it is deliberate, say which "
              "and why in your answer so the project can record an exception. Do not describe the fix: apply it.")


# ---------------------------------------------------------------------------
# Per-session state
# ---------------------------------------------------------------------------

def _state(session_id: str) -> Dict[str, Any]:
    with _LOCK:
        return _SESSIONS.setdefault(str(session_id), {"notified": {}, "closing_sig": "", "fix_rounds": 0, "last": None})


def fix_rounds(session_id: str) -> int:
    return int(_state(session_id).get("fix_rounds") or 0)


def note_fix_round(session_id: str) -> int:
    st = _state(session_id)
    st["fix_rounds"] = int(st.get("fix_rounds") or 0) + 1
    return st["fix_rounds"]


def _signature(files: Sequence[str], waivers: Sequence[Dict[str, str]]) -> str:
    h = hashlib.sha1()
    for full in sorted(files):
        h.update(full.encode("utf-8", "ignore"))
        h.update(_read(full).encode("utf-8", "ignore"))
    for w in sorted(waivers or [], key=lambda x: str(x.get("rule"))):
        h.update(f"{w.get('rule')}={w.get('reason')}".encode("utf-8", "ignore"))
    return h.hexdigest()


def closing_pass(session_id: str, workspace: str, paths: Iterable[str], *, waivers: Sequence[Dict[str, str]] = ()) -> Dict[str, Any]:
    """The full pass for the turn that is closing. Blocking: call from a thread.

    Runs once for a given set of file contents and exceptions: asked again with nothing changed it answers
    `{ran: False, reason: "unchanged"}`. A blocking result carries `message` for the agent.
    """
    files = _design_files(workspace, paths)
    if not files:
        return {"ran": False, "reason": "no_design_files"}
    st = _state(session_id)
    sig = _signature(files, waivers)
    if st.get("closing_sig") == sig:
        return {"ran": False, "reason": "unchanged"}
    if not client.base_if_running():
        return {"ran": False, "reason": "workshop_stopped"}
    result = check(workspace, files, level="full", waivers=waivers)
    if not result.get("ok"):
        return {"ran": False, "reason": result.get("reason") or "failed"}
    st["closing_sig"] = sig
    st["last"] = {"at": time.time(), "gate": result["gate"], "counts": result["counts"], "ms": result["ms"]}
    out = {"ran": True, "gate": result["gate"], "counts": result["counts"], "findings": result["findings"],
           "pages": result["pages"], "files": result["files"], "ms": result["ms"], "signature": sig}
    if result["gate"] == "fix":
        out["message"] = pass_message(result)
    return out


# ---------------------------------------------------------------------------
# Listeners (lifecycle hooks)
# ---------------------------------------------------------------------------

def on_turn_start(ctx: Dict[str, Any]) -> None:
    session_id = str(ctx.get("session_id") or "")
    if session_id:
        _state(session_id)["fix_rounds"] = 0
    return None


def on_post_tool(ctx: Dict[str, Any]) -> Optional[str]:
    """After an edit of a UI source: the unambiguous rules, one short note when something is open."""
    try:
        return _after_edit(ctx)
    except Exception:  # noqa: BLE001 - a note is never worth a turn
        logger.debug("design_hook: post_tool failed", exc_info=True)
        return None


def _after_edit(ctx: Dict[str, Any]) -> Optional[str]:
    from src.agent_harness import FILE_MUTATION_TOOLS
    if ctx.get("tool") not in FILE_MUTATION_TOOLS:
        return None
    result = ctx.get("result")
    if isinstance(result, dict) and (result.get("error") or result.get("ok") is False):
        return None
    workspace = str(ctx.get("workspace") or "")
    paths = [p for p in (ctx.get("paths") or []) if is_design_file(str(p))]
    if not paths or not workspace:
        return None
    session_id = str(ctx.get("session_id") or "")
    # The turn already resolved the project's settings; the lookup by chat is the fallback for other callers.
    given = {"design_hook": ctx.get("design_hook"), "design_waivers": ctx.get("design_waivers"),
             "project_id": ctx.get("project_id")} if "design_hook" in ctx else None
    opts = options_for(session_id, ctx.get("owner"), str(ctx.get("project_id") or ""), options=given)
    if not opts["enabled"] or not client.base_if_running():
        return None
    st = _state(session_id)
    notes = []
    for full in _design_files(workspace, paths)[:3]:
        result = check(workspace, [full], level="static", waivers=opts["waivers"])
        if not result.get("ok"):
            continue
        rel = _rel(full, os.path.realpath(workspace))
        rules = frozenset(str(f["rule"]) for f in result["findings"])
        if rules == st["notified"].get(full, frozenset()):
            continue
        st["notified"][full] = rules
        if result["findings"]:
            notes.append(note_text(rel, result["findings"]))
    return "\n\n".join(notes) or None


def install() -> None:
    """Register the listeners. Idempotent."""
    from src import lifecycle_hooks as hooks
    hooks.register_listener("turn_start", on_turn_start, listener_id=LISTENER_ID, name="Design check")
    hooks.register_listener("post_tool", on_post_tool, listener_id=LISTENER_ID, name="Design check", threaded=True)


def uninstall() -> None:
    from src import lifecycle_hooks as hooks
    hooks.unregister_listener(LISTENER_ID)


def status(session_id: str = "") -> Dict[str, Any]:
    """Whether the design rules answer, where, and the last closing pass of a chat.

    `running` is true when the rules can be asked now (the workshop answers and its design module is on);
    `workshop_running` and `design_module` say which of the two is missing.
    """
    probe = client.info(force=True)
    out: Dict[str, Any] = {"workshop_url": client.workshop_url(), "running": bool(probe.get("base")),
                           "workshop_running": bool(probe.get("running")), "design_module": probe.get("design"),
                           "extensions": list(DESIGN_EXTS)}
    if session_id:
        out["last_pass"] = _state(session_id).get("last")
    return out


def start_module() -> Dict[str, Any]:
    """Turn the workshop's design module on. Explicit: a button or a tool, never an edit. Blocking."""
    probe = client.start_design_module()
    return {**status(), "started": bool(probe.get("started")), **({"error": probe["error"]} if probe.get("error") else {})}
