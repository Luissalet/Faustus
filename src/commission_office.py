"""commission_office.py -- the commission office (Hangar proposal E29, first cut).

One record per commission (encargo) that follows its professional cycle:
opportunity -> proposal -> agreed -> in progress -> delivered -> closed (or
lost). The first cut covers scope -> milestone -> delivery, linking contacts
and files, and points at the owners of each thing instead of copying them:

* **Scope** is kept as versions (initial request, agreement, change requests).
  A new version is compared with the current agreement by the scope
  comparator (``src/scope_compare.py``, E30) and the comparison id is kept.
* **Milestones** have a due date and deliverables; a deliverable may point at
  a delivery package (``src/delivery_package.py``, E33), whose *ready* flag is
  read live, never copied.
* **Contacts** are names and roles, optionally the id of a People's Hoard
  person; **files** are paths with a role; **payments** are labels with the
  expected amount and an optional Ledger reference. Nothing is fetched from or
  written to People or Ledger here.
* **People and Ledger, read live** (follow-up of E29): ``people_lookup`` asks
  People's Hoard (``find_people``) through the local Hub and ``link_person``
  stores only the id; ``reconcile_payments`` reads Ledger's entries
  (``list_entries``, read-only) around each expected payment and lists the
  incoming movements of exactly that amount, scored by date and by the client
  or label in the counterparty or note; ``confirm_payment`` records the chosen
  movement id. Nothing is written to People or Ledger.
* **Status** is computed: next milestone, overdue ones, deliverables without a
  ready package, open scope questions, decisions.
* **Proposal** (E31): ``action=proposal`` drafts a Word + Markdown proposal from
  the record through ``src/proposal_builder.py`` (verified / assumption /
  question per point, internal names and paths masked and listed).

Records live in DATA_DIR/commissions/<owner>/<id>.json. One dispatcher,
``run(owner, args)``, serves the chat tool, HTTP and MCP; errors come back as
``{"error", "error_class", "exit_code": 1}``.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

logger = logging.getLogger(__name__)

STAGES = ("opportunity", "proposal", "agreed", "in_progress", "delivered", "closed", "lost")
SCOPE_KINDS = ("initial", "agreement", "change")
ACTIONS = ("create", "update", "list", "show", "add_scope", "add_milestone", "update_milestone", "link_package",
           "add_contact", "add_file", "add_decision", "add_payment", "status", "proposal",
           "people_lookup", "link_person", "reconcile_payments", "confirm_payment",
           "list_meetings", "import_meeting", "promote_meeting_item")
PROMOTE_AS = ("decision", "requirement", "milestone")
LEDGER_PAGE = 200
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")
_ID = re.compile(r"[a-z0-9-]{3,80}")


class OfficeError(Exception):
    def __init__(self, error_class: str, detail: str):
        super().__init__(detail)
        self.error_class = error_class
        self.detail = detail


def _err(error_class: str, detail: str) -> Dict[str, Any]:
    return {"error": detail, "error_class": error_class, "exit_code": 1}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _today() -> _dt.date:
    return _dt.date.today()


def _date(raw: Any, field: str) -> str:
    s = str(raw or "").strip()
    if not s:
        return ""
    try:
        return _dt.date.fromisoformat(s[:10]).isoformat()
    except ValueError:
        raise OfficeError("office.bad_date", f"{field} must be a date YYYY-MM-DD, got {s!r}") from None


def _s(args: Mapping[str, Any], key: str, limit: int = 2000) -> str:
    return str(args.get(key) or "").strip()[:limit]


def _dir(owner: str) -> Path:
    from src.constants import DATA_DIR
    return Path(DATA_DIR) / "commissions" / (_SAFE.sub("_", str(owner or "_"))[:80] or "_")


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "encargo"
    return f"{s}-{hashlib.sha1(f'{text}{time.time()}'.encode()).hexdigest()[:6]}"


def _load(owner: str, cid: str) -> Dict[str, Any]:
    if not _ID.fullmatch(cid or ""):
        raise OfficeError("office.not_found", f"unknown commission {cid!r}")
    p = _dir(owner) / f"{cid}.json"
    if not p.is_file():
        raise OfficeError("office.not_found", f"unknown commission {cid!r}")
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


def _next_id(items: List[Dict[str, Any]], prefix: str) -> str:
    return f"{prefix}{len(items) + 1}"


# ---------------------------------------------------------------------------

def create(owner: str, args: Mapping[str, Any]) -> Dict[str, Any]:
    title = _s(args, "title", 160)
    if not title:
        raise OfficeError("office.bad_args", "title is required")
    stage = _s(args, "stage") or "opportunity"
    if stage not in STAGES:
        raise OfficeError("office.bad_stage", f"stage must be one of {', '.join(STAGES)}")
    rec: Dict[str, Any] = {"schema": "commission/1", "id": _slug(title), "title": title, "client": _s(args, "client", 160),
                           "stage": stage, "created": _now(), "summary": _s(args, "summary"),
                           "budget": _budget(args.get("budget")), "scope": [], "milestones": [], "contacts": [],
                           "files": [], "decisions": [], "payments": [], "events": []}
    _event(rec, "created", f"{title} ({stage})")
    if _s(args, "request"):
        _add_scope(owner, rec, {"kind": "initial", "text": args.get("request")})
    _save(owner, rec)
    return rec


def _budget(raw: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, Mapping):
        return None
    try:
        amount = float(raw.get("amount"))
    except (TypeError, ValueError):
        raise OfficeError("office.bad_budget", "budget.amount must be a number") from None
    return {"amount": amount, "currency": str(raw.get("currency") or "EUR")[:8], "note": str(raw.get("note") or "")[:300]}


def update(owner: str, rec: Dict[str, Any], args: Mapping[str, Any]) -> Dict[str, Any]:
    changed = []
    for key in ("title", "client", "summary"):
        if key in args and args.get(key) is not None:
            rec[key] = _s(args, key, 2000 if key == "summary" else 160)
            changed.append(key)
    if args.get("stage"):
        st = _s(args, "stage")
        if st not in STAGES:
            raise OfficeError("office.bad_stage", f"stage must be one of {', '.join(STAGES)}")
        if st != rec["stage"]:
            _event(rec, "stage", f"{rec['stage']} -> {st}")
            rec["stage"] = st
            changed.append("stage")
    if "budget" in args:
        rec["budget"] = _budget(args.get("budget"))
        changed.append("budget")
    if changed:
        _event(rec, "updated", ", ".join(changed))
    return rec


def _current_agreement(rec: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    for v in reversed(rec.get("scope") or []):
        if v["kind"] == "agreement":
            return v
    return None


def _add_scope(owner: str, rec: Dict[str, Any], args: Mapping[str, Any]) -> Dict[str, Any]:
    kind = _s(args, "kind") or "change"
    text = str(args.get("text") or "").strip()
    if kind not in SCOPE_KINDS:
        raise OfficeError("office.bad_kind", f"scope kind must be one of {', '.join(SCOPE_KINDS)}")
    if not text:
        raise OfficeError("office.bad_args", "scope text is required")
    version: Dict[str, Any] = {"version": len(rec["scope"]) + 1, "kind": kind, "text": text[:20000], "at": _now(),
                               "source": _s(args, "source", 300)}
    agreed = _current_agreement(rec)
    base = agreed
    if kind == "agreement" and agreed is None:
        base = next((v for v in reversed(rec["scope"]) if v["kind"] == "initial"), None)
    if base is not None:
        from src import scope_compare
        initial = next((v["text"] for v in rec["scope"] if v["kind"] == "initial"), "")
        cmp = scope_compare.run(owner, {"agreement": base["text"], "new_request": text, "graph": _s(args, "graph"),
                                        "initial": initial if kind == "change" else "",
                                        "effort_hours": args.get("effort_hours") if isinstance(args.get("effort_hours"), Mapping) else {},
                                        "title": f"{rec['title']} · v{base['version']} -> v{version['version']}",
                                        "lang": _s(args, "lang") or "es"})
        if not cmp.get("error"):
            version["comparison"] = {"id": cmp["id"], "against": base["version"], "counts": cmp["counts"],
                                     "questions": cmp["questions"]}
    rec["scope"].append(version)
    if kind == "agreement" and rec["stage"] in ("opportunity", "proposal"):
        _event(rec, "stage", f"{rec['stage']} -> agreed")
        rec["stage"] = "agreed"
    _event(rec, "scope", f"v{version['version']} {kind}" + (f" ({sum(v for k, v in version['comparison']['counts'].items() if k != 'included')} differences)" if version.get("comparison") else ""))
    return version


def _milestone(rec: Mapping[str, Any], mid: str) -> Dict[str, Any]:
    for m in rec.get("milestones") or []:
        if m["id"] == mid:
            return m
    raise OfficeError("office.not_found", f"no milestone {mid!r}")


def add_milestone(rec: Dict[str, Any], args: Mapping[str, Any]) -> Dict[str, Any]:
    title = _s(args, "title", 160)
    if not title:
        raise OfficeError("office.bad_args", "milestone title is required")
    m = {"id": _next_id(rec["milestones"], "M"), "title": title, "due": _date(args.get("due"), "due"), "done": None,
         "deliverables": [], "amount": None}
    if args.get("amount") is not None:
        try:
            m["amount"] = float(args["amount"])
        except (TypeError, ValueError):
            raise OfficeError("office.bad_args", "amount must be a number") from None
    for d in args.get("deliverables") or []:
        m["deliverables"].append({"title": str(d)[:160], "package_id": None})
    rec["milestones"].append(m)
    _event(rec, "milestone", f"{m['id']} {title}" + (f" due {m['due']}" if m["due"] else ""))
    return m


def update_milestone(rec: Dict[str, Any], args: Mapping[str, Any]) -> Dict[str, Any]:
    m = _milestone(rec, _s(args, "milestone"))
    if args.get("title"):
        m["title"] = _s(args, "title", 160)
    if "due" in args:
        m["due"] = _date(args.get("due"), "due")
    if "done" in args:
        m["done"] = _date(args.get("done") if args.get("done") not in (True, "true") else _today().isoformat(), "done") if args.get("done") else None
        _event(rec, "milestone", f"{m['id']} {'done ' + m['done'] if m['done'] else 'reopened'}")
    if args.get("add_deliverable"):
        m["deliverables"].append({"title": _s(args, "add_deliverable", 160), "package_id": None})
    return m


def link_package(owner: str, rec: Dict[str, Any], args: Mapping[str, Any]) -> Dict[str, Any]:
    from src import delivery_package
    m = _milestone(rec, _s(args, "milestone"))
    pid = _s(args, "package_id")
    pkg = delivery_package.run(owner, {"action": "show", "id": pid})
    if pkg.get("error"):
        raise OfficeError("office.unknown_package", f"no delivery package {pid!r}")
    title = _s(args, "deliverable", 160) or pkg["name"]
    for d in m["deliverables"]:
        if d["title"] == title:
            d["package_id"] = pid
            break
    else:
        m["deliverables"].append({"title": title, "package_id": pid})
    _event(rec, "delivery", f"{m['id']} ← package {pkg['name']} ({'ready' if pkg.get('ready') else 'not ready'})")
    return m


def _append(rec: Dict[str, Any], key: str, item: Dict[str, Any], label: str) -> Dict[str, Any]:
    rec[key].append(item)
    _event(rec, key.rstrip("s"), label)
    return item



# ---------------------------------------------------------------------------
# People and Ledger through the local Hub (read-only)

def _hub_call(app: str, tool: str, arguments: Mapping[str, Any]) -> Dict[str, Any]:
    """Call a family app's tool through the Hub; never raises."""
    try:
        from src import family_services
        from src.hoard_link import family
        family_services.configure()
        res = family.call(app, tool, dict(arguments), timeout=30.0)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    if not isinstance(res, dict):
        return {"ok": False, "error": "unexpected answer from the hub"}
    if res.get("ok") is False or res.get("error"):
        return {"ok": False, "error": str(res.get("error") or f"HTTP {res.get('status')}")}
    return {"ok": True, "result": res.get("result", res)}


