"""src/annotations.py -- annotate what the agent produced and send the notes back to it; guided code review.

    targets      what is being annotated: the agent's ``plan`` (an answer in the chat), a workspace ``file``
                 (Markdown or HTML), a Studio ``doc``, a local ``web`` page (proxied, see
                 src/annotation_proxy.py) or the ``diff`` of a review
    annotations  one note on a quoted piece of a target: ``comment``, ``suggestion`` (with the replacement
                 text), ``delete`` or ``question``; saved per chat and kept after they are sent
    compose      every pending note of a chat (or of one target) as one user message that quotes what each
                 note is about; sending marks them as sent
    review       the workspace's uncommitted changes, or its diff against a branch/ref, parsed into files,
                 hunks and numbered lines so each line can carry a note
    guide        the chat's model orders the review's hunks into chapters (core, effects, glue) with a short
                 summary each; the hunks themselves stay the real diff

Storage: ``DATA_DIR/annotations/<chat>.json`` = {"annotations": [...], "review": {...} | null,
"target": {...} | null}. One process owns the file; a lock serialises writers.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
from typing import Any, Dict, List, Optional

TYPES = ("comment", "suggestion", "delete", "question")
KINDS = ("plan", "file", "doc", "web", "diff", "text")
CHAPTER_KINDS = ("core", "effects", "glue")
MAX_QUOTE = 4000
MAX_TEXT = 8000
MAX_PER_CHAT = 2000
MAX_GUIDE_DIFF_CHARS = 60_000

_TYPE_LABEL = {"comment": "Comentario", "suggestion": "Cambio propuesto", "delete": "Borrar", "question": "Pregunta"}
_KIND_LABEL = {"plan": "el plan", "file": "el fichero", "doc": "el documento", "web": "la web",
               "diff": "los cambios", "text": "el texto"}

_lock = threading.RLock()


class AnnotationError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# -- storage --------------------------------------------------------------------
_root_override: Optional[str] = None


def set_root_for_tests(path: Optional[str]) -> None:
    global _root_override
    _root_override = path


def _root() -> str:
    if _root_override:
        return _root_override
    from src.constants import DATA_DIR
    return os.path.join(DATA_DIR, "annotations")


def _safe(session: str) -> str:
    sid = str(session or "").strip()
    if not sid or len(sid) > 200:
        raise AnnotationError("bad_args", "`session` is required")
    return re.sub(r"[^A-Za-z0-9_.-]", "_", sid)


def _path(session: str) -> str:
    return os.path.join(_root(), _safe(session) + ".json")


def _load(session: str) -> Dict[str, Any]:
    try:
        with open(_path(session), encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            data.setdefault("annotations", [])
            return data
    except (OSError, ValueError):
        pass
    return {"annotations": [], "review": None, "target": None}


def _save(session: str, data: Dict[str, Any]) -> None:
    path = _path(session)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


# -- targets ----------------------------------------------------------------------
def clean_target(target: Any) -> Dict[str, str]:
    t = target if isinstance(target, dict) else {}
    kind = str(t.get("kind") or "").strip().lower()
    if kind not in KINDS:
        raise AnnotationError("bad_args", f"target.kind must be one of {', '.join(KINDS)}")
    ref = str(t.get("ref") or "").strip()[:2048]
    title = str(t.get("title") or "").strip()[:300]
    if kind == "file" and os.path.isabs(ref):
        ref = os.path.normpath(ref)
    out = {"kind": kind, "ref": ref, "title": title or ref or _KIND_LABEL[kind]}
    if kind == "web":
        from src.annotation_proxy import check_upstream
        out["ref"] = check_upstream(ref)
    if t.get("workspace"):
        out["workspace"] = str(t["workspace"])[:1024]
    return out


def target_key(target: Dict[str, Any]) -> str:
    return f"{target.get('kind')}:{target.get('ref') or ''}"


def set_target(session: str, target: Any) -> Dict[str, str]:
    """The target the chat's Annotate tab should show (what annotate_open asked for)."""
    tgt = clean_target(target)
    with _lock:
        data = _load(session)
        data["target"] = tgt
        _save(session, data)
    return tgt


