"""proposal_builder.py -- professional proposal from a commission (Hangar proposal E31).

Builds a proposal draft (Word document plus Markdown) from the record of the
commission office (``src/commission_office.py``): context from the first
request, the agreed scope, pending change requests, plan and milestones,
amounts, assumptions, exclusions and the questions still open. Every statement
carries where it comes from, so the reader can tell:

* **verified** -- taken from the commission record (agreed scope, milestones,
  recorded decisions, delivery packages read as ready);
* **assumption** -- given by the author for this proposal;
* **question** -- still open (scope comparison questions, ambiguous clauses).

Before anything is written, the text goes through a **leak check**: local
paths, internal tool and family names, package ids and e-mail addresses that
are not the client's are masked, and each masking is listed in the result.
Nothing is invented: a section without data says so instead of being filled.

Files go to DATA_DIR/commissions/<owner>/<commission id>/proposals/.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Tuple

_INTERNAL = ["Faustus", "Hoard", "HoardLink", "Hoard Hub", "Odysseus", "Ágora", "Agora", "Vulcan", "Vitruvius",
             "Nightingale", "Dantzig", "Maxwell", "Shannon", "Atlas", "Ledger", "People's", "Prometheus", "Sparks",
             "Theseus", "Heron", "Gulliver", "Daedalus", "Babel", "Darwin", "Kafka", "Cicero", "Writer's", "Coase",
             "MCP", "Ollama", "derivation graph", "decision map"]
_PATH = re.compile(r"(?:[A-Za-z]:\\[^\s,;)]+|\\\\[^\s,;)]+|(?<![\w.])/(?:home|Users|mnt|tmp|var|opt)/[^\s,;)]+)")
_PKG = re.compile(r"\b\d{8}-\d{6}-[0-9a-f]{6}\b")
_MAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

_T = {
    "es": {"title": "Propuesta", "client": "Cliente", "date": "Fecha", "context": "Contexto", "scope": "Alcance acordado",
           "pending": "Cambios solicitados pendientes de acordar", "plan": "Plan e hitos", "budget": "Importe",
           "assumptions": "Supuestos", "exclusions": "Fuera del alcance", "questions": "Preguntas abiertas",
           "decisions": "Decisiones tomadas", "legend": "Origen de cada punto: [verificado] sale del expediente del encargo; "
           "[supuesto] lo indica esta propuesta; [pregunta] sigue abierta.",
           "verified": "verificado", "assumption": "supuesto", "question": "pregunta", "none": "Sin datos en el expediente.",
           "total": "Total de los hitos", "budget_line": "Presupuesto indicado", "due": "fecha", "ready": "entrega comprobada"},
    "en": {"title": "Proposal", "client": "Client", "date": "Date", "context": "Context", "scope": "Agreed scope",
           "pending": "Requested changes not yet agreed", "plan": "Plan and milestones", "budget": "Amount",
           "assumptions": "Assumptions", "exclusions": "Out of scope", "questions": "Open questions",
           "decisions": "Decisions taken", "legend": "Origin of each point: [verified] comes from the commission record; "
           "[assumption] is stated by this proposal; [question] is still open.",
           "verified": "verified", "assumption": "assumption", "question": "question", "none": "No data in the record.",
           "total": "Milestones total", "budget_line": "Stated budget", "due": "due", "ready": "delivery checked"},
}


def _clauses(text: str) -> List[str]:
    from src.scope_compare import clauses
    return clauses(text)


def _mask(text: str, allowed_mails: set, extra: List[str], leaks: List[Dict[str, str]]) -> str:
    def sub(rx, kind, s):
        def rep(m):
            val = m.group(0)
            if kind == "email" and val.lower() in allowed_mails:
                return val
            leaks.append({"kind": kind, "value": val})
            return "[…]"
        return rx.sub(rep, s)
    text = sub(_PATH, "path", text)
    text = sub(_PKG, "internal id", text)
    text = sub(_MAIL, "email", text)
    for term in sorted(set(_INTERNAL + [t for t in extra if t]), key=len, reverse=True):
        rx = re.compile(r"(?<![\w])" + re.escape(term) + r"(?:'s)?(?![\w])", re.I)
        if rx.search(text):
            for m in rx.finditer(text):
                leaks.append({"kind": "internal name", "value": m.group(0)})
            text = rx.sub("[…]", text)
    return text


def build(owner: str, rec: Mapping[str, Any], status: Mapping[str, Any], args: Mapping[str, Any]) -> Dict[str, Any]:
    lang = str(args.get("lang") or "es").lower()
    t = _T.get(lang, _T["es"])
    lines_of = lambda v: [x.strip(" -*\t") for x in (v.splitlines() if isinstance(v, str) else (v or [])) if str(x).strip()]
    assumptions = lines_of(args.get("assumptions"))
    exclusions = lines_of(args.get("exclusions"))
    extra_terms = lines_of(args.get("internal_terms"))
    sections: List[Dict[str, Any]] = []

    def add(title: str, items: List[Tuple[str, str]]):
        sections.append({"title": title, "items": [{"text": x, "origin": o} for x, o in items]})

    scope = rec.get("scope") or []
    initial = next((v for v in scope if v["kind"] == "initial"), None)
    agreed = next((v for v in reversed(scope) if v["kind"] == "agreement"), None)
    pending = [v for v in scope if v["kind"] == "change" and (not agreed or v["version"] > agreed["version"])]
    add(t["context"], [(c, "verified") for c in _clauses(initial["text"])] if initial else [])
    add(t["scope"], [(c, "verified") for c in _clauses(agreed["text"])] if agreed else [])
    if pending:
        add(t["pending"], [(c, "question") for v in pending for c in _clauses(v["text"])])
    plan = []
    for m in rec.get("milestones") or []:
        bits = [m["title"]]
        if m.get("due"):
            bits.append(f"{t['due']} {m['due']}")
        if m.get("amount") is not None:
            bits.append(f"{m['amount']:.2f} {(rec.get('budget') or {}).get('currency', 'EUR')}")
        ready = [d["title"] for d in status.get("deliverables") or [] if d["milestone"] == m["id"] and d.get("ready")]
        if ready:
            bits.append(f"{t['ready']}: {', '.join(ready)}")
        plan.append((" · ".join(bits), "verified"))
    add(t["plan"], plan)
    money = []
    if rec.get("budget"):
        b = rec["budget"]
        money.append((f"{t['budget_line']}: {b['amount']:.2f} {b['currency']}", "verified"))
    if status.get("billing_expected") is not None:
        money.append((f"{t['total']}: {status['billing_expected']:.2f} {(rec.get('budget') or {}).get('currency', 'EUR')}", "verified"))
    add(t["budget"], money)
    add(t["decisions"], [(f"{d['date']}: {d['text']}", "verified") for d in rec.get("decisions") or []])
    add(t["assumptions"], [(a, "assumption") for a in assumptions])
    add(t["exclusions"], [(x, "assumption") for x in exclusions])
    questions = list(status.get("open_questions") or [])
    add(t["questions"], [(q, "question") for q in questions])

    allowed = {c.get("email", "").lower() for c in rec.get("contacts") or [] if c.get("email")}
    leaks: List[Dict[str, str]] = []
    title = _mask(f"{t['title']}: {rec['title']}", allowed, extra_terms, leaks)
    client = _mask(rec.get("client") or "", allowed, extra_terms, leaks)
    for s in sections:
        for it in s["items"]:
            it["text"] = _mask(it["text"], allowed, extra_terms, leaks)
    date = time.strftime("%Y-%m-%d")
    md = [f"# {title}", "", f"**{t['client']}:** {client or '—'}  ", f"**{t['date']}:** {date}", "", f"_{t['legend']}_", ""]
    for s in sections:
        md += [f"## {s['title']}", ""]
        md += [f"- {it['text']} [{t[it['origin']]}]" for it in s["items"]] or [t["none"]]
        md.append("")
    base = Path(_dir(owner, rec["id"]))
    base.mkdir(parents=True, exist_ok=True)
    n = len(list(base.glob("propuesta-*.md"))) + 1
    stem = f"propuesta-{n}"
    (base / f"{stem}.md").write_text("\n".join(md), encoding="utf-8")
    docx_path = base / f"{stem}.docx"
    _docx(docx_path, title, client, date, t, sections)
    result = {"name": stem, "title": title, "client": client, "date": date, "sections": sections, "leaks": leaks,
              "files": {"docx": str(docx_path), "md": str(base / f"{stem}.md")},
              "counts": {o: sum(1 for s in sections for it in s["items"] if it["origin"] == o) for o in ("verified", "assumption", "question")}}
    (base / f"{stem}.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return result


def _docx(path: Path, title: str, client: str, date: str, t: Mapping[str, str], sections: List[Dict[str, Any]]) -> None:
    from docx import Document
    doc = Document()
    doc.add_heading(title, level=0)
    doc.add_paragraph(f"{t['client']}: {client or '—'}")
    doc.add_paragraph(f"{t['date']}: {date}")
    p = doc.add_paragraph(t["legend"])
    p.runs[0].italic = True
    for s in sections:
        doc.add_heading(s["title"], level=1)
        if not s["items"]:
            doc.add_paragraph(t["none"])
        for it in s["items"]:
            para = doc.add_paragraph(style="List Bullet")
            para.add_run(it["text"] + " ")
            tag = para.add_run(f"[{t[it['origin']]}]")
            tag.italic = True
    doc.save(str(path))


def _dir(owner: str, cid: str) -> Path:
    from src.commission_office import _dir as office_dir
    return office_dir(owner) / cid / "proposals"


def proposal_file(owner: str, cid: str, name: str, kind: str) -> Path:
    if not re.fullmatch(r"propuesta-\d{1,4}", name or "") or kind not in ("docx", "md"):
        raise FileNotFoundError(name)
    p = _dir(owner, cid) / f"{name}.{kind}"
    if not p.is_file():
        raise FileNotFoundError(name)
    return p