def _rows(result: Any, *keys: str) -> List[Dict[str, Any]]:
    if isinstance(result, list):
        return [r for r in result if isinstance(r, dict)]
    if isinstance(result, dict):
        for k in keys:
            if isinstance(result.get(k), list):
                return [r for r in result[k] if isinstance(r, dict)]
        if isinstance(result.get("result"), (list, dict)):
            return _rows(result["result"], *keys)
    return []


def people_lookup(args: Mapping[str, Any]) -> Dict[str, Any]:
    name = _s(args, "name", 120)
    if not name:
        raise OfficeError("office.bad_args", "name is required")
    res = _hub_call("people", "find_people", {"query": name, "limit": 6})
    if not res["ok"]:
        return {"people": [], "source": "people", "error": res["error"]}
    people = []
    for r in _rows(res["result"], "results", "people", "candidates", "items", "matches"):
        pid = r.get("id") or r.get("person_id")
        if pid:
            people.append({"id": str(pid), "name": str(r.get("name") or r.get("display_name") or ""),
                           "score": r.get("score"),
                           "relation": r.get("relation") or r.get("relationship") or ", ".join(str(c) for c in r.get("circles") or [])})
    return {"people": people, "source": "people"}


def link_person(rec: Dict[str, Any], args: Mapping[str, Any]) -> Dict[str, Any]:
    name, pid = _s(args, "name", 120), _s(args, "people_id", 80)
    if not name or not pid:
        raise OfficeError("office.bad_args", "name and people_id are required")
    for c in rec["contacts"]:
        if c["name"] == name:
            c["people_id"] = pid
            break
    else:
        rec["contacts"].append({"name": name, "role": _s(args, "role", 80), "email": "", "people_id": pid})
    _event(rec, "contact", f"{name} ↔ People {pid}")
    return rec