# -- annotations --------------------------------------------------------------------
def _clean_anchor(anchor: Any) -> Dict[str, Any]:
    a = anchor if isinstance(anchor, dict) else {}
    out: Dict[str, Any] = {}
    for key in ("selector", "file", "side", "hunk", "url"):
        if a.get(key) not in (None, ""):
            out[key] = str(a[key])[:1024]
    for key in ("line", "start", "end"):
        if isinstance(a.get(key), (int, float)) and not isinstance(a.get(key), bool):
            out[key] = int(a[key])
    return out


def add(session: str, target: Any, type_: str, quote: str = "", text: str = "", anchor: Any = None,
        author: str = "person") -> Dict[str, Any]:
    tgt = clean_target(target)
    kind = str(type_ or "comment").strip().lower()
    if kind not in TYPES:
        raise AnnotationError("bad_args", f"type must be one of {', '.join(TYPES)}")
    quote = str(quote or "").strip()[:MAX_QUOTE]
    text = str(text or "").strip()[:MAX_TEXT]
    if not quote and not text:
        raise AnnotationError("bad_args", "an annotation needs the quoted part or a text")
    if kind in ("comment", "question", "suggestion") and not text:
        raise AnnotationError("bad_args", f"a {kind} needs its text")
    note = {
        "id": "an_" + secrets.token_hex(5), "target": tgt, "type": kind, "quote": quote, "text": text,
        "anchor": _clean_anchor(anchor), "author": str(author or "person")[:80],
        "created_at": time.time(), "sent_at": None,
    }
    with _lock:
        data = _load(session)
        if len(data["annotations"]) >= MAX_PER_CHAT:
            raise AnnotationError("too_many", f"this chat already has {MAX_PER_CHAT} annotations")
        data["annotations"].append(note)
        _save(session, data)
    return note


def list_(session: str, target: Any = None, pending_only: bool = False) -> List[Dict[str, Any]]:
    data = _load(session)
    key = target_key(clean_target(target)) if target else None
    out = []
    for note in data["annotations"]:
        if key and target_key(note["target"]) != key:
            continue
        if pending_only and note.get("sent_at"):
            continue
        out.append(note)
    return out


def update(session: str, note_id: str, **fields: Any) -> Dict[str, Any]:
    with _lock:
        data = _load(session)
        for note in data["annotations"]:
            if note["id"] == note_id:
                if fields.get("type") is not None:
                    if fields["type"] not in TYPES:
                        raise AnnotationError("bad_args", f"type must be one of {', '.join(TYPES)}")
                    note["type"] = fields["type"]
                if fields.get("text") is not None:
                    note["text"] = str(fields["text"]).strip()[:MAX_TEXT]
                if fields.get("pending"):
                    note["sent_at"] = None
                _save(session, data)
                return note
    raise AnnotationError("not_found", f"no annotation {note_id!r} in this chat")


def delete(session: str, note_id: Optional[str] = None, target: Any = None, sent_only: bool = False) -> int:
    """One annotation by id, or all of a target (or of the chat), or only the ones already sent."""
    key = target_key(clean_target(target)) if target else None
    with _lock:
        data = _load(session)
        before = len(data["annotations"])
        keep = []
        for note in data["annotations"]:
            hit = (note["id"] == note_id) if note_id else (not key or target_key(note["target"]) == key)
            if hit and sent_only and not note.get("sent_at"):
                hit = False
            if not hit:
                keep.append(note)
        data["annotations"] = keep
        if len(keep) != before:
            _save(session, data)
        return before - len(keep)


def _short(text: str, n: int = 600) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def _where(note: Dict[str, Any]) -> str:
    a = note.get("anchor") or {}
    if note["target"]["kind"] == "diff" and a.get("file"):
        side = {"new": "línea nueva", "old": "línea quitada"}.get(a.get("side", ""), "línea")
        return f" en `{a['file']}:{a.get('line', '?')}` ({side})"
    if note["target"]["kind"] == "web" and a.get("selector"):
        return f" en el elemento `{_short(a['selector'], 160)}`"
    return ""


def _line(i: int, note: Dict[str, Any]) -> str:
    label = _TYPE_LABEL[note["type"]]
    quote = _short(note.get("quote") or "")
    where = _where(note)
    text = (note.get("text") or "").strip()
    if note["type"] == "suggestion":
        head = f"{i}. {label}{where}: «{quote}» → «{text}»" if quote else f"{i}. {label}{where}: {text}"
        return head
    if note["type"] == "delete":
        head = f"{i}. {label}{where}: «{quote}»"
        return head + (f"\n   {text}" if text else "")
    head = f"{i}. {label}{where}" + (f" sobre «{quote}»" if quote else "")
    return head + (f"\n   {text}" if text else "")


