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
ACTIONS = ("create", "update", "list", "show", "add_scope", "add_milestone", "update_milestone", "link_package", "link_review",
           "add_contact", "add_file", "add_decision", "add_payment", "status", "proposal",
           "people_lookup", "link_person", "reconcile_payments", "confirm_payment",
           "list_meetings", "import_meeting", "promote_meeting_item", "delete", "delete_meeting")
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


def link_review(owner: str, rec: Dict[str, Any], args: Mapping[str, Any]) -> Dict[str, Any]:
    """File a review desk (E32) under a milestone. The desk keeps the link (its ``commission`` and ``milestone``);
    the milestone then shows «delivered vN / open comments» read live from the desk."""
    from src import review_desk
    m = _milestone(rec, _s(args, "milestone"))
    did = _s(args, "review_id", 80)
    try:
        desk = review_desk.attach(owner, did, rec["id"], m["id"])
    except review_desk.DeskError:
        raise OfficeError("office.unknown_review", f"no review desk {did!r}") from None
    _event(rec, "delivery", f"{m['id']} ← review {desk['title']}")
    return m


def note_review_delivery(owner: str, desk: Mapping[str, Any]) -> None:
    """Called by the review desk when it delivers a version: leaves an event on the commission it is filed under."""
    d = desk.get("delivered") or {}
    try:
        rec = _load(owner, str(desk.get("commission") or ""))
    except OfficeError:
        return
    open_n = len(d.get("open_comments") or [])
    _event(rec, "delivery", f"{desk.get('milestone') or '—'} review «{desk.get('title')}» delivered v{d.get('version')} {d.get('name') or ''}"
           + (f" with {open_n} open comment(s)" if open_n else ""))
    _save(owner, rec)


def _reviews(owner: str, rec: Mapping[str, Any]) -> List[Dict[str, Any]]:
    try:
        from src import review_desk
        return [{"id": d["id"], "title": d["title"], "milestone": d.get("milestone") or "", "latest": d.get("latest"),
                 "delivered": d.get("delivered"), "open": d.get("open", 0), "versions": d.get("versions", 0)}
                for d in review_desk.list_desks(owner) if d.get("commission") == rec.get("id")]
    except Exception:  # noqa: BLE001
        logger.debug("commission office: review desks not readable", exc_info=True)
        return []


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


MAX_COMBINED = 3          # a payment received in at most this many incoming movements
COMBO_POOL = 40           # movements considered for combinations (the closest in date and wording)


def _received(p: Mapping[str, Any]) -> float:
    return round(sum(float(r.get("amount") or 0) for r in p.get("receipts") or []), 2)


def _used_tx(rec: Mapping[str, Any]) -> set:
    used = {str(r.get("tx_id")) for p in rec.get("payments") or [] for r in p.get("receipts") or []}
    used |= {str(p.get("ledger_ref")) for p in rec.get("payments") or [] if p.get("confirmed") and p.get("ledger_ref") and not p.get("receipts")}
    return used


def _score(e: Mapping[str, Any], due: Optional[_dt.date], words: set) -> float:
    score = 0.0
    if words & _words_of(f"{e.get('counterparty') or ''} {e.get('note') or ''}"):
        score += 0.2
    try:
        ed = _dt.date.fromisoformat(str(e.get("date"))[:10])
        if due and abs((ed - due).days) <= 7:
            score += 0.2
    except ValueError:
        pass
    return score


def _combinations(pool: List[Dict[str, Any]], cents: int) -> List[List[Dict[str, Any]]]:
    """Groups of 2..MAX_COMBINED movements whose amounts add up to ``cents`` (±1 cent)."""
    from itertools import combinations
    out = []
    for k in range(2, MAX_COMBINED + 1):
        for group in combinations(pool, k):
            if abs(sum(e["_c"] for e in group) - cents) <= 1:
                out.append(list(group))
                if len(out) >= 20:
                    return out
    return out