def _words_of(text: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]{3,}", (text or "").lower())}


def reconcile_payments(rec: Dict[str, Any]) -> Dict[str, Any]:
    pending = [p for p in rec.get("payments") or [] if p.get("amount") is not None and not p.get("confirmed")]
    if not pending:
        return {"checked": 0, "entries_scanned": 0}
    today = _today()
    dates = [_dt.date.fromisoformat(p["due"]) for p in pending if p.get("due")] or [today]
    start = min(dates) - _dt.timedelta(days=60)
    end = max(max(dates) + _dt.timedelta(days=60), today)
    res = _hub_call("ledger", "list_entries", {"from": start.isoformat(), "to": end.isoformat(), "limit": LEDGER_PAGE, "order": "asc"})
    if not res["ok"]:
        for p in pending:
            p["ledger_check"] = {"at": _now(), "error": res["error"]}
        return {"checked": 0, "error": res["error"]}
    entries = _rows(res["result"], "items", "entries", "results")
    truncated = len(entries) >= LEDGER_PAGE
    client_words = _words_of(rec.get("client") or "")
    for p in pending:
        cents = round(float(p["amount"]) * 100)
        due = _dt.date.fromisoformat(p["due"]) if p.get("due") else None
        label_words = _words_of(p["label"]) | client_words
        cands = []
        for e in entries:
            try:
                ec = int(e.get("amount_cents"))
            except (TypeError, ValueError):
                continue
            if ec <= 0 or abs(ec - cents) > 1:
                continue
            score = 0.6
            text = f"{e.get('counterparty') or ''} {e.get('note') or ''}"
            if label_words & _words_of(text):
                score += 0.2
            try:
                ed = _dt.date.fromisoformat(str(e.get("date"))[:10])
                if due and abs((ed - due).days) <= 7:
                    score += 0.2
            except ValueError:
                pass
            cands.append({"tx_id": e.get("id"), "date": e.get("date"), "amount": ec / 100,
                          "counterparty": e.get("counterparty") or "", "score": round(score, 2)})
        cands.sort(key=lambda c: -c["score"])
        p["ledger_check"] = {"at": _now(), "candidates": cands[:3], "entries_scanned": len(entries),
                             **({"truncated": True} if truncated else {})}
    _event(rec, "payment", f"Ledger checked for {len(pending)} payment(s): "
           + ", ".join(f"{p['label']} {'candidates ' + str(len(p['ledger_check']['candidates'])) if p['ledger_check']['candidates'] else 'no match'}" for p in pending)
           + (f" (only the first {LEDGER_PAGE} movements of the window were read; a missing match proves nothing)" if truncated else ""))
    return {"checked": len(pending), "entries_scanned": len(entries), **({"truncated": True} if truncated else {})}