def compose(session: str, ids: Optional[List[str]] = None, target: Any = None, mark_sent: bool = True,
            include_sent: bool = False) -> Dict[str, Any]:
    """The pending notes (or the ones named) as one user message, grouped by what they are about."""
    key = target_key(clean_target(target)) if target else None
    wanted = set(ids or [])
    with _lock:
        data = _load(session)
        chosen = [n for n in data["annotations"]
                  if (not wanted or n["id"] in wanted)
                  and (not key or target_key(n["target"]) == key)
                  and (include_sent or wanted or not n.get("sent_at"))]
        if not chosen:
            return {"text": "", "count": 0, "ids": []}
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for note in chosen:
            groups.setdefault(target_key(note["target"]), []).append(note)
        blocks: List[str] = []
        for notes in groups.values():
            tgt = notes[0]["target"]
            title = tgt.get("title") or tgt.get("ref") or ""
            head = f"Anotaciones sobre {_KIND_LABEL.get(tgt['kind'], 'el texto')} «{title}»"
            if tgt["kind"] in ("file", "web") and tgt.get("ref") and tgt.get("ref") != title:
                head += f" ({tgt['ref']})"
            if tgt["kind"] == "diff" and data.get("review"):
                rv = data["review"]
                head += f" — {rv.get('workspace', '')}, frente a {rv.get('base') or 'HEAD'}"
            lines = [head + ":"]
            lines += [_line(i, n) for i, n in enumerate(notes, 1)]
            blocks.append("\n".join(lines))
        text = "\n\n".join(blocks)
        text += "\n\nTen en cuenta estas anotaciones y responde a las preguntas; aplica los cambios y borrados donde corresponda."
        if mark_sent:
            now = time.time()
            for note in chosen:
                note["sent_at"] = now
            _save(session, data)
    return {"text": text, "count": len(chosen), "ids": [n["id"] for n in chosen]}


def state(session: str) -> Dict[str, Any]:
    data = _load(session)
    notes = data["annotations"]
    return {"target": data.get("target"), "annotations": notes,
            "pending": sum(1 for n in notes if not n.get("sent_at")),
            "review": _review_summary(data.get("review"))}


# -- review ------------------------------------------------------------------------
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


def parse_diff(text: str) -> List[Dict[str, Any]]:
    """A unified diff as files -> hunks -> lines, each line with its old and new number."""
    files: List[Dict[str, Any]] = []
    cur: Optional[Dict[str, Any]] = None
    hunk: Optional[Dict[str, Any]] = None
    old_no = new_no = 0
    hid = 0
    for raw in (text or "").splitlines():
        if raw.startswith("diff --git "):
            m = re.match(r"diff --git a/(.+?) b/(.+)$", raw)
            path = m.group(2).strip() if m else raw[11:].strip()
            cur = {"path": path, "status": "modified", "hunks": [], "added": 0, "removed": 0}
            files.append(cur)
            hunk = None
            continue
        if cur is None:
            continue
        if hunk is None:
            if raw.startswith("new file mode") or raw.startswith("--- /dev/null"):
                cur["status"] = "added"
            elif raw.startswith("deleted file mode") or raw.startswith("+++ /dev/null"):
                cur["status"] = "deleted"
            elif raw.startswith("rename to "):
                cur["status"] = "renamed"
                cur["path"] = raw[10:].strip()
            elif raw.startswith("+++ b/") and cur["status"] != "deleted":
                cur["path"] = raw[6:].strip()
            elif raw.startswith("Binary files"):
                cur["status"] = "binary"
        m = _HUNK_RE.match(raw)
        if m:
            hid += 1
            old_no, new_no = int(m.group(1)), int(m.group(3))
            hunk = {"id": f"H{hid}", "header": raw, "context": m.group(5).strip(), "lines": []}
            cur["hunks"].append(hunk)
            continue
        if hunk is None:
            continue
        if raw.startswith("\\"):
            continue
        tag, body = (raw[:1], raw[1:]) if raw else (" ", "")
        if tag == "+":
            hunk["lines"].append({"k": "add", "old": None, "new": new_no, "text": body})
            new_no += 1
            cur["added"] += 1
        elif tag == "-":
            hunk["lines"].append({"k": "del", "old": old_no, "new": None, "text": body})
            old_no += 1
            cur["removed"] += 1
        else:
            hunk["lines"].append({"k": "ctx", "old": old_no, "new": new_no, "text": body})
            old_no += 1
            new_no += 1
    return files


