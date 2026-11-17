"""Agent loop of one turn: plan -> act -> observe, read from the turn's own stream events.

``agent_runs`` feeds every event a detached run publishes into the ``LoopState`` of
that run. Model text or reasoning is *plan*, a tool call is *act* (with the tool's
name) and a tool result is *observe*. Nothing is inferred from elapsed time: a phase
changes only when an event says so.

Besides the phase, the state keeps the steps the turn took (plan, tools with a short
human summary of their arguments, results with ok/error and time, commit, delivery)
and a checklist of work practices. The checklist depends on the kind of work the
turn actually did (``classify``): code (tests, browser, docs, commit identity, no
evidence files, Agora delivery), research (sources opened and cited, dated facts,
nothing claimed from an unseen source, answer in the user's language), files and
documents (output opened after writing, requested format), operations (commands
finished well and checked afterwards) or a plain conversation, which has none.
Practices that do not apply are left out. The last state of each session stays
readable after the turn ends, as a summary to review.
"""
from __future__ import annotations

import json
import re
import threading
import time
from collections import OrderedDict
from typing import Any, Optional
from urllib.parse import urlparse

MAX_STEPS = 80
MAX_SESSIONS = 64
MAX_URLS = 400
EXPECTED_AUTHOR = "Luissalet"

KINDS = ("code", "research", "files", "ops", "chat")
KIND_LABELS = {
    "code": "Code", "research": "Research", "files": "Files and documents",
    "ops": "Operations", "chat": "Conversation",
}
LABELS = {
    "tests": "Tests run",
    "browser": "App checked in a browser",
    "docs": "Docs updated (FAUSTUS.md / README)",
    "commit": "Commit with the right identity",
    "no_evidence": "No evidence files committed",
    "agora": "Delivered in the Agora (submit / done)",
    "sources_opened": "Sources opened, not only searched",
    "sources_cited": "Sources cited in the answer",
    "dated": "Facts carry a checked date",
    "verified": "Nothing claimed from an unseen source",
    "language": "Answer in the user's language",
    "output_checked": "Output opened and checked",
    "format": "In the requested format",
    "ran_ok": "Commands finished without errors",
    "checked": "Result checked after running",
}
KIND_PRACTICES = {
    "code": ("tests", "browser", "docs", "commit", "no_evidence", "agora"),
    "research": ("sources_opened", "sources_cited", "dated", "verified", "language"),
    "files": ("output_checked", "format", "language"),
    "ops": ("ran_ok", "checked", "language"),
    "chat": (),
}
# Kept for callers that read the code checklist directly.
PRACTICES = tuple((pid, LABELS[pid]) for pid in KIND_PRACTICES["code"])

_TESTS = re.compile(r"\b(pytest|py\.test|unittest|vitest|jest|tox|(?:npm|pnpm|yarn)(?: run)? (?:test|check)|"
                    r"go test|cargo test|node --test)\b", re.I)
_BROWSER = re.compile(r"playwright|puppeteer|chromium|headless|browser", re.I)
_DOCS = re.compile(r"\b(FAUSTUS|README(?:\.es)?|AGENTS)\.md\b", re.I)
_WRITE_TOOL = re.compile(r"write|edit|patch|replace|create_file|str_replace", re.I)
_COMMIT = re.compile(r"\bgit\b[^\n|;&]*\bcommit\b", re.I)
_ADD = re.compile(r"\bgit\b[^\n|;&]*\badd\b", re.I)
_AUTHOR = re.compile(r"--author[= ]+[\"']?([^<\"']+)|user\.name=[\"']?([^\"'\s]+)|GIT_AUTHOR_NAME=[\"']?([^\"'\s]+)")
_TRAILER = re.compile(r"Co-Authored-By:", re.I)
_EVIDENCE = re.compile(r"\.(png|jpe?g|webp|gif|mp4|webm|har)\b|evidenc|screenshot|captura|qa[-_][\w-]*\.json", re.I)
_DELIVERY = re.compile(r"\bagora(?:\.cmd|\.py)?\s[^\n]*\b(?:submit|done)\s+#?\d+|hub_agora_(?:task_)?(?:submit|done)", re.I)

