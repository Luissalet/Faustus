"""review_desk.py -- review desk for deliverables (Hangar E32).

One desk per deliverable. Every version is a snapshot (copied, hashed and
reopened with the delivery-package readers), so "final.docx" saved over itself
three times is still v1, v2 and v3 and there is never a "final-final-2".
Comments are anchored (``src/anchors.py``: page and rectangle, time range,
cell, 3D object, line or symbol) to the version they were made on and keep
pointing at it after newer versions exist; feedback that names an older
version, or an older file name, is recognised as such. Each comment has a
state (open, in_progress, fixed, wontfix, answered) and ``deliver`` names the
one version to hand over, refusing while comments are open unless forced, and
later tells whether the delivered file still matches its hash.

Storage: DATA_DIR/review_desks/<owner>/<id>.json and <id>/vN.<ext>.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import logging
import re
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from src import anchors

logger = logging.getLogger(__name__)

ACTIONS = ("create", "list", "show", "status", "add_version", "comment", "import_feedback", "set_status",
           "compare", "deliver", "delete", "view")
STATES = ("open", "in_progress", "fixed", "wontfix", "answered")
OPEN_STATES = ("open", "in_progress")
SNAPSHOT_LIMIT = 512 * 1024 * 1024
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")
_ID = re.compile(r"[a-z0-9-]{3,80}")
_TEXT_EXT = {".txt", ".md", ".csv", ".tsv", ".json", ".py", ".js", ".ts", ".tsx", ".html", ".htm", ".xml", ".css",
             ".yaml", ".yml", ".ini", ".toml", ".svg", ".gcode", ".scad", ".srt", ".vtt"}
_VERSION_RX = re.compile(r"\b(?:v|versi[oó]n\s*|version\s*|rev\.?\s*)(\d{1,3})\b", re.I)


class DeskError(Exception):
    def __init__(self, error_class: str, detail: str):
        super().__init__(detail)
        self.error_class = error_class
        self.detail = detail


def _err(error_class: str, detail: str) -> Dict[str, Any]:
    return {"error": detail, "error_class": error_class, "exit_code": 1}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _s(args: Mapping[str, Any], key: str, limit: int = 2000) -> str:
    return str(args.get(key) or "").strip()[:limit]


def _dir(owner: str) -> Path:
    from src.constants import DATA_DIR
    return Path(DATA_DIR) / "review_desks" / (_SAFE.sub("_", str(owner or "_"))[:80] or "_")


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "entregable"
    return f"{s}-{hashlib.sha1(f'{text}{time.time()}'.encode()).hexdigest()[:6]}"


def _load(owner: str, did: str) -> Dict[str, Any]:
    if not _ID.fullmatch(did or ""):
        raise DeskError("desk.not_found", f"unknown review desk {did!r}")
    p = _dir(owner) / f"{did}.json"
    if not p.is_file():
        raise DeskError("desk.not_found", f"unknown review desk {did!r}")
    return json.loads(p.read_text(encoding="utf-8"))


def _save(owner: str, rec: Dict[str, Any]) -> None:
    d = _dir(owner)
    d.mkdir(parents=True, exist_ok=True)
    rec["updated"] = _now()
    tmp = d / f"{rec['id']}.json.tmp"
    tmp.write_text(json.dumps(rec, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(d / f"{rec['id']}.json")


def _event(rec: Dict[str, Any], kind: str, text: str) -> None:
    rec.setdefault("events", []).append({"at": _now(), "kind": kind, "text": text[:300]})


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _resolve(raw: str, confine: bool) -> Path:
    from src import delivery_package
    return delivery_package._path(raw, confine)


# ---------------------------------------------------------------------------

def create(owner: str, args: Mapping[str, Any]) -> Dict[str, Any]:
    title = _s(args, "title", 160)
    if not title:
        raise DeskError("desk.bad_args", "title is required")
    rec = {"schema": "review_desk/1", "id": _slug(title), "title": title, "created": _now(),
           "commission": _s(args, "commission", 80), "milestone": _s(args, "milestone", 20),
           "versions": [], "comments": [], "delivered": None, "events": []}
    _event(rec, "created", title)
    return rec


def _version(rec: Mapping[str, Any], n: Any) -> Dict[str, Any]:
    vs = rec.get("versions") or []
    if not vs:
        raise DeskError("desk.no_versions", "this desk has no versions yet")
    if n in (None, "", "latest"):
        return vs[-1]
    try:
        k = int(str(n).lstrip("vV"))
    except ValueError:
        raise DeskError("desk.bad_args", f"version must be a number such as 2 or v2, got {n!r}") from None
    for v in vs:
        if v["n"] == k:
            return v
    raise DeskError("desk.not_found", f"no version v{k}")


def add_version(owner: str, rec: Dict[str, Any], args: Mapping[str, Any], *, confine: bool) -> Dict[str, Any]:
    from src import delivery_package
    src = _resolve(_s(args, "path", 1000), confine)
    if not src.is_file():
        raise DeskError("desk.bad_path", f"{src} is not a file")
    digest = _sha256(src)
    for v in rec["versions"]:
        if v["sha256"] == digest:
            raise DeskError("desk.same_content", f"same content as v{v['n']} ({v['name']}); nothing new to review")
    n = len(rec["versions"]) + 1
    size = src.stat().st_size
    snap: Optional[str] = None
    if size <= SNAPSHOT_LIMIT:
        d = _dir(owner) / rec["id"]
        d.mkdir(parents=True, exist_ok=True)
        target = d / f"v{n}{src.suffix.lower()}"
        shutil.copy2(src, target)
        snap = str(target)
    v = {"n": n, "name": src.name, "source": str(src), "snapshot": snap, "sha256": digest, "size": size,
         "at": _now(), "note": _s(args, "note", 500), "reopen": delivery_package.reopen(Path(snap or src))}
    rec["versions"].append(v)
    carried = [c["id"] for c in rec["comments"] if c["status"] in OPEN_STATES]
    _event(rec, "version", f"v{n} {src.name}" + (f" · {len(carried)} open comment(s) to check" if carried else ""))
    return v


def _version_from_text(rec: Mapping[str, Any], text: str) -> Optional[int]:
    m = _VERSION_RX.search(text)
    if m and any(v["n"] == int(m.group(1)) for v in rec["versions"]):
        return int(m.group(1))
    low = text.lower()
    named = [v for v in rec["versions"] if v["name"].lower() in low]
    names = {v["name"].lower() for v in named}
    if named and len(named) == len(names):
        return max(named, key=lambda v: len(v["name"]))["n"]
    return None


def add_comment(rec: Dict[str, Any], args: Mapping[str, Any]) -> Dict[str, Any]:
    text = _s(args, "text", 4000)
    if not text:
        raise DeskError("desk.bad_args", "comment text is required")
    if args.get("version") not in (None, ""):
        v = _version(rec, args.get("version"))
        how = "given"
    else:
        guessed = _version_from_text(rec, text)
        v = _version(rec, guessed)
        how = "named in the text" if guessed else "latest"
    raw_anchor = args.get("anchor")
    anchor = anchors.parse(raw_anchor) if isinstance(raw_anchor, str) and raw_anchor.strip() else anchors.parse(text)
    latest = rec["versions"][-1]["n"]
    c = {"id": f"C{len(rec['comments']) + 1}", "version": v["n"], "version_from": how, "text": text,
         "author": _s(args, "author", 80), "anchor": anchor, "ref": anchors.ref(rec["id"], v["n"], anchor),
         "at": _now(), "status": "open", "about_older": v["n"] < latest, "history": []}
    rec["comments"].append(c)
    _event(rec, "comment", f"{c['id']} on v{v['n']}" + (f" {anchor['label']}" if anchor else "")
           + (f" (older than v{latest})" if c["about_older"] else ""))
    return c


def import_feedback(rec: Dict[str, Any], args: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """One comment per line; a heading line ending in ':' that names a version applies to the lines below it."""
    text = str(args.get("text") or "")
    if not text.strip():
        raise DeskError("desk.bad_args", "feedback text is required")
    current: Optional[int] = _version_from_text(rec, str(args.get("about") or "")) if args.get("about") else None
    out = []
    for raw in text.splitlines():
        line = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s+", "", raw).strip()
        if len(line) < 4 or line.startswith("#"):
            continue
        own = _version_from_text(rec, line)
        if line.endswith(":"):
            current = own if own is not None else current
            continue
        n = own if own is not None else current
        c = add_comment(rec, {"text": line, "author": args.get("author"), **({"version": n} if n is not None else {})})
        if own is not None:
            c["version_from"] = "named in the text"
        elif n is not None:
            c["version_from"] = "named in the feedback"
        out.append(c)
    if not out:
        raise DeskError("desk.bad_args", "no comment lines found in the feedback")
    return out


def set_status(rec: Dict[str, Any], args: Mapping[str, Any]) -> Dict[str, Any]:
    cid, state = _s(args, "comment", 20), _s(args, "status", 20)
    if state not in STATES:
        raise DeskError("desk.bad_args", f"status must be one of {', '.join(STATES)}")
    c = next((x for x in rec["comments"] if x["id"] == cid), None)
    if c is None:
        raise DeskError("desk.not_found", f"no comment {cid!r}")
    if state == c["status"] and not args.get("note") and not args.get("fixed_in"):
        return c
    entry = {"at": _now(), "from": c["status"], "to": state, "note": _s(args, "note", 500)}
    if state == "fixed":
        entry["in"] = _version(rec, args.get("fixed_in") or None)["n"]
        c["fixed_in"] = entry["in"]
    c["history"].append(entry)
    c["status"] = state
    _event(rec, "status", f"{cid} {entry['from']} -> {state}" + (f" in v{entry['in']}" if entry.get("in") else ""))
    return c


def _text_of(path: Path) -> Optional[List[str]]:
    ext = path.suffix.lower()
    try:
        if ext in _TEXT_EXT:
            return path.read_text(encoding="utf-8", errors="replace").splitlines()
        if ext == ".docx":
            from docx import Document
            return [p.text for p in Document(str(path)).paragraphs]
        if ext == ".xlsx":
            from openpyxl import load_workbook
            wb = load_workbook(str(path), read_only=True, data_only=True)
            out = []
            for ws in wb.worksheets:
                for row in ws.iter_rows():
                    for cell in row:
                        if cell.value is not None and len(out) < 20000:
                            out.append(f"{ws.title}!{cell.coordinate} = {cell.value}")
            return out
        if ext == ".pdf":
            from pypdf import PdfReader
            out = []
            for i, page in enumerate(PdfReader(str(path)).pages, 1):
                out += [f"[p{i}] {ln}" for ln in (page.extract_text() or "").splitlines() if ln.strip()]
            return out
    except Exception:  # noqa: BLE001
        logger.debug("review desk: no text for %s", path, exc_info=True)
    return None


def compare(rec: Mapping[str, Any], args: Mapping[str, Any]) -> Dict[str, Any]:
    vs = rec.get("versions") or []
    if len(vs) < 2 and not args.get("a"):
        raise DeskError("desk.no_versions", "two versions are needed to compare")
    a = _version(rec, args.get("a") or vs[-2]["n"])
    b = _version(rec, args.get("b") or vs[-1]["n"])
    out: Dict[str, Any] = {"a": a["n"], "b": b["n"], "same_content": a["sha256"] == b["sha256"],
                           "size_delta": b["size"] - a["size"], "facts": {}}
    fa = (a.get("reopen") or {}).get("facts") or {}
    fb = (b.get("reopen") or {}).get("facts") or {}
    for k in sorted(set(fa) | set(fb)):
        if fa.get(k) != fb.get(k):
            out["facts"][k] = [fa.get(k), fb.get(k)]
    ta = _text_of(Path(a["snapshot"] or a["source"])) if (a["snapshot"] or a["source"]) else None
    tb = _text_of(Path(b["snapshot"] or b["source"])) if (b["snapshot"] or b["source"]) else None
    if ta is not None and tb is not None:
        diff = list(difflib.unified_diff(ta, tb, f"v{a['n']}", f"v{b['n']}", lineterm="", n=1))
        out["text"] = {"added": sum(1 for d in diff if d.startswith("+") and not d.startswith("+++")),
                       "removed": sum(1 for d in diff if d.startswith("-") and not d.startswith("---")),
                       "diff": diff[:400], "truncated": len(diff) > 400}
    else:
        out["text"] = None
    out["comments_on_a"] = [{"id": c["id"], "status": c["status"], "anchor": (c.get("anchor") or {}).get("label"), "text": c["text"][:200]}
                            for c in rec.get("comments") or [] if c["version"] == a["n"]]
    return out


def view_at(rec: Mapping[str, Any], args: Mapping[str, Any]) -> Dict[str, Any]:
    """Open a version at an anchor (Hangar E22): ``comment`` (its version and anchor), ``ref`` (review:<id>@vN#anchor)
    or ``version`` + ``anchor``. Reads the snapshot kept for that version, so it still shows what was commented."""
    from src import anchor_view
    anchor = None
    n: Any = args.get("version")
    if _s(args, "comment", 20):
        c = next((x for x in rec.get("comments") or [] if x["id"] == _s(args, "comment", 20)), None)
        if c is None:
            raise DeskError("desk.not_found", f"no comment {_s(args, 'comment', 20)!r}")
        n, anchor = c["version"], c.get("anchor")
    elif _s(args, "ref", 300):
        parsed = anchors.parse_ref(_s(args, "ref", 300))
        if not parsed or parsed["desk"] != rec["id"]:
            raise DeskError("desk.bad_args", f"{_s(args, 'ref', 300)!r} is not a reference to this desk")
        n, anchor = parsed["version"], parsed["anchor"]
    elif _s(args, "anchor", 200):
        anchor = anchors.parse(_s(args, "anchor", 200))
        if anchor is None:
            raise DeskError("desk.bad_args", f"no anchor found in {_s(args, 'anchor', 200)!r}")
    v = _version(rec, n)
    path = Path(v["snapshot"]) if v.get("snapshot") and Path(v["snapshot"]).is_file() else Path(v["source"])
    if not path.is_file():
        raise DeskError("desk.not_found", f"v{v['n']} is no longer on disk")
    shown = anchor_view.view(path, anchor, image=args.get("image") in (True, "true", 1, "1"))
    return {"version": v["n"], "name": v["name"], "ref": anchors.ref(rec["id"], v["n"], anchor), "latest": rec["versions"][-1]["n"],
            "from_snapshot": path != Path(v["source"]), **shown}


def deliver(rec: Dict[str, Any], args: Mapping[str, Any]) -> Dict[str, Any]:
    v = _version(rec, args.get("version"))
    still_open = [c["id"] for c in rec["comments"] if c["status"] in OPEN_STATES]
    if still_open and args.get("force") not in (True, "true", 1, "1"):
        raise DeskError("desk.open_comments", f"{len(still_open)} comment(s) still open ({', '.join(still_open[:8])}); "
                                              "close them or pass force to deliver anyway")
    rec["delivered"] = {"version": v["n"], "name": v["name"], "sha256": v["sha256"], "snapshot": v["snapshot"],
                        "at": _now(), "open_comments": still_open, "note": _s(args, "note", 500)}
    _event(rec, "delivered", f"v{v['n']} {v['name']}" + (f" with {len(still_open)} open comment(s)" if still_open else ""))
    return rec["delivered"]


def status(rec: Mapping[str, Any]) -> Dict[str, Any]:
    vs = rec.get("versions") or []
    latest = vs[-1]["n"] if vs else None
    comments = rec.get("comments") or []
    counts = {s: sum(1 for c in comments if c["status"] == s) for s in STATES}
    d = rec.get("delivered")
    check = None
    if d:
        snap = Path(d["snapshot"]) if d.get("snapshot") else None
        src = next((Path(v["source"]) for v in vs if v["n"] == d["version"]), None)
        check = {"snapshot_matches": (_sha256(snap) == d["sha256"]) if snap and snap.is_file() else None,
                 "source_still_matches": (_sha256(src) == d["sha256"]) if src and src.is_file() else None,
                 "newer_versions": [v["n"] for v in vs if v["n"] > d["version"]]}
    return {"latest": latest, "versions": len(vs), "counts": counts,
            "open_on_latest": [c["id"] for c in comments if c["status"] in OPEN_STATES and c["version"] == latest],
            "open_from_older": [c["id"] for c in comments if c["status"] in OPEN_STATES and latest and c["version"] < latest],
            "about_older_when_made": [c["id"] for c in comments if c.get("about_older")],
            "delivered": d, "delivered_check": check,
            "deliverable": f"v{d['version']} {d['name']} sha256:{d['sha256'][:12]}" if d else None}


def list_desks(owner: str) -> List[Dict[str, Any]]:
    d = _dir(owner)
    out = []
    if d.is_dir():
        for p in d.glob("*.json"):
            try:
                r = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            out.append({"id": r["id"], "title": r["title"], "updated": r.get("updated"), "versions": len(r.get("versions") or []),
                        "open": sum(1 for c in r.get("comments") or [] if c["status"] in OPEN_STATES),
                        "delivered": (r.get("delivered") or {}).get("version"), "commission": r.get("commission") or "",
                        "milestone": r.get("milestone") or "", "latest": (r.get("versions") or [{}])[-1].get("n"),
                        "delivered_name": (r.get("delivered") or {}).get("name"), "delivered_at": (r.get("delivered") or {}).get("at")})
    return sorted(out, key=lambda r: r.get("updated") or "", reverse=True)


def attach(owner: str, did: str, commission: str, milestone: str) -> Dict[str, Any]:
    """File the desk under a commission milestone (``commission_office`` link_review); it was free or under another one."""
    rec = _load(owner, did)
    before = (rec.get("commission") or "", rec.get("milestone") or "")
    rec["commission"], rec["milestone"] = commission[:80], milestone[:20]
    if before != (rec["commission"], rec["milestone"]):
        _event(rec, "filed", f"commission {commission} {milestone}" + (f" (was {before[0]} {before[1]})" if before[0] else ""))
        _save(owner, rec)
    return rec


def _tell_commission(owner: str, rec: Mapping[str, Any]) -> None:
    """A delivery made here leaves an event on the commission the desk is filed under; never blocks the delivery."""
    if not rec.get("commission"):
        return
    try:
        from src import commission_office
        commission_office.note_review_delivery(owner, rec)
    except Exception:  # noqa: BLE001
        logger.debug("review desk: could not tell the commission", exc_info=True)


def render(m: Dict[str, Any]) -> str:
    if "deleted" in m:
        return f"Deleted review desk {m['deleted']} ({m.get('title')}); source files untouched."
    if "desks" in m:
        return "\n".join(f"- {r['id']}: {r['title']} · {r['versions']} versions · {r['open']} open"
                         + (f" · delivered v{r['delivered']}" if r.get("delivered") else "") for r in m["desks"]) or "No review desks."
    if "view" in m:
        v = m["view"]
        head = f"{v['ref']} ({v['name']}{', older than v' + str(v['latest']) if v['version'] < v['latest'] else ''}): "
        if not v.get("found"):
            return head + "not found" + (f" — {v['reason']}" if v.get("reason") else "")
        if v.get("lines"):
            return head + "\n" + "\n".join(f"{'>' if ln['hit'] else ' '} {ln['n']:>5} {ln['text']}" for ln in v["lines"])
        if v.get("grid"):
            return head + f"{v['sheet']}!{v['cell']} = {v.get('value')}"
        if v.get("kind") == "page":
            return head + f"page {v['page']} of {v['pages']}" + (f"; in the rectangle: {v['text_in_rect']}" if v.get("text_in_rect") else "")
        if v.get("kind") == "object":
            return head + f"object {v['object']} among {', '.join(v['objects'][:20])}"
        if v.get("kind") == "time":
            return head + f"{v['media']} from {v['start']} s" + (f" to {v['end']} s" if v.get("end") is not None else "")
        return head + "whole file"
    if "comparison" in m:
        c = m["comparison"]
        lines = [f"v{c['a']} -> v{c['b']}: {'same content' if c['same_content'] else 'different'}, size {c['size_delta']:+d} bytes"]
        lines += [f"  {k}: {a} -> {b}" for k, (a, b) in c["facts"].items()]
        if c.get("text"):
            lines.append(f"  text: +{c['text']['added']} -{c['text']['removed']} lines")
            lines += ["  " + d for d in c["text"]["diff"][:60]]
        return "\n".join(lines)
    if "versions" in m and "comments" in m:
        st = m.get("status") or {}
        lines = [f"Review desk {m['id']} '{m['title']}': {len(m['versions'])} versions, latest v{st.get('latest')}"]
        for v in m["versions"]:
            lines.append(f"  v{v['n']} {v['name']} sha256:{v['sha256'][:12]} {v['at']} reopen={v['reopen'].get('status')}")
        for c in m["comments"]:
            lines.append(f"  {c['id']} [{c['status']}] v{c['version']}" + (f" {c['anchor']['label']}" if c.get("anchor") else "")
                         + (" (older version)" if c.get("about_older") else "") + f": {c['text'][:160]}")
        if st.get("deliverable"):
            lines.append(f"Deliverable: {st['deliverable']}")
        return "\n".join(lines)
    return json.dumps(m, ensure_ascii=False)


def run(owner: str, args: Mapping[str, Any], *, confine: bool = True) -> Dict[str, Any]:
    action = _s(args, "action") or "show"
    try:
        if action not in ACTIONS:
            raise DeskError("desk.bad_args", f"action must be one of {', '.join(ACTIONS)}")
        if action == "list":
            return {"desks": list_desks(owner)}
        if action == "create":
            rec = create(owner, args)
            if _s(args, "path"):
                add_version(owner, rec, args, confine=confine)
            _save(owner, rec)
            return {**rec, "status": status(rec)}
        if action == "delete":
            return delete(owner, _s(args, "id"))
        if action == "view" and not _s(args, "id") and _s(args, "ref", 300):
            parsed = anchors.parse_ref(_s(args, "ref", 300))
            if not parsed:
                raise DeskError("desk.bad_args", "ref must look like review:<desk>@v<N>#<anchor>")
            args = {**args, "id": parsed["desk"]}
        rec = _load(owner, _s(args, "id"))
        if action in ("show", "status"):
            return {**rec, "status": status(rec)}
        if action == "compare":
            return {"id": rec["id"], "comparison": compare(rec, args)}
        if action == "view":
            return {"id": rec["id"], "view": view_at(rec, args)}
        extra: Dict[str, Any] = {}
        if action == "add_version":
            extra["version"] = add_version(owner, rec, args, confine=confine)
        elif action == "comment":
            extra["comment"] = add_comment(rec, args)
        elif action == "import_feedback":
            extra["imported"] = import_feedback(rec, args)
        elif action == "set_status":
            extra["comment"] = set_status(rec, args)
        elif action == "deliver":
            extra["delivered"] = deliver(rec, args)
        _save(owner, rec)
        if action == "deliver":
            _tell_commission(owner, rec)
        return {**rec, "status": status(rec), **extra}
    except DeskError as exc:
        return _err(exc.error_class, exc.detail)
    except (ValueError, OSError) as exc:
        return _err("desk.refused", str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.warning("review desk failed", exc_info=True)
        return _err("desk.error", f"{type(exc).__name__}: {exc}")


def delete(owner: str, did: str) -> Dict[str, Any]:
    """Remove the desk record and Faustus' snapshots of its versions; the files the
    versions were taken from are never touched."""
    rec = _load(owner, did)
    base = _dir(owner).resolve()
    snaps = (base / did).resolve()
    if snaps.parent == base and snaps.is_dir():
        shutil.rmtree(snaps)
    (base / f"{did}.json").unlink()
    return {"deleted": did, "title": rec.get("title"), "removed": {"versions": len(rec.get("versions") or []),
                                                                 "comments": len(rec.get("comments") or [])},
            "kept": {"source_files": "never touched"}, "commission": rec.get("commission") or None}


def snapshot_path(owner: str, did: str, n: int) -> Path:
    rec = _load(owner, did)
    v = _version(rec, n)
    if not v.get("snapshot") or not Path(v["snapshot"]).is_file():
        raise DeskError("desk.not_found", f"v{n} has no snapshot")
    return Path(v["snapshot"])