def confirm_payment(rec: Dict[str, Any], args: Mapping[str, Any]) -> Dict[str, Any]:
    label, tx = _s(args, "label", 160), _s(args, "tx_id", 120)
    if not label or not tx:
        raise OfficeError("office.bad_args", "label and tx_id are required")
    for p in rec.get("payments") or []:
        if p["label"] == label:
            cand = next((c for c in (p.get("ledger_check") or {}).get("candidates") or [] if c["tx_id"] == tx), None)
            p.update({"ledger_ref": tx, "confirmed": True, "received_on": (cand or {}).get("date") or _date(args.get("date"), "date")})
            _event(rec, "payment", f"{label} confirmed with Ledger movement {tx}")
            return p
    raise OfficeError("office.not_found", f"no payment {label!r}")


# ---------------------------------------------------------------------------
# Meetings -> requirements and open questions (E27)

def _meeting(rec: Mapping[str, Any], mid: str) -> Dict[str, Any]:
    for m in rec.get("meetings") or []:
        if m["id"] == mid:
            return m
    raise OfficeError("office.not_found", f"no meeting {mid!r} in this commission")


def _meeting_owner(owner: str) -> str:
    return "" if owner in ("", "_") else owner


def recent_meetings(owner: str) -> Dict[str, Any]:
    from src import meetings
    rows = meetings.list_meetings(owner=_meeting_owner(owner), limit=30)
    return {"meetings": [{"id": r.get("id"), "title": r.get("title") or r.get("id"), "date": r.get("date") or r.get("created") or ""}
                         for r in rows if r.get("id")]}