# What a turn does, read from its tools and the paths it touched.
_GIT = re.compile(r"(?:^|[\s;&|(])git\s+\w", re.I)
_AGORA_TASK = re.compile(r"\bagora(?:\.cmd|\.py)?\s[^\n]*\b(?:claim|submit|done|review)\s+#?\d+|hub_agora_task_", re.I)
_CODE_FILE = re.compile(r"\.(py|pyi|ts|tsx|js|jsx|mjs|cjs|css|scss|vue|svelte|go|rs|java|kt|c|cc|cpp|h|hpp|cs|rb|"
                        r"php|sh|ps1|psm1|bat|cmd|sql|toml|ya?ml|json|ini|cfg|lock|gradle|swift)$", re.I)
_DOC_EXT = r"md|txt|docx?|odt|rtf|pdf|xlsx?|ods|csv|tsv|pptx?|odp|html?|png|jpe?g|webp|gif|svg|mp4|mp3|wav|epub"
_DOC_FILE = re.compile(rf"\.({_DOC_EXT})$", re.I)
_DOC_OUT = re.compile(rf"(?:\s-o\s+|--output[= ]|>\s*|--out[= ]|save(?:fig|_as)?\(\s*[\"'])([^\s\"'>]*\.(?:{_DOC_EXT}))\b", re.I)
_DOC_TOOL = re.compile(r"create_document|edit_document|update_document|generate_image|image_gen|make_slides|"
                       r"export_document|render_document|save_document|write_report", re.I)
_WEB_TOOL = re.compile(r"web_?search|web_?fetch|fetch_url|open_url|search_web|searx|browse|browser|navigate", re.I)
_FETCH_TOOL = re.compile(r"web_?fetch|fetch_url|open_url|browse|browser|navigate", re.I)
_FETCH_CMD = re.compile(r"\b(curl|wget|Invoke-WebRequest|iwr|httpx?)\b[^\n]*https?://", re.I)
_INSPECT_TOOL = re.compile(r"^(read|read_file|view|view_file|open_file|inspect_deliverable|inspect_media|"
                           r"inspect_document|preview|look_at_image)$", re.I)
_READ_ONLY_TOOL = re.compile(r"^(read|read_file|view|view_file|ls|list|list_dir|glob|grep|search_files|find|"
                             r"lookup_tools|todowrite|todo_write|update_plan|ask_user|search_chats|"
                             r"search_project_chats|recall|manage_memory)$", re.I)
_READ_ONLY_CMD = re.compile(r"^\s*(ls|dir|cat|type|head|tail|less|more|grep|rg|find|tree|wc|stat|du|df|pwd|which|"
                            r"where|whoami|echo|env|printenv|ps|tasklist|top|uptime|free|nvidia-smi|ping|nslookup|"
                            r"Get-\w+|Test-\w+|Select-String|git\s+(?:status|log|diff|show|branch|remote)|"
                            r"docker\s+(?:ps|images|logs|inspect)|systemctl\s+status|journalctl|"
                            r"kubectl\s+(?:get|describe|logs)|curl\s+(?:-s\s+)?(?:-I\s+)?https?://)\b", re.I)

_URL = re.compile(r"https?://[^\s<>\"'`)\]}]+", re.I)
_CITE = re.compile(r"\[[^\]]+\]\(https?://|\[\d{1,2}\]|https?://", re.I)
_DATE = re.compile(
    r"\b\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}\b|\b(19|20)\d{2}-\d{2}-\d{2}\b|"
    r"\b\d{1,2}\s+(?:de\s+)?(?:ene(?:ro)?|feb(?:rero)?|mar(?:zo)?|abr(?:il)?|may(?:o)?|jun(?:io)?|jul(?:io)?|"
    r"ago(?:sto)?|sep(?:tiembre)?|oct(?:ubre)?|nov(?:iembre)?|dic(?:iembre)?|jan(?:uary)?|february|march|april|"
    r"june|july|aug(?:ust)?|sept?(?:ember)?|october|dec(?:ember)?)\b|"
    r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2}\b|"
    r"\b(?:en|in|de|del|since|desde|as of|a)\s+(?:19|20)\d{2}\b|\b(?:19|20)\d{2}\b", re.I)