def reconcile_payments(rec: Dict[str, Any]) -> Dict[str, Any]:
    """Look in Ledger (read-only) for each pending payment: one incoming movement of exactly what is still owed,
    two or three movements that add up to it, or a single smaller movement from the same client (a partial payment)."""
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
    used = _used_tx(rec)
    incoming = []
    for e in entries:
        try:
            ec = int(e.get("amount_cents"))
        except (TypeError, ValueError):
            continue
        if ec > 0 and e.get("id") is not None and str(e.get("id")) not in used:
            incoming.append({**e, "_c": ec})

    def cand(group: List[Dict[str, Any]], kind: str, score: float) -> Dict[str, Any]:
        return {"kind": kind, "tx_id": str(group[0].get("id")), "tx_ids": [str(e.get("id")) for e in group],
                "date": group[-1].get("date"), "dates": [e.get("date") for e in group],
                "amount": round(sum(e["_c"] for e in group) / 100, 2), "amounts": [e["_c"] / 100 for e in group],
                "counterparty": ", ".join(dict.fromkeys(str(e.get("counterparty") or "") for e in group)), "score": round(score, 2)}

    for p in pending:
        owed = round(float(p["amount"]) - _received(p), 2)
        cents = round(owed * 100)
        due = _dt.date.fromisoformat(p["due"]) if p.get("due") else None
        words = _words_of(p["label"]) | client_words
        scored = [(e, _score(e, due, words)) for e in incoming]
        exact = sorted((cand([e], "exact", 0.6 + s) for e, s in scored if abs(e["_c"] - cents) <= 1), key=lambda c: -c["score"])
        smaller = sorted(((e, s) for e, s in scored if e["_c"] < cents - 1), key=lambda x: -x[1])[:COMBO_POOL]
        combos = sorted((cand(sorted(g, key=lambda e: str(e.get("date"))), "combined", 0.5 + min(_score(e, due, words) for e in g))
                         for g in _combinations([e for e, _ in smaller], cents)), key=lambda c: -c["score"])
        partial = sorted((cand([e], "partial", 0.3 + s) for e, s in smaller                    # only from the same client or label
                          if words & _words_of(f"{e.get('counterparty') or ''} {e.get('note') or ''}")), key=lambda c: -c["score"])
        p["ledger_check"] = {"at": _now(), "owed": owed, "candidates": exact[:3] + combos[:3] + partial[:3],
                             "entries_scanned": len(entries), **({"truncated": True} if truncated else {})}
    _event(rec, "payment", f"Ledger checked for {len(pending)} payment(s): "
           + ", ".join(f"{p['label']} {'candidates ' + str(len(p['ledger_check']['candidates'])) if p['ledger_check']['candidates'] else 'no match'}" for p in pending)
           + (f" (only the first {LEDGER_PAGE} movements of the window were read; a missing match proves nothing)" if truncated else ""))
    return {"checked": len(pending), "entries_scanned": len(entries), **({"truncated": True} if truncated else {})}