def import_meeting(owner: str, rec: Dict[str, Any], args: Mapping[str, Any]) -> Dict[str, Any]:
    from src import meeting_requirements as mr
    text = str(args.get("text") or "")
    source = _s(args, "source", 160)
    meeting_id = _s(args, "meeting_id", 120)
    date = _date(args.get("date"), "date")
    if meeting_id and not text.strip():
        from src import meetings
        found = meetings.get_meeting(meeting_id, owner=_meeting_owner(owner))
        if not found:
            raise OfficeError("office.not_found", f"no meeting {meeting_id!r}")
        text = str(found.get("markdown") or found.get("transcript") or "")
        source = source or str(found.get("title") or meeting_id)
        date = date or _date(str(found.get("date") or found.get("created") or "")[:10], "date")
    if not text.strip():
        raise OfficeError("office.bad_args", "text or meeting_id is required")
    items = mr.extract(text[:200000])
    for item in items:
        item["promoted"] = None
    rec.setdefault("meetings", [])
    m = {"id": _next_id(rec["meetings"], "R"), "source": source or "meeting", "meeting_id": meeting_id,
         "date": date or _today().isoformat(), "at": _now(), "items": items, "counts": mr.counts(items)}
    rec["meetings"].append(m)
    c = m["counts"]
    _event(rec, "meeting", f"{m['id']} {m['source']}: {c['decision']} decisions, {c['promise']} promises, "
                           f"{c['requirement']} requirements, {c['proposal']} proposals, {c['question']} questions")
    return m


