"""scope_compare.py -- scope comparator (Hangar proposal E30).

Between the initial request, the agreement and a new request, says which
clauses of the new request are already included, changed, added, removed or
ambiguous, and -- only when there is ground for it -- what each one affects:

* **Clauses** are split from the texts (lines, bullets, sentences) and matched
  by overlap of normalised words (accents folded, stop words out, short
  stems); formats (PDF, DOCX, STL...) and quantities with units (40 mm, 3
  páginas, 20 %) are compared apart, so "exportar en DOCX" against "exportar
  en PDF" is a *change*, not new work.
* **Effects** come from the decision map of ``src/derivation_graph.py`` (E23):
  a clause that names a map item (by its exact title words or a format) is
  followed through ``affected_by`` to the derivation nodes and deliverables
  that depend on it. One node is a *local* change, most of the graph is a
  *pipeline* change. Without a map, or without an item the clause names, the
  effect is ``unknown`` and the answer says what to link -- nothing is guessed
  by meaning.
* **Effort** is only a sum of the ranges the caller gives per derivation node
  (``effort_hours``); nodes without a range are listed, never filled in.
* **Commercial options** per clause follow from status and effect (absorb,
  change order with the range, phase 2, keep the agreed version, ask) and
  ambiguous clauses become concrete questions for the client.

Comparisons are kept under DATA_DIR/scope_comparisons/<owner>/. One dispatcher,
``run(owner, args)``, serves the chat tool, HTTP and MCP; errors come back as
``{"error", "error_class", "exit_code": 1}``.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

ACTIONS = ("compare", "list", "show")
STATUSES = ("included", "changed", "added", "removed", "ambiguous")
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")

_STOP = set("""
a al algo ante como con cual cuando de del desde donde e el ella ellas ellos en entre era es esa ese eso esta este
esto estos estas fue ha hay la las le les lo los mas me mi mis muy ni no nos o os para pero por que se ser si sin
sobre son su sus tambien te todo todos tu un una unas uno unos y ya debe deben tiene tienen hacer hace haga
the a an and or of to in on for with by from at as is are be be will should must can it its this that these those
we you they our your their into than then also please need needs
""".split())
_VAGUE = re.compile(r"\b(etc|algo|mejor(?:ar|as|a)?|bonit[oa]s?|flexible|similar|parecid[oa]|lo que haga falta|"
                    r"lo necesario|cuanto antes|rapid[oa]|some|better|nicer|whatever|as needed|asap|flexible|"
                    r"similar|improve[ds]?|etcetera|y demas|tipo)\b")
_REMOVE = re.compile(r"\b(quitar|quita|quitad|eliminar|elimina|retirar|retira|ya no|no hace falta|no hacen falta|"
                     r"no incluir|sin incluir|prescindir|remove|no longer|leave out)\b")
_FORMATS = {"pdf", "docx", "doc", "xlsx", "xls", "csv", "pptx", "ppt", "stl", "step", "stp", "glb", "gltf", "obj",
            "3mf", "png", "jpg", "jpeg", "svg", "html", "json", "mp4", "dxf", "ifc", "md", "odt", "ods"}
_UNIT_WORDS = {"milimetros": "milimetros", "milimetro": "milimetros", "millimeters": "milimetros",
               "millimetres": "milimetros", "centimetros": "centimetros", "centimeters": "centimetros",
               "metros": "metros", "meters": "metros", "metres": "metros", "pulgadas": "pulgadas",
               "pulgada": "pulgadas", "inches": "pulgadas", "inch": "pulgadas", "pies": "pies", "feet": "pies",
               "kilos": "kilos", "kilogramos": "kilos", "gramos": "gramos", "libras": "libras", "pounds": "libras",
               "euros": "euros", "euro": "euros", "dolares": "dolares", "dollars": "dolares", "usd": "dolares",
               "espanol": "espanol", "castellano": "espanol", "spanish": "espanol", "ingles": "ingles",
               "english": "ingles", "frances": "frances", "french": "frances", "aleman": "aleman", "german": "aleman",
               "color": "color", "colour": "color", "blanco": "blanco", "negro": "negro", "white": "blanco", "black": "negro"}
_UNITS = r"(mm|cm|m|km|kg|g|eur|euros?|€|h|horas?|hours?|dias?|days?|semanas?|weeks?|px|%|paginas?|pages?|piezas?|unidades?|units?|idiomas?|languages?)"
_QTY = re.compile(r"(\d+(?:[.,]\d+)?)\s*" + _UNITS + r"\b")

_TEXT = {
    "es": {
        "included": "Ya incluido en lo acordado ({ref}); no hace falta orden de cambio.",
        "absorb": "Asumirlo dentro del alcance actual (efecto local: {nodes}).",
        "small_co": "Orden de cambio pequeña{rng}.",
        "co": "Orden de cambio{rng}: rehace {n} paso(s) derivados ({nodes}).",
        "phase2": "Dejarlo para una segunda fase.",
        "keep": "Mantener la versión acordada ({ref}).",
        "remove": "Retirarlo y replanificar; ya hay trabajo hecho en: {nodes}.",
        "remove_plain": "Retirarlo y replanificar.",
        "unknown_effect": "Efecto sin fundamento: enlaza el requisito en el mapa de decisiones para saber qué rehacer.",
        "q_vague": "¿Qué significa exactamente «{word}» en «{clause}»? Concretar el criterio de aceptación.",
        "q_two": "¿«{clause}» se refiere a «{a}» o a «{b}»?",
        "q_partial": "¿«{clause}» sustituye a «{a}» o es un trabajo nuevo?",
        "q_remove": "¿Qué parte del alcance acordado se quiere quitar con «{clause}»?",
        "rng": " de {lo}–{hi} h",
        "rng_partial": " de al menos {lo}–{hi} h (sin rango para: {missing})",
    },
    "en": {
        "included": "Already in the agreed scope ({ref}); no change order needed.",
        "absorb": "Absorb it within the current scope (local effect: {nodes}).",
        "small_co": "Small change order{rng}.",
        "co": "Change order{rng}: reruns {n} derived step(s) ({nodes}).",
        "phase2": "Leave it for a second phase.",
        "keep": "Keep the agreed version ({ref}).",
        "remove": "Remove it and re-plan; work already done in: {nodes}.",
        "remove_plain": "Remove it and re-plan.",
        "unknown_effect": "Effect not grounded: link the requirement in the decision map to know what to redo.",
        "q_vague": "What exactly does \"{word}\" mean in \"{clause}\"? Pin down the acceptance criterion.",
        "q_two": "Does \"{clause}\" refer to \"{a}\" or to \"{b}\"?",
        "q_partial": "Does \"{clause}\" replace \"{a}\" or is it new work?",
        "q_remove": "Which part of the agreed scope should \"{clause}\" remove?",
        "rng": " of {lo}–{hi} h",
        "rng_partial": " of at least {lo}–{hi} h (no range for: {missing})",
    },
}


class ScopeError(Exception):
    def __init__(self, error_class: str, detail: str):
        super().__init__(detail)
        self.error_class = error_class
        self.detail = detail


def _err(error_class: str, detail: str) -> Dict[str, Any]:
    return {"error": detail, "error_class": error_class, "exit_code": 1}


# ---------------------------------------------------------------------------
# text

def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(ch for ch in text if not unicodedata.combining(ch)).replace("€", " eur ")


def clauses(text: str) -> List[str]:
    out: List[str] = []
    for line in (text or "").splitlines():
        line = re.sub(r"^\s*(?:[-*•]|\d+[.)]|[a-z][.)])\s+", "", line).strip()
        if not line:
            continue
        for part in re.split(r"(?<=[.;!?])\s+(?=[A-ZÁÉÍÓÚÑ0-9¿¡])", line):
            part = part.strip().rstrip(".;")
            if len(_words(part)) >= 2 or _formats(part) or _quantities(part):
                out.append(part)
    return out


def _words(text: str) -> List[str]:
    return [w for w in re.findall(r"[a-z0-9]+", _fold(text)) if w not in _STOP and not w.isdigit()]


def _stem(word: str) -> str:
    return word[:5] if len(word) > 5 else word


def _stems(text: str) -> set:
    return {_stem(w) for w in _words(text) if w not in _FORMATS and w not in _UNIT_WORDS}


def _formats(text: str) -> set:
    """Categorical attributes a clause pins down: file formats and unit or
    language words (canonicalised), compared apart from the other words."""
    out = set()
    for w in re.findall(r"[a-z0-9]+", _fold(text)):
        if w in _FORMATS:
            out.add(w)
        elif w in _UNIT_WORDS:
            out.add(_UNIT_WORDS[w])
    return out


def _quantities(text: str) -> Dict[str, str]:
    q: Dict[str, str] = {}
    for num, unit in _QTY.findall(_fold(text)):
        u = unit.rstrip("s") if unit not in ("%",) else unit
        q[u] = num.replace(",", ".")
    return q


def _sim(a: str, b: str) -> float:
    sa, sb = _stems(a), _stems(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _removal_free(text: str) -> str:
    return _REMOVE.sub(" ", _fold(text))


# ---------------------------------------------------------------------------
# comparison

def _diff(base: str, new: str) -> Dict[str, Any]:
    fa, fb = _formats(base), _formats(new)
    qa, qb = _quantities(base), _quantities(new)
    d: Dict[str, Any] = {}
    if fa != fb and fa and fb:          # a clause that names no format/unit does not change one
        d["attributes"] = {"from": sorted(fa - fb) or sorted(fa), "to": sorted(fb - fa) or sorted(fb)}
    changed_q = {u: {"from": qa[u], "to": qb[u]} for u in qa.keys() & qb.keys() if qa[u] != qb[u]}
    if changed_q:
        d["quantities"] = changed_q
    new_q = {u: qb[u] for u in qb.keys() - qa.keys()}
    if new_q:
        d["new_quantities"] = new_q
    return d


def _classify(new_clauses: List[str], base_clauses: List[str], replace: bool) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    matched_base: set = set()
    for i, c in enumerate(new_clauses):
        removal = bool(_REMOVE.search(_fold(c)))
        probe = _removal_free(c) if removal else c
        scored = sorted(((_sim(probe, b), j) for j, b in enumerate(base_clauses)), reverse=True)
        best, j = scored[0] if scored else (0.0, -1)
        second = scored[1][0] if len(scored) > 1 else 0.0
        fmt_hit = [k for k, b in enumerate(base_clauses) if _formats(c) & _formats(b)]
        row: Dict[str, Any] = {"n": i + 1, "clause": c, "score": round(best, 2)}
        vague = _VAGUE.search(_fold(c))
        if removal:
            if best >= 0.25:
                row.update(status="removed", match=j)
                matched_base.add(j)
            else:
                row.update(status="ambiguous", reason="removal", match=None)
        elif best >= 0.5:
            d = _diff(base_clauses[j], c)
            extra = _stems(probe) - _stems(base_clauses[j])
            if not (d.get("attributes") or d.get("quantities")) and extra and best < 0.8:
                # the request names something the agreed clause does not: sharing most words
                # with it is not enough to call it "already included" (no change order)
                row.update(status="ambiguous", reason="partial", match=j, extra_words=sorted(extra))
            else:
                row.update(status="changed" if d.get("attributes") or d.get("quantities") else "included", match=j, diff=d)
            matched_base.add(j)
        elif best >= 0.34:                  # at least a third of the words in common
            d = _diff(base_clauses[j], c)
            if second >= 0.34 and best - second < 0.08:
                row.update(status="ambiguous", reason="two", match=j, other=scored[1][1])
            elif d.get("attributes") or d.get("quantities"):
                row.update(status="changed", match=j, diff=d)
                matched_base.add(j)
            else:
                row.update(status="ambiguous", reason="partial", match=j)
        else:
            row.update(status="added", match=None)
        if vague and row["status"] in ("added", "changed", "included"):
            row["status_before_vague"] = row["status"]
            row.update(status="ambiguous", reason="vague", vague_word=vague.group(1))
        rows.append(row)
    if replace:
        for j, b in enumerate(base_clauses):
            if j not in matched_base and not any(r.get("match") == j for r in rows):
                rows.append({"n": len(rows) + 1, "clause": b, "status": "removed", "match": j,
                             "reason": "not_restated", "score": 0.0})
    return rows


# ---------------------------------------------------------------------------
# effects through the decision map

def _map(owner: str, graph: str) -> Optional[Dict[str, Any]]:
    from src import derivation_graph as dg
    try:
        return dg.map_show(owner, graph)
    except Exception:  # noqa: BLE001 - an unknown graph is "no grounding", not an error
        return None


def _items_named(text_words: set, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    hits = []
    for it in items:
        title = it.get("title") or ""
        tw = set(_words(title)) | _formats(title)
        tw = {_UNIT_WORDS.get(w, w) for w in tw}
        if tw and tw <= text_words:
            hits.append(it)
    return hits


def _clause_words(text: str) -> set:
    return {_UNIT_WORDS.get(w, w) for w in _words(text)} | _formats(text)


def _effect(owner: str, graph: str, gmap: Dict[str, Any], row: Dict[str, Any], base_clause: str,
            effort: Mapping[str, Any]) -> Dict[str, Any]:
    from src import derivation_graph as dg
    attrs = (row.get("diff") or {}).get("attributes") or {}
    if row["status"] == "changed" and attrs:
        words = set(attrs.get("from") or []) | set(attrs.get("to") or [])     # only what changed
    elif row["status"] == "removed":
        words = _clause_words(base_clause or row["clause"])
    else:
        words = _clause_words(row["clause"])
    items = _items_named(words, gmap.get("items") or [])
    if not items:
        return {"class": "unknown", "items": [], "recalculate": [], "deliverables": []}
    recalc: List[str] = []
    deliverables: List[str] = []
    via: List[List[str]] = []
    for it in items:
        try:
            res = dg.affected_by(owner, graph, it["id"])
        except Exception:  # noqa: BLE001
            continue
        for n in res.get("recalculate") or []:
            if n not in recalc:
                recalc.append(n)
        for d in res.get("deliverables") or []:
            if d["title"] not in deliverables:
                deliverables.append(d["title"])
        via += [a["via"] for a in res.get("affected") or []][:6]
    total = max(1, len(gmap.get("nodes") or []))
    if not recalc and not deliverables:
        cls = "none"
    elif len(recalc) <= 1:
        cls = "local"
    elif len(recalc) >= 4 or len(recalc) / total >= 0.6:
        cls = "pipeline"
    else:
        cls = "partial"
    eff: Dict[str, Any] = {"class": cls, "items": [i["id"] for i in items], "recalculate": recalc,
                           "deliverables": deliverables, "via": via}
    lo = hi = 0.0
    missing = []
    for n in recalc:
        r = effort.get(n) if isinstance(effort, Mapping) else None
        if isinstance(r, (list, tuple)) and len(r) == 2:
            lo += float(r[0]); hi += float(r[1])
        else:
            missing.append(n)
    if recalc and len(missing) < len(recalc):
        eff["effort_hours"] = [round(lo, 2), round(hi, 2)]
    if missing:
        eff["no_effort_for"] = missing
    return eff


def _options(row: Dict[str, Any], base: List[str], lang: str) -> Tuple[List[str], Optional[str]]:
    t = _TEXT[lang]
    st = row["status"]
    ref = f"A{row['match'] + 1}: «{base[row['match']]}»" if row.get("match") is not None and row["match"] >= 0 else ""
    eff = row.get("effect") or {}
    nodes = ", ".join(eff.get("recalculate") or eff.get("deliverables") or []) or "—"
    rng = ""
    if eff.get("effort_hours"):
        lo, hi = eff["effort_hours"]
        rng = (t["rng_partial"].format(lo=lo, hi=hi, missing=", ".join(eff["no_effort_for"]))
               if eff.get("no_effort_for") else t["rng"].format(lo=lo, hi=hi))
    question = None
    if st == "included":
        return [t["included"].format(ref=ref)], None
    if st == "ambiguous":
        reason = row.get("reason")
        if reason == "vague":
            question = t["q_vague"].format(word=row.get("vague_word"), clause=row["clause"])
        elif reason == "two":
            question = t["q_two"].format(clause=row["clause"], a=base[row["match"]], b=base[row["other"]])
        elif reason == "removal":
            question = t["q_remove"].format(clause=row["clause"])
        else:
            question = t["q_partial"].format(clause=row["clause"], a=base[row["match"]])
        return [], question
    if st == "removed":
        return [t["remove"].format(nodes=nodes) if eff.get("recalculate") else t["remove_plain"]], None
    cls = eff.get("class", "unknown")
    opts: List[str] = []
    if cls in ("local", "none"):
        opts += [t["absorb"].format(nodes=nodes), t["small_co"].format(rng=rng)]
    elif cls in ("partial", "pipeline"):
        opts += [t["co"].format(rng=rng, n=len(eff.get("recalculate") or []), nodes=nodes), t["phase2"]]
    else:
        opts += [t["unknown_effect"], t["phase2"]]
    if st == "changed" and ref:
        opts.append(t["keep"].format(ref=ref))
    return opts, None


def _owner_dir(owner: str) -> Path:
    from src.constants import DATA_DIR
    return Path(DATA_DIR) / "scope_comparisons" / (_SAFE.sub("_", str(owner or "_"))[:80] or "_")


def compare(owner: str, args: Mapping[str, Any]) -> Dict[str, Any]:
    agreement = str(args.get("agreement") or "").strip()
    new_request = str(args.get("new_request") or "").strip()
    initial = str(args.get("initial") or "").strip()
    if not agreement or not new_request:
        raise ScopeError("scope.bad_args", "agreement and new_request are required")
    lang = str(args.get("lang") or "es").lower()
    lang = lang if lang in _TEXT else "es"
    replace = str(args.get("mode") or "delta").lower() == "replace"
    base = clauses(agreement)
    new = clauses(new_request)
    if not base or not new:
        raise ScopeError("scope.bad_args", "no clause found in the agreement or the new request")
    rows = _classify(new, base, replace)
    graph = str(args.get("graph") or "").strip()
    gmap = _map(owner, graph) if graph else None
    effort = args.get("effort_hours") if isinstance(args.get("effort_hours"), Mapping) else {}
    questions: List[str] = []
    for r in rows:
        if r["status"] in ("changed", "added", "removed"):
            if gmap is not None:
                r["effect"] = _effect(owner, graph, gmap, r, base[r["match"]] if r.get("match") is not None else "", effort)
            else:
                r["effect"] = {"class": "unknown", "reason": "no decision map given"}
        r["options"], q = _options(r, base, lang)
        if q:
            r["question"] = q
            questions.append(q)
        if r.get("match") is not None and r["match"] >= 0:
            r["agreed"] = base[r["match"]]
            r["agreed_ref"] = f"A{r['match'] + 1}"
    negotiated = []
    if initial:
        for r in _classify(base, clauses(initial), True):
            if r["status"] != "included":
                negotiated.append({"clause": r["clause"], "status": r["status"], **({"diff": r["diff"]} if r.get("diff") else {})})
    counts = {s: sum(1 for r in rows if r["status"] == s) for s in STATUSES}
    cid = time.strftime("%Y%m%d-%H%M%S") + "-" + hashlib.sha1((agreement + new_request).encode()).hexdigest()[:6]
    result = {"schema": "scope-comparison/1", "id": cid, "title": str(args.get("title") or "").strip()[:120],
              "created": time.strftime("%Y-%m-%dT%H:%M:%S"), "lang": lang, "mode": "replace" if replace else "delta",
              "graph": graph or None, "graph_found": gmap is not None if graph else None,
              "agreement_clauses": base, "rows": rows, "counts": counts, "questions": questions,
              "negotiated": negotiated,
              "limits": ("Clauses are matched by shared words, formats and quantities, not by meaning; effects come only "
                         "from explicit decision-map links; effort only from the ranges given.")}
    d = _owner_dir(owner)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{cid}.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return result


def list_comparisons(owner: str) -> List[Dict[str, Any]]:
    d = _owner_dir(owner)
    rows = []
    if d.is_dir():
        for p in sorted(d.glob("*.json"), reverse=True):
            try:
                m = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            rows.append({"id": m["id"], "title": m.get("title") or "", "created": m["created"], "counts": m["counts"],
                         "questions": len(m.get("questions") or []), "graph": m.get("graph")})
    return rows


def show(owner: str, cid: str) -> Dict[str, Any]:
    if not re.fullmatch(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{6}", cid or ""):
        raise ScopeError("scope.not_found", f"unknown comparison {cid!r}")
    p = _owner_dir(owner) / f"{cid}.json"
    if not p.is_file():
        raise ScopeError("scope.not_found", f"unknown comparison {cid!r}")
    return json.loads(p.read_text(encoding="utf-8"))


def render(m: Dict[str, Any]) -> str:
    if "rows" not in m:
        return json.dumps(m, ensure_ascii=False)
    c = m["counts"]
    lines = [f"Scope comparison {m['id']}: " + ", ".join(f"{c[s]} {s}" for s in STATUSES)]
    for r in m["rows"]:
        eff = r.get("effect") or {}
        extra = f" [{eff.get('class')}" + (f", {eff['effort_hours'][0]}-{eff['effort_hours'][1]} h" if eff.get("effort_hours") else "") + "]" if eff else ""
        lines.append(f"- {r['status']}: {r['clause']}" + (f" (vs {r['agreed_ref']})" if r.get("agreed_ref") else "") + extra)
    if m["questions"]:
        lines.append("Questions: " + " | ".join(m["questions"]))
    return "\n".join(lines)


def run(owner: str, args: Mapping[str, Any]) -> Dict[str, Any]:
    action = str(args.get("action") or "compare").strip().lower()
    try:
        if action not in ACTIONS:
            raise ScopeError("scope.bad_args", f"action must be one of {', '.join(ACTIONS)}")
        if action == "list":
            return {"comparisons": list_comparisons(owner)}
        if action == "show":
            return show(owner, str(args.get("id") or ""))
        return compare(owner, args)
    except ScopeError as exc:
        return _err(exc.error_class, exc.detail)
    except (ValueError, OSError) as exc:
        return _err("scope.refused", str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.warning("scope comparison failed", exc_info=True)
        return _err("scope.error", f"{type(exc).__name__}: {exc}")