def confirm_payment(rec: Dict[str, Any], args: Mapping[str, Any]) -> Dict[str, Any]:
    """Record the Ledger movement(s) received for a payment. ``tx_ids`` (or one ``tx_id``) are added to what was
    already received; the payment is confirmed when the movements cover its amount, otherwise it stays partly paid.
    The amount of each movement comes from the last Ledger check; ``amount`` gives it for a single movement that was
    not among the candidates (without it, a lone unknown movement is taken as the whole payment, as before)."""
    label = _s(args, "label", 160)
    raw = args.get("tx_ids")
    ids = [str(x).strip()[:120] for x in (raw if isinstance(raw, list) else str(raw or "").split(",")) if str(x).strip()]
    if _s(args, "tx_id", 120) and _s(args, "tx_id", 120) not in ids:
        ids.insert(0, _s(args, "tx_id", 120))
    if not label or not ids:
        raise OfficeError("office.bad_args", "label and tx_id (or tx_ids) are required")
    p = next((x for x in rec.get("payments") or [] if x["label"] == label), None)
    if p is None:
        raise OfficeError("office.not_found", f"no payment {label!r}")
    known: Dict[str, Dict[str, Any]] = {}
    for c in (p.get("ledger_check") or {}).get("candidates") or []:
        for i, tid in enumerate(c.get("tx_ids") or [c.get("tx_id")]):
            amounts, dates = c.get("amounts") or [c.get("amount")], c.get("dates") or [c.get("date")]
            known[str(tid)] = {"amount": amounts[i] if i < len(amounts) else None, "date": dates[i] if i < len(dates) else None}
    used = _used_tx(rec) - {str(r.get("tx_id")) for r in p.get("receipts") or []}
    clash = [i for i in ids if i in used]
    if clash:
        raise OfficeError("office.bad_args", f"movement(s) {', '.join(clash)} already recorded for another payment")
    receipts = p.setdefault("receipts", [])
    have = {str(r["tx_id"]) for r in receipts}
    total = float(p["amount"]) if p.get("amount") is not None else None
    for tid in ids:
        if tid in have:
            continue
        info = known.get(tid) or {}
        amount = info.get("amount")
        if amount is None and args.get("amount") is not None and len(ids) == 1:
            try:
                amount = float(args["amount"])
            except (TypeError, ValueError):
                raise OfficeError("office.bad_args", "amount must be a number") from None
        if amount is None and len(ids) == 1 and total is not None:
            amount = round(total - _received(p), 2)
        if amount is None:
            raise OfficeError("office.bad_args", f"amount of movement {tid!r} is unknown: run reconcile_payments first or pass the movements it offered")
        receipts.append({"tx_id": tid, "amount": amount, "date": info.get("date") or _date(args.get("date"), "date")})
    got = _received(p)
    p["received"] = got
    p["ledger_ref"] = ", ".join(str(r["tx_id"]) for r in receipts)
    full = total is None or got >= total - 0.01
    p["confirmed"] = full
    p["received_on"] = max((str(r.get("date") or "") for r in receipts), default="") or None
    _event(rec, "payment", f"{label}: {', '.join(ids)} from Ledger" + (" — paid" if full else f" — {got:g} of {total:g} received"))
    return p


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
    received = round(sum(_received(p) if p.get("receipts") else float(p["amount"]) for p in pays
                         if (p.get("receipts") or p.get("confirmed")) and p.get("amount") is not None), 2)
    to_confirm = [p["label"] for p in pays if not p.get("confirmed") and ((p.get("ledger_check") or {}).get("candidates"))]
    partly = [{"label": p["label"], "received": _received(p), "amount": p["amount"]} for p in pays
              if not p.get("confirmed") and p.get("receipts")]
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
            "payments_partly_paid": partly,
            "reviews": _reviews(owner, rec),
            "people_linked": sum(1 for c in rec.get("contacts") or [] if c.get("people_id")),
            "meeting_items_pending": sum(1 for mt in rec.get("meetings") or [] for i in mt["items"] if not i.get("promoted")),
            "meeting_open_questions": [f"{mt['id']}#{i['n']} {i['text']}" for mt in rec.get("meetings") or []
                                       for i in mt["items"] if i["kind"] == "question" and not i.get("promoted")],
            "budget": rec.get("budget")}


# ---------------------------------------------------------------------------
# Delete (the record and what is derived from it; never the user's files)

def _records(owner: str) -> List[Dict[str, Any]]:
    d = _dir(owner)
    out = []
    if d.is_dir():
        for p in d.glob("*.json"):
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
    return out


def _package_ids(rec: Mapping[str, Any]) -> List[str]:
    return sorted({d["package_id"] for m in rec.get("milestones") or [] for d in m.get("deliverables") or [] if d.get("package_id")})


def _comparison_ids(rec: Mapping[str, Any]) -> List[str]:
    return sorted({v["comparison"]["id"] for v in rec.get("scope") or [] if (v.get("comparison") or {}).get("id")})