def promote_meeting_item(owner: str, rec: Dict[str, Any], args: Mapping[str, Any]) -> Dict[str, Any]:
    m = _meeting(rec, _s(args, "meeting"))
    try:
        n = int(args.get("n"))
    except (TypeError, ValueError):
        raise OfficeError("office.bad_args", "n (item number) is required") from None
    item = next((i for i in m["items"] if i["n"] == n), None)
    if item is None:
        raise OfficeError("office.not_found", f"no item {n} in meeting {m['id']}")
    target = _s(args, "as")
    if target not in PROMOTE_AS:
        raise OfficeError("office.bad_args", f"as must be one of {', '.join(PROMOTE_AS)}")
    if item.get("promoted"):
        raise OfficeError("office.already_promoted", f"item {n} is already a {item['promoted']['as']}")
    if not item["firm"] and args.get("force") not in (True, "true", 1, "1"):
        raise OfficeError("office.not_firm", f"item {n} is a {item['kind']}: tentative wording or a question is never "
                                             "a commitment; pass force to promote it anyway")
    origin = " ".join(x for x in (f"{m['id']}#{n}", item.get("speaker"), item.get("time")) if x) + f" · {m['source']}"
    if target == "requirement":
        last = (rec.get("scope") or [None])[-1]
        text = f"{last['text'].rstrip()}\n- {item['text']}" if last else item["text"]
        version = _add_scope(owner, rec, {"kind": "change", "text": text, "source": origin, "lang": _s(args, "lang") or "es"})
        ref = f"v{version['version']}"
    elif target == "decision":
        _append(rec, "decisions", {"date": m["date"], "text": item["text"][:1000], "by": item.get("speaker") or "",
                                   "source": origin}, item["text"][:80])
        ref = f"#{len(rec['decisions'])}"
    else:
        ms = add_milestone(rec, {"title": item["text"][:160], "due": args.get("due")})
        ms["source"] = origin
        ref = ms["id"]
    item["promoted"] = {"as": target, "at": _now(), "ref": ref, "forced": not item["firm"]}
    _event(rec, "meeting", f"{m['id']}#{n} -> {target} {ref}")
    return item