_FORMATS = {
    "pdf": ("pdf",), "docx": ("docx", "doc"), "word": ("docx", "doc"), "xlsx": ("xlsx", "xls"),
    "excel": ("xlsx", "xls"), "hoja de cálculo": ("xlsx", "xls", "ods", "csv"), "spreadsheet": ("xlsx", "xls", "ods", "csv"),
    "csv": ("csv",), "pptx": ("pptx", "ppt"), "powerpoint": ("pptx", "ppt"), "presentación": ("pptx", "ppt", "odp", "pdf"),
    "slides": ("pptx", "ppt", "odp", "pdf"), "markdown": ("md",), ".md": ("md",), "json": ("json",),
    "html": ("html", "htm"), "png": ("png",), "jpg": ("jpg", "jpeg"), "jpeg": ("jpg", "jpeg"), "svg": ("svg",),
    "txt": ("txt",), "epub": ("epub",),
}

_STOP_ES = frozenset(
    "de la que el en y los se del las un por con una su para es al lo como más pero sus le ya este sí porque esta "
    "entre cuando muy sin sobre también hasta hay donde quien desde todo nos durante todos uno les ni contra otros "
    "ese eso ante ellos esto antes algunos qué unos yo otro otras otra él tanto esa estos mucho quienes nada muchos "
    "cual poco ella estar estas algunas algo nosotros mi mis tú te ti tu tus ellas cómo dónde cuál puedes hazme "
    "dime busca buscar quiero necesito hoy está son fue ha han".split())
_STOP_EN = frozenset(
    "the of and to in is that it for on with as was are be this by at or from have an not but what all were when we "
    "there can your which their if will about how out up them then so some would make like into has more could than "
    "been who its now did get may please find show tell me give today is are does do you".split())
_WORD = re.compile(r"[a-záéíóúñü']+", re.I)

# The words a person reads for a tool, and the arguments worth showing.
TOOL_TITLES = {
    "bash": "Terminal", "shell": "Terminal", "powershell": "Terminal", "exec_command": "Terminal",
    "local_shell": "Terminal", "python": "Python", "read_file": "Read", "read": "Read", "write_file": "Write",
    "write": "Write", "edit_file": "Edit", "edit": "Edit", "multiedit": "Edit", "apply_patch": "Patch",
    "ls": "List", "glob": "Find files", "grep": "Search in files", "web_search": "Search the web",
    "websearch": "Search the web", "web_fetch": "Open URL", "webfetch": "Open URL", "fetch_url": "Open URL",
    "browser": "Browser", "create_document": "Create document", "edit_document": "Edit document",
    "update_document": "Update document", "generate_image": "Generate image", "delegate_agents": "Delegate",
    "task": "Delegate", "ask_user": "Ask", "update_plan": "Plan", "todowrite": "Tasks", "manage_memory": "Memory",
    "inspect_deliverable": "Inspect deliverable", "inspect_media": "Inspect media", "lookup_tools": "Look up tools",
}
_ARG_KEYS = ("query", "q", "url", "file_path", "path", "filename", "pattern", "command", "cmd", "title", "prompt",
             "description", "name", "task", "text", "input")