def _review_summary(review: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not review:
        return None
    return {k: review.get(k) for k in ("workspace", "base", "at", "files", "guide") if k in review}


def open_review(session: str, workspace: str, base: str = "HEAD", diff_text: str = "") -> Dict[str, Any]:
    """The workspace's changes against ``base`` (uncommitted when HEAD), or a diff given as text."""
    from src import staged_review
    base = (base or "HEAD").strip() or "HEAD"
    if diff_text.strip():
        raw, truncated, err = diff_text, False, None
        workspace = workspace or ""
    else:
        if not workspace or not os.path.isabs(workspace) or not os.path.isdir(workspace):
            raise AnnotationError("bad_args", "workspace must be an existing absolute directory")
        res = staged_review.collect_diff(workspace, base, max_chars=400_000)
        raw, truncated, err = res.get("diff") or "", bool(res.get("truncated")), res.get("error")
        if err:
            raise AnnotationError("git", str(err))
    files = parse_diff(raw)
    review = {"workspace": workspace, "base": base, "at": time.time(), "truncated": truncated,
              "files": [{"path": f["path"], "status": f["status"], "added": f["added"], "removed": f["removed"],
                         "hunks": len(f["hunks"])} for f in files],
              "diff": raw, "guide": None}
    if session:
        with _lock:
            data = _load(session)
            data["review"] = review
            data["target"] = {"kind": "diff", "ref": f"{workspace}@{base}", "title": f"Cambios frente a {base}",
                              "workspace": workspace}
            _save(session, data)
    return {"workspace": workspace, "base": base, "truncated": truncated, "files": files,
            "stats": {"files": len(files), "added": sum(f["added"] for f in files),
                      "removed": sum(f["removed"] for f in files),
                      "hunks": sum(len(f["hunks"]) for f in files)}}


def current_review(session: str) -> Optional[Dict[str, Any]]:
    data = _load(session)
    rv = data.get("review")
    if not rv:
        return None
    files = parse_diff(rv.get("diff") or "")
    return {"workspace": rv.get("workspace"), "base": rv.get("base"), "at": rv.get("at"),
            "truncated": rv.get("truncated", False), "files": files, "guide": rv.get("guide"),
            "stats": {"files": len(files), "added": sum(f["added"] for f in files),
                      "removed": sum(f["removed"] for f in files),
                      "hunks": sum(len(f["hunks"]) for f in files)}}


def _guide_prompt(files: List[Dict[str, Any]]) -> str:
    parts: List[str] = []
    size = 0
    for f in files:
        for h in f["hunks"]:
            body = "\n".join(("+" if ln["k"] == "add" else "-" if ln["k"] == "del" else " ") + ln["text"]
                             for ln in h["lines"])
            chunk = f"### {h['id']} {f['path']} ({f['status']})\n{h['header']}\n{body}\n"
            if size + len(chunk) > MAX_GUIDE_DIFF_CHARS:
                chunk = f"### {h['id']} {f['path']} ({f['status']})\n{h['header']}\n(hunk omitted for length)\n"
            parts.append(chunk)
            size += len(chunk)
    return (
        "Eres revisor de código. Ordena estos cambios para que una persona los lea en el mejor orden.\n"
        "Agrupa los hunks (H1, H2…) en capítulos de tres clases:\n"
        "- core: el núcleo, la lógica nueva o cambiada que justifica el cambio;\n"
        "- effects: los efectos, lo que cambia de comportamiento por ello (llamadas, interfaz, datos, tests);\n"
        "- glue: el pegamento, cableado, registros, imports, configuración y documentación.\n"
        "Cada hunk va en un solo capítulo. Ordena los capítulos para leer primero el núcleo.\n"
        "Resumen de cada capítulo: dos o tres frases en castellano, concretas, sobre qué hace el código.\n"
        "Responde SOLO con JSON: {\"overview\": \"una frase\", \"chapters\": [{\"kind\": \"core|effects|glue\", "
        "\"title\": \"título corto\", \"summary\": \"…\", \"hunks\": [\"H1\", \"H2\"]}]}\n\n"
        + "\n".join(parts)
    )


def parse_guide(raw: str, files: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The model's chapters, kept to hunks that exist; hunks it left out go to a last chapter."""
    text = str(raw or "")
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise AnnotationError("unparsed", "the model did not answer with chapters")
    try:
        data = json.loads(text[start:end + 1])
    except ValueError as exc:
        raise AnnotationError("unparsed", f"the model's chapters are not valid JSON: {exc}") from exc
    known = [h["id"] for f in files for h in f["hunks"]]
    seen: set = set()
    chapters: List[Dict[str, Any]] = []
    for ch in data.get("chapters") or []:
        if not isinstance(ch, dict):
            continue
        kind = str(ch.get("kind") or "").lower()
        kind = kind if kind in CHAPTER_KINDS else "glue"
        hunks = [h for h in (str(x).strip().upper() for x in ch.get("hunks") or []) if h in known and h not in seen]
        seen.update(hunks)
        if not hunks:
            continue
        chapters.append({"kind": kind, "title": _short(ch.get("title") or kind, 120),
                         "summary": _short(ch.get("summary") or "", 900), "hunks": hunks})
    rest = [h for h in known if h not in seen]
    if rest:
        chapters.append({"kind": "glue", "title": "Resto", "summary": "Hunks que el modelo no clasificó.", "hunks": rest})
    order = {k: i for i, k in enumerate(CHAPTER_KINDS)}
    chapters.sort(key=lambda c: order[c["kind"]])
    return {"overview": _short(data.get("overview") or "", 400), "chapters": chapters}


def _resolve_model(owner: Optional[str], wanted: str = ""):
    from src import endpoint_resolver as er
    if wanted:
        ep = er.endpoint_id_serving(wanted, owner=owner or None)
        routed = er.resolve_endpoint_by_id(ep, model=wanted, owner=owner or None) if ep else None
        if routed and routed[0] and routed[1]:
            return routed
    routed = er.resolve_endpoint("utility", owner=owner or None)
    if routed and routed[0] and routed[1]:
        return routed
    raise AnnotationError("no_model", "no model endpoint is configured for the guide")


async def guide(session: str, owner: Optional[str] = None, model: str = "", timeout: float = 180.0) -> Dict[str, Any]:
    """Ask the chat's model to order the open review into chapters; saved with the review."""
    import asyncio
    review = current_review(session)
    if not review:
        raise AnnotationError("no_review", "no review is open in this chat; call review_open first")
    files = review["files"]
    if not any(f["hunks"] for f in files):
        raise AnnotationError("empty", "the review has no changed lines to order")
    url, mdl, headers = await asyncio.to_thread(_resolve_model, owner, model)
    from src.llm_core import llm_call_async
    try:
        raw = await asyncio.wait_for(
            llm_call_async(url=url, model=mdl, messages=[{"role": "user", "content": _guide_prompt(files)}],
                           headers=headers, temperature=0.1, max_tokens=0, timeout=int(timeout), max_retries=1,
                           session_id=session or None, workload="foreground", _spend_purpose="review_guide"),
            timeout=timeout + 30)
    except asyncio.TimeoutError as exc:
        raise AnnotationError("timeout", f"the model did not answer within {int(timeout) + 30} s") from exc
    if isinstance(raw, tuple):
        raw = raw[0]
    result = parse_guide(raw, files)
    result["model"] = mdl
    result["at"] = time.time()
    with _lock:
        data = _load(session)
        if data.get("review"):
            data["review"]["guide"] = result
            _save(session, data)
    return result


def render_guide(guide_: Dict[str, Any], files: List[Dict[str, Any]]) -> str:
    """The chapters as text for an agent or an MCP caller: summary, then the hunks' real lines."""
    by_id = {h["id"]: (f["path"], h) for f in files for h in f["hunks"]}
    label = {"core": "Núcleo", "effects": "Efectos", "glue": "Pegamento"}
    out = [guide_.get("overview") or ""]
    for i, ch in enumerate(guide_.get("chapters") or [], 1):
        out.append(f"\n## {i}. {label.get(ch['kind'], ch['kind'])}: {ch['title']}\n{ch['summary']}")
        for hid in ch["hunks"]:
            if hid not in by_id:
                continue
            path, h = by_id[hid]
            out.append(f"\n{hid} {path} {h['header']}")
    return "\n".join(x for x in out if x is not None).strip()