def status(owner: str, rec: Mapping[str, Any]) -> Dict[str, Any]:
    from src import delivery_package
    today = _today()
    pending = [m for m in rec.get("milestones") or [] if not m.get("done")]
    overdue = [m["id"] for m in pending if m.get("due") and _dt.date.fromisoformat(m["due"]) < today]
    upcoming = sorted((m for m in pending if m.get("due")), key=lambda m: m["due"])
    deliverables = []
    for m in rec.get("milestones") or []:
        for d in m["deliverables"]:
            ready = None
            if d.get("package_id"):
                pkg = delivery_package.run(owner, {"action": "show", "id": d["package_id"]})
                ready = bool(pkg.get("ready")) and bool((pkg.get("reimport") or {}).get("ok")) if not pkg.get("error") else None
            deliverables.append({"milestone": m["id"], "title": d["title"], "package_id": d.get("package_id"), "ready": ready})
    agreed = _current_agreement(rec)
    last = (rec.get("scope") or [None])[-1]
    open_questions = (last or {}).get("comparison", {}).get("questions", []) if last and last.get("kind") == "change" else []
    expected = sum(float(m["amount"]) for m in rec.get("milestones") or [] if m.get("amount") is not None)
    pays = rec.get("payments") or []
    received = sum(float(p["amount"]) for p in pays if p.get("confirmed") and p.get("amount") is not None)
    to_confirm = [p["label"] for p in pays if not p.get("confirmed") and ((p.get("ledger_check") or {}).get("candidates"))]
    return {"stage": rec["stage"], "today": today.isoformat(),
            "agreement_version": agreed["version"] if agreed else None,
            "pending_change": bool(last and last["kind"] == "change"),
            "open_questions": open_questions,
            "next_milestone": {"id": upcoming[0]["id"], "title": upcoming[0]["title"], "due": upcoming[0]["due"]} if upcoming else None,
            "overdue": overdue, "milestones_done": sum(1 for m in rec.get("milestones") or [] if m.get("done")),
            "milestones_total": len(rec.get("milestones") or []),
            "deliverables": deliverables,
            "deliverables_without_ready_package": [d["title"] for d in deliverables if d["ready"] is not True],
            "billing_expected": expected if rec.get("milestones") else None,
            "payments_received": received if pays else None,
            "payments_to_confirm": to_confirm,
            "people_linked": sum(1 for c in rec.get("contacts") or [] if c.get("people_id")),
            "meeting_items_pending": sum(1 for mt in rec.get("meetings") or [] for i in mt["items"] if not i.get("promoted")),
            "meeting_open_questions": [f"{mt['id']}#{i['n']} {i['text']}" for mt in rec.get("meetings") or []
                                       for i in mt["items"] if i["kind"] == "question" and not i.get("promoted")],
            "budget": rec.get("budget")}


def list_commissions(owner: str) -> List[Dict[str, Any]]:
    d = _dir(owner)
    out = []
    if d.is_dir():
        for p in d.glob("*.json"):
            try:
                r = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            pending = [m for m in r.get("milestones") or [] if not m.get("done")]
            dues = sorted(m["due"] for m in pending if m.get("due"))
            out.append({"id": r["id"], "title": r["title"], "client": r.get("client", ""), "stage": r["stage"],
                        "updated": r.get("updated"), "next_due": dues[0] if dues else None,
                        "overdue": sum(1 for x in dues if _dt.date.fromisoformat(x) < _today()),
                        "scope_versions": len(r.get("scope") or [])})
    return sorted(out, key=lambda r: r.get("updated") or "", reverse=True)


def render(m: Dict[str, Any]) -> str:
    if "commissions" in m:
        return "\n".join(f"- {r['id']}: {r['title']} [{r['stage']}]" + (f" next {r['next_due']}" if r.get("next_due") else "")
                         for r in m["commissions"]) or "No commissions."
    if "title" in m and "stage" in m and "milestones" in m:
        st = m.get("status") or {}
        lines = [f"Commission {m['id']} '{m['title']}' [{m['stage']}] client {m.get('client') or '—'}",
                 f"Scope versions: {len(m['scope'])}; milestones {st.get('milestones_done', 0)}/{st.get('milestones_total', len(m['milestones']))}"]
        if st.get("overdue"):
            lines.append("Overdue: " + ", ".join(st["overdue"]))
        if st.get("deliverables_without_ready_package"):
            lines.append("Without a ready package: " + ", ".join(st["deliverables_without_ready_package"]))
        if st.get("open_questions"):
            lines.append("Open scope questions: " + " | ".join(st["open_questions"]))
        if st.get("meeting_open_questions"):
            lines.append("Open meeting questions: " + " | ".join(st["meeting_open_questions"]))
        for mt in m.get("meetings") or []:
            lines.append(f"Meeting {mt['id']} {mt['source']}: " + ", ".join(f"{k} {v}" for k, v in mt["counts"].items() if v))
            for i in mt["items"]:
                mark = f" -> {i['promoted']['as']}" if i.get("promoted") else ""
                lines.append(f"  {i['n']}. [{i['kind']}] {i.get('time') or ''} {i.get('speaker') or ''}: {i['text']}{mark}")
        return "\n".join(lines)
    return json.dumps(m, ensure_ascii=False)