def _brief(value: Any, limit: int = 140) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _int(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _args(command: str) -> Any:
    """A tool's arguments: the JSON object when the call carried one, else the raw text."""
    text = (command or "").strip()
    if text[:1] in "{[":
        try:
            return json.loads(text)
        except ValueError:
            pass
    return text


def tool_title(tool: str) -> str:
    low = (tool or "").lower()
    if low in TOOL_TITLES:
        return TOOL_TITLES[low]
    if low.startswith("mcp__"):
        low = low.rsplit("__", 1)[-1]
    if low.startswith("browser_"):
        return "Browser · " + low[len("browser_"):].replace("_", " ")
    return TOOL_TITLES.get(low, (low or "tool").replace("_", " ").capitalize())


def args_summary(command: str, limit: int = 96) -> str:
    """The one argument that says what a call does (its query, URL, path or command), never raw JSON."""
    args = _args(command)
    if isinstance(args, dict):
        for key in _ARG_KEYS:
            value = args.get(key)
            if isinstance(value, list) and value and all(isinstance(x, str) for x in value):
                value = " ".join(value)
            if isinstance(value, (str, int, float)) and str(value).strip():
                return _brief(str(value).strip().splitlines()[0], limit)
        first = next((v for v in args.values() if isinstance(v, str) and v.strip()), "")
        return _brief(first.splitlines()[0] if first else "", limit)
    if isinstance(args, list):
        return _brief(" ".join(str(x) for x in args), limit)
    return _brief(args.splitlines()[0] if args else "", limit)


def _target(command: str) -> str:
    """The path a write or document call produced, when it names one."""
    args = _args(command)
    if isinstance(args, dict):
        for key in ("file_path", "path", "filename", "output", "output_path", "target", "title"):
            if isinstance(args.get(key), str) and args[key].strip():
                return args[key].strip()
        return ""
    if isinstance(args, str):
        m = re.search(r"\*\*\* (?:Update|Add) File:\s*(\S+)", args)
        if m:
            return m.group(1)
        first = args.splitlines()[0].strip() if args else ""
        return first if first and " " not in first else ""    # a bare path
    return ""


def _shell(command: str) -> str:
    """The command line of a shell call, whether it came as text or as ``{command: ...}``."""
    args = _args(command)
    if isinstance(args, dict):
        value = args.get("command") or args.get("cmd") or ""
        return " ".join(map(str, value)) if isinstance(value, list) else str(value)
    return args if isinstance(args, str) else ""


def _ended(call: dict[str, Any]) -> bool:
    return bool(call.get("ended", call.get("ok") is not None))


def _writes(call: dict[str, Any]) -> tuple[str, str]:
    """``(kind, path)`` of what a call wrote: ``code``, ``doc`` or ``""``."""
    tool = str(call.get("tool") or "")
    command = str(call.get("command") or "")
    if _DOC_TOOL.search(tool):
        return "doc", _target(command) or tool
    if _WRITE_TOOL.search(tool) and not _READ_ONLY_TOOL.match(tool):
        path = _target(command)
        return ("doc" if _DOC_FILE.search(path) else "code"), path   # no readable path: an edit to code
    out = _DOC_OUT.search(command)
    return ("doc", out.group(1)) if out else ("", "")


def classify(calls: list[dict[str, Any]]) -> str:
    """The kind of work a turn did: code, research, files, ops or chat.

    Code wins when the turn edited code, used git, ran tests or touched an Agora task;
    then files when it produced documents, images or data files; then research when it
    searched or opened the web; then operations for any other tool; chat with no tools.
    """
    if not calls:
        return "chat"
    kinds = set()
    for call in calls:
        tool = str(call.get("tool") or "")
        command = str(call.get("command") or "")
        wrote, path = _writes(call)
        doc_md = wrote == "doc" and _DOCS.search(path or "")
        if (wrote == "code" or doc_md or _GIT.search(command) or _TESTS.search(command)
                or _AGORA_TASK.search(f"{tool} {command}")):
            kinds.add("code")
        elif wrote == "doc":
            kinds.add("files")
        elif _WEB_TOOL.search(tool) or _FETCH_CMD.search(command):
            kinds.add("research")
        elif not _READ_ONLY_TOOL.match(tool):
            kinds.add("ops")
        else:
            kinds.add("read")
    for kind in ("code", "files", "research", "ops"):
        if kind in kinds:
            return kind
    return "chat"                               # only reading: an answer, not work to review


def language(text: str) -> Optional[str]:
    """``es`` or ``en`` from common words, or None when the text does not say."""
    words = [w.lower() for w in _WORD.findall(text or "")[:400]]
    es = sum(w in _STOP_ES for w in words)
    en = sum(w in _STOP_EN for w in words)
    if es - en >= 2 or (es >= 1 and en == 0 and len(words) >= 3):
        return "es"
    if en - es >= 2 or (en >= 1 and es == 0 and len(words) >= 3):
        return "en"
    return None


def _domain(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _code_marks(calls: list[dict[str, Any]], author: str) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}

    def mark(pid: str, state: str, evidence: str) -> None:
        if found.get(pid, {}).get("state") == "fail" and state != "fail":
            return
        if found.get(pid, {}).get("state") == "ok" and state == "unknown":
            return
        found[pid] = {"state": state, "evidence": _brief(evidence)}

    committed = False
    for call in calls:
        tool = str(call.get("tool") or "")
        command = str(call.get("command") or "")
        text = f"{tool} {command}"
        ok = call.get("ok")
        if _TESTS.search(command) and _ended(call):
            mark("tests", "unknown" if ok is None else "ok" if ok else "fail", command)
        if _BROWSER.search(text) and ok is not False:
            mark("browser", "ok", text)
        if _DOCS.search(command) and (_WRITE_TOOL.search(tool) or _ADD.search(command) or _COMMIT.search(command)):
            mark("docs", "ok", text)
        if (_ADD.search(command) or _COMMIT.search(command)) and _EVIDENCE.search(command):
            mark("no_evidence", "fail", command)
        if _COMMIT.search(command) and ok is not False:
            committed = True
            who = next((g for m in _AUTHOR.finditer(command) for g in m.groups() if g), "").strip()
            if _TRAILER.search(command):
                mark("commit", "fail", "trailer in the message: " + command)
            elif who:
                mark("commit", "ok" if who.lower().startswith(author.lower()) else "fail", "author " + who)
            else:
                mark("commit", "unknown", "commit without an explicit author (repository identity): " + command)
        if _DELIVERY.search(text) and ok is not False:
            mark("agora", "ok", text)
    if committed and "no_evidence" not in found:
        mark("no_evidence", "ok", "no image, capture or QA file staged")
    return found


def _research_marks(calls: list[dict[str, Any]], answer: str, seen_urls: list[str]) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    opened = next((c for c in calls if c.get("ok") is not False and _ended(c)
                   and (_FETCH_TOOL.search(str(c.get("tool") or "")) or _FETCH_CMD.search(str(c.get("command") or "")))),
                  None)
    if opened:
        found["sources_opened"] = {"state": "ok", "evidence": _brief(args_summary(str(opened.get("command") or "")))}
    cited = [u.rstrip(".,;:") for u in _URL.findall(answer or "")]
    if _CITE.search(answer or ""):
        found["sources_cited"] = {"state": "ok", "evidence": _brief(cited[0] if cited else _CITE.search(answer).group(0))}
    date = _DATE.search(answer or "")
    if date:
        start = max(0, date.start() - 40)
        found["dated"] = {"state": "ok", "evidence": _brief(answer[start: date.end() + 20])}
    if cited:
        known = {_domain(u) for u in seen_urls} | {_domain(u) for c in calls for u in _URL.findall(str(c.get("command") or ""))}
        unseen = [u for u in cited if _domain(u) and _domain(u) not in known]
        found["verified"] = ({"state": "fail", "evidence": _brief("cited but never opened or returned: " + ", ".join(unseen[:3]))}
                             if unseen else {"state": "ok", "evidence": _brief(f"{len(cited)} cited links seen in the tool results")})
    else:
        found["verified"] = {"state": "unknown", "evidence": "no links in the answer to compare with the results"}
    return found


def _files_marks(calls: list[dict[str, Any]], prompt: str) -> tuple[dict[str, dict[str, Any]], set[str]]:
    found: dict[str, dict[str, Any]] = {}
    skip: set[str] = set()
    outputs: list[tuple[int, str]] = []
    for i, call in enumerate(calls):
        wrote, path = _writes(call)
        if wrote and call.get("ok") is not False:
            outputs.append((i, path))
    if outputs:
        last = outputs[-1][0]
        names = {re.split(r"[\\/]", p)[-1].lower() for _, p in outputs if p}
        check = next((c for c in calls[last + 1:] if c.get("ok") is True and (
            _INSPECT_TOOL.match(str(c.get("tool") or ""))
            or any(n and n in str(c.get("command") or "").lower() for n in names))), None)
        if check:
            found["output_checked"] = {"state": "ok", "evidence": _brief(
                f"{tool_title(str(check.get('tool') or ''))}: {args_summary(str(check.get('command') or ''))}")}
    low = (prompt or "").lower()
    wanted = sorted({ext for word, exts in _FORMATS.items() if re.search(rf"(?<![\w.]){re.escape(word)}\b", low) for ext in exts})
    if not wanted:
        skip.add("format")                      # nobody asked for a format: nothing to judge
    elif outputs:
        exts = {p.rsplit(".", 1)[-1].lower() for _, p in outputs if "." in p}
        hit = sorted(exts & set(wanted))
        found["format"] = ({"state": "ok", "evidence": "." + ", .".join(hit)} if hit else
                           {"state": "fail", "evidence": _brief(f"asked for .{', .'.join(wanted)}; wrote "
                                                                + ", ".join(p for _, p in outputs if p))})
    return found, skip


def _ops_marks(calls: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    ended = [c for c in calls if _ended(c)]
    if ended:
        last = ended[-1]
        failed = sum(c.get("ok") is False for c in ended)
        what = _brief(args_summary(str(last.get("command") or "")) or tool_title(str(last.get("tool") or "")))
        if last.get("ok") is False:
            found["ran_ok"] = {"state": "fail", "evidence": what}
        elif last.get("ok") is True:
            found["ran_ok"] = {"state": "ok", "evidence": what + (f" (after {failed} failed)" if failed else "")}
        else:
            found["ran_ok"] = {"state": "unknown", "evidence": what}
    acting = [i for i, c in enumerate(calls) if not _READ_ONLY_TOOL.match(str(c.get("tool") or ""))
              and not _READ_ONLY_CMD.match(_shell(str(c.get("command") or "")))]
    if not acting and ended:
        found["checked"] = {"state": "ok", "evidence": "only commands that read"}
    elif acting:
        check = next((c for c in calls[acting[-1] + 1:] if c.get("ok") is True), None)
        if check:
            found["checked"] = {"state": "ok", "evidence": _brief(args_summary(str(check.get("command") or ""))
                                                                  or tool_title(str(check.get("tool") or "")))}
    return found


def judge_practices(calls: list[dict[str, Any]], *, author: str = EXPECTED_AUTHOR, answer: str = "",
                    prompt: str = "", kind: Optional[str] = None, seen_urls: Optional[list[str]] = None,
                    final: bool = False) -> list[dict[str, Any]]:
    """Mark the practices of the turn's kind of work from what its calls ran and what it answered.

    ``calls`` are ``{tool, command, ok}`` records (``ok`` None while a call runs). A practice
    is ``ok`` with the call or text that proves it, ``fail`` with what breaks it, ``unknown``
    when the evidence exists but does not settle it, and ``pending`` when nothing seen
    touches it yet. Practices that do not apply to this turn are left out.
    """
    kind = kind or classify(calls)
    skip: set[str] = set()
    if kind == "code":
        found = _code_marks(calls, author)
    elif kind == "research":
        found = _research_marks(calls, answer, seen_urls or [])
    elif kind == "files":
        found, skip = _files_marks(calls, prompt)
    elif kind == "ops":
        found = _ops_marks(calls)
    else:
        found = {}
    if "language" in KIND_PRACTICES.get(kind, ()):
        asked = language(prompt)
        if asked is None:
            skip.add("language")                # no prompt, or one too short to tell
        elif (answer or "").strip():
            said = language(answer)
            found["language"] = ({"state": "unknown", "evidence": "the answer does not say"} if said is None else
                                 {"state": "ok" if said == asked else "fail", "evidence": f"asked in {asked}, answered in {said}"})
        elif final:
            found["language"] = {"state": "unknown", "evidence": "no written answer"}
    return [{"id": pid, "label": LABELS[pid], **found.get(pid, {"state": "pending", "evidence": ""})}
            for pid in KIND_PRACTICES.get(kind, ()) if pid not in skip]


def summary(practices: list[dict[str, Any]]) -> dict[str, int]:
    return {"ok": sum(p["state"] == "ok" for p in practices),
            "fail": sum(p["state"] == "fail" for p in practices),
            "open": sum(p["state"] in ("pending", "unknown") for p in practices)}


class LoopState:
    """The loop of one run. Not thread-safe by itself; the registry below guards it."""

    def __init__(self, session_id: str, run_id: str, started_at: float, prompt: str = ""):
        self.session_id = session_id
        self.run_id = run_id
        self.started_at = started_at
        self.prompt = (prompt or "")[:4000]
        self.phase = "plan"
        self.phase_since = started_at
        self.lap = 1
        self.tool = ""
        self.tool_since: Optional[float] = None
        self.input_tokens: Optional[int] = None
        self.output_tokens: Optional[int] = None
        self.ended_at: Optional[float] = None
        self.outcome = ""
        self.answer = ""
        self.urls: list[str] = []
        self.steps: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []

    def _step(self, kind: str, label: str, now: float, **extra: Any) -> None:
        self.steps.append({"kind": kind, "label": _brief(label), "at": now, "lap": self.lap, **extra})
        if len(self.steps) > MAX_STEPS:
            del self.steps[0]

    def _enter(self, phase: str, now: float) -> None:
        if phase != self.phase:
            self.phase = phase
            self.phase_since = now

    def feed(self, payload: dict[str, Any], now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        kind = str(payload.get("type") or "")
        if not kind and "delta" in payload:
            delta = str(payload.get("delta") or "")
            if not delta.strip():
                return                     # a separator between rounds is not the model writing
            if not payload.get("thinking") and len(self.answer) < 20000:
                self.answer += delta
            if self.phase != "plan" or not self.steps:
                self._step("plan", "Reasoning" if payload.get("thinking") else "Writing", now)
            self._enter("plan", now)
            return
        round_ = _int(payload.get("round"))
        if round_ and round_ > self.lap:
            self.lap = round_
        if kind == "tool_start":
            self.tool = _brief(payload.get("tool"), 64) or "tool"
            self.tool_since = now
            command = str(payload.get("full_command") or payload.get("command") or "")
            self.calls.append({"tool": self.tool, "command": command[:2000], "ok": None, "at": now})
            summary_ = args_summary(command)
            self._step("tool", f"{tool_title(self.tool)}: {summary_}" if summary_ else tool_title(self.tool), now,
                       tool=self.tool, title=tool_title(self.tool), detail=summary_)
            self._enter("act", now)
        elif kind == "tool_output":
            tool = _brief(payload.get("tool"), 64) or self.tool
            exit_code = payload.get("exit_code")
            ok = exit_code in (None, 0) and str(payload.get("result_status") or "ok") not in ("error", "failed")
            call = next((c for c in reversed(self.calls) if c["ok"] is None and c["tool"] == tool), None)
            if call is None:
                call = {"tool": tool, "command": "", "ok": None, "at": now}
                self.calls.append(call)
            call["ok"] = ok
            call["command"] = call["command"] or str(payload.get("command") or "")[:2000]
            if len(self.urls) < MAX_URLS:
                self.urls.extend(_URL.findall(str(payload.get("output") or "")[:20000])[: MAX_URLS - len(self.urls)])
            command = call["command"]
            ms = round(max(0.0, now - float(call.get("at") or now)) * 1000)
            status = "ok" if ok else f"error (exit {exit_code})" if exit_code not in (None, 0) else "error"
            self._step("result", f"{tool_title(tool)}: {status} · {_secs(ms)}", now, tool=tool, ok=ok, ms=ms)
            if ok and _COMMIT.search(command):
                self._step("commit", command, now)
            if ok and _DELIVERY.search(f"{tool} {command}"):
                self._step("submit", command, now)
            self.tool = ""
            self.tool_since = None
            self._enter("observe", now)
        elif kind == "agent_step" and self.phase != "observe":
            # A new round after a tool result is the model reading that result: still observe until it writes.
            self._enter("plan", now)
        elif kind == "metrics":
            data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
            self.input_tokens = _int(data.get("input_tokens")) if data.get("input_tokens") is not None else self.input_tokens
            self.output_tokens = _int(data.get("output_tokens")) if data.get("output_tokens") is not None else self.output_tokens

    def finish(self, outcome: str, now: Optional[float] = None) -> None:
        if self.ended_at is not None:
            return
        self.ended_at = time.time() if now is None else now
        self.outcome = outcome
        self.tool = ""
        self._step("end", outcome, self.ended_at)
        self._enter("done", self.ended_at)

    def snapshot(self, now: Optional[float] = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        end = self.ended_at or now
        kind = classify(self.calls)
        practices = judge_practices(self.calls, answer=self.answer, prompt=self.prompt, kind=kind,
                                    seen_urls=self.urls, final=self.ended_at is not None)
        return {
            "session_id": self.session_id, "run_id": self.run_id, "running": self.ended_at is None,
            "phase": self.phase, "phase_since": self.phase_since, "lap": self.lap,
            "tool": self.tool or None, "tool_title": tool_title(self.tool) if self.tool else None,
            "tool_since": self.tool_since,
            "started_at": self.started_at, "ended_at": self.ended_at, "outcome": self.outcome or None,
            "elapsed_s": round(max(0.0, end - self.started_at), 1),
            "tokens": {"input": self.input_tokens, "output": self.output_tokens},
            "tool_calls": len(self.calls), "failed_calls": sum(1 for c in self.calls if c["ok"] is False),
            "kind": kind, "kind_label": KIND_LABELS[kind],
            "steps": list(self.steps), "practices": practices, "practices_summary": summary(practices),
        }


def _secs(ms: int) -> str:
    return f"{ms / 1000:.1f} s" if ms < 10000 else f"{round(ms / 1000)} s"


_LOCK = threading.Lock()
_STATES: "OrderedDict[str, LoopState]" = OrderedDict()


def begin(session_id: str, run_id: str, started_at: Optional[float] = None, prompt: str = "") -> LoopState:
    state = LoopState(session_id, run_id, time.time() if started_at is None else started_at, prompt)
    with _LOCK:
        _STATES.pop(session_id, None)
        _STATES[session_id] = state
        while len(_STATES) > MAX_SESSIONS:
            _STATES.popitem(last=False)
    return state


def feed(state: Optional[LoopState], payload: dict[str, Any]) -> None:
    if state is None:
        return
    with _LOCK:
        state.feed(payload)


def finish(state: Optional[LoopState], outcome: str) -> None:
    if state is None:
        return
    with _LOCK:
        state.finish(outcome)


def snapshot(session_id: str) -> Optional[dict[str, Any]]:
    """The running or last loop of a session; None when this process has not run one."""
    with _LOCK:
        state = _STATES.get(session_id)
        return state.snapshot() if state is not None else None