def commissions_linking_package(owner: str, pkg_id: str) -> List[Dict[str, str]]:
    return [{"id": r["id"], "title": r["title"]} for r in _records(owner) if pkg_id in _package_ids(r)]


def commissions_linking_comparison(owner: str, cid: str) -> List[Dict[str, str]]:
    return [{"id": r["id"], "title": r["title"]} for r in _records(owner) if cid in _comparison_ids(r)]


def delete(owner: str, rec: Mapping[str, Any], with_linked: bool = False) -> Dict[str, Any]:
    """Delete a commission. Always removed: the record, its drafted proposals and the
    scope comparisons made from its own scope versions. Kept unless ``with_linked``:
    its delivery packages (unless another commission links them) and the review desks
    filed under it. Never touched: imported Faustus meetings (the record only holds
    copies of their statements), the files listed in it and any source file."""
    import shutil
    from src import delivery_package, review_desk, scope_compare
    cid = rec["id"]
    others = [r for r in _records(owner) if r["id"] != cid]
    shared = {p for r in others for p in _package_ids(r)}
    pkgs = _package_ids(rec)
    desks = [d["id"] for d in review_desk.list_desks(owner) if d.get("commission") == cid]
    removed: Dict[str, Any] = {"record": cid, "proposals": 0, "scope_comparisons": [], "packages": [], "review_desks": []}
    kept: Dict[str, Any] = {"packages": [], "review_desks": [], "imported_meetings": len(rec.get("meetings") or []),
                            "listed_files": len(rec.get("files") or []), "source_files": "never touched"}
    folder = _dir(owner) / cid
    if folder.is_dir():
        removed["proposals"] = sum(1 for p in (folder / "proposals").glob("propuesta-*.docx")) if (folder / "proposals").is_dir() else 0
        shutil.rmtree(folder)
    (_dir(owner) / f"{cid}.json").unlink()
    for cmp_id in _comparison_ids(rec):
        res = scope_compare.run(owner, {"action": "delete", "id": cmp_id})
        if not res.get("error"):
            removed["scope_comparisons"].append(cmp_id)
    for pid in pkgs:
        if pid in shared:
            kept["packages"].append({"id": pid, "reason": "linked from another commission"})
        elif with_linked and not delivery_package.run(owner, {"action": "delete", "id": pid}).get("error"):
            removed["packages"].append(pid)
        else:
            kept["packages"].append({"id": pid, "reason": "kept (with_linked not set)" if not with_linked else "already gone"})
    for did in desks:
        if with_linked and not review_desk.run(owner, {"action": "delete", "id": did}).get("error"):
            removed["review_desks"].append(did)
        else:
            kept["review_desks"].append({"id": did, "reason": "kept (with_linked not set)"})
    return {"deleted": cid, "title": rec.get("title"), "removed": removed, "kept": kept}


def delete_meeting(owner: str, args: Mapping[str, Any]) -> Dict[str, Any]:
    """Delete the notes of a Faustus meeting. Commissions that imported it keep their copy of the statements."""
    from src import meetings
    mid = _s(args, "meeting_id", 120)
    if not mid:
        raise OfficeError("office.bad_args", "meeting_id is required")
    res = meetings.delete_meeting(mid, owner=_meeting_owner(owner))
    if res is None:
        raise OfficeError("office.not_found", f"no meeting {mid!r}")
    imported = [{"id": r["id"], "title": r["title"]} for r in _records(owner)
                if any(m.get("meeting_id") == mid for m in r.get("meetings") or [])]
    return {"deleted_meeting": mid, "title": res["title"], "removed": res["removed"],
            "kept": {"recording_and_job_folder": "never touched", "copies_in_commissions": imported}}


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
        if action == "delete_meeting":
            return delete_meeting(owner, args)
        if action == "create":
            rec = create(owner, args)
            return {**rec, "status": status(owner, rec)}
        rec = _load(owner, _s(args, "id"))
        if action == "delete":
            return delete(owner, rec, args.get("with_linked") in (True, "true", 1, "1"))
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
        elif action == "link_review":
            link_review(owner, rec, args)
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