def run(owner: str, args: Mapping[str, Any]) -> Dict[str, Any]:
    action = _s(args, "action") or "show"
    try:
        if action not in ACTIONS:
            raise OfficeError("office.bad_args", f"action must be one of {', '.join(ACTIONS)}")
        if action == "list":
            return {"commissions": list_commissions(owner)}
        if action == "people_lookup":
            return people_lookup(args)
        if action == "list_meetings":
            return recent_meetings(owner)
        if action == "create":
            rec = create(owner, args)
            return {**rec, "status": status(owner, rec)}
        rec = _load(owner, _s(args, "id"))
        if action in ("show", "status"):
            return {**rec, "status": status(owner, rec)}
        if action == "update":
            update(owner, rec, args)
        elif action == "add_scope":
            _add_scope(owner, rec, args)
        elif action == "add_milestone":
            add_milestone(rec, args)
        elif action == "update_milestone":
            update_milestone(rec, args)
        elif action == "link_package":
            link_package(owner, rec, args)
        elif action == "add_contact":
            name = _s(args, "name", 120)
            if not name:
                raise OfficeError("office.bad_args", "contact name is required")
            _append(rec, "contacts", {"name": name, "role": _s(args, "role", 80), "email": _s(args, "email", 160),
                                      "people_id": _s(args, "people_id", 80)}, name)
        elif action == "add_file":
            path = _s(args, "path", 1000)
            if not path:
                raise OfficeError("office.bad_args", "path is required")
            _append(rec, "files", {"path": path, "role": _s(args, "role", 40) or "other", "note": _s(args, "note", 300)},
                    Path(path).name)
        elif action == "add_decision":
            text = _s(args, "text", 1000)
            if not text:
                raise OfficeError("office.bad_args", "decision text is required")
            _append(rec, "decisions", {"date": _date(args.get("date"), "date") or _today().isoformat(), "text": text,
                                       "by": _s(args, "by", 80)}, text[:80])
        elif action == "add_payment":
            label = _s(args, "label", 160)
            if not label:
                raise OfficeError("office.bad_args", "payment label is required")
            try:
                amount = float(args.get("amount")) if args.get("amount") is not None else None
            except (TypeError, ValueError):
                raise OfficeError("office.bad_args", "amount must be a number") from None
            _append(rec, "payments", {"label": label, "amount": amount, "due": _date(args.get("due"), "due"),
                                      "ledger_ref": _s(args, "ledger_ref", 120)}, label)
        elif action == "link_person":
            link_person(rec, args)
        elif action == "reconcile_payments":
            summary = reconcile_payments(rec)
            _save(owner, rec)
            return {**rec, "status": status(owner, rec), "reconcile": summary}
        elif action == "confirm_payment":
            confirm_payment(rec, args)
        elif action == "import_meeting":
            import_meeting(owner, rec, args)
        elif action == "promote_meeting_item":
            promote_meeting_item(owner, rec, args)
        elif action == "proposal":
            from src import proposal_builder
            result = proposal_builder.build(owner, rec, status(owner, rec), args)
            rec.setdefault("proposals", []).append({"name": result["name"], "date": result["date"],
                                                    "counts": result["counts"], "masked": len(result["leaks"])})
            if rec["stage"] == "opportunity":
                _event(rec, "stage", "opportunity -> proposal")
                rec["stage"] = "proposal"
            _event(rec, "proposal", f"{result['name']} ({result['counts']['verified']} verified, "
                                    f"{result['counts']['assumption']} assumptions, {result['counts']['question']} questions)")
            _save(owner, rec)
            return {**rec, "status": status(owner, rec), "proposal": result}
        _save(owner, rec)
        return {**rec, "status": status(owner, rec)}
    except OfficeError as exc:
        return _err(exc.error_class, exc.detail)
    except (ValueError, OSError) as exc:
        return _err("office.refused", str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.warning("commission office failed", exc_info=True)
        return _err("office.error", f"{type(exc).__name__}: {exc}")
