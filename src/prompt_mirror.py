"""src/prompt_mirror.py -- a scorecard of how the owner directs Faustus, from their own chats.

Reads the owner's chat history (sessions + chat_messages) for a window (30 days by
default), separates what a person typed from machine traffic (scheduled task prompts,
group-chat relays, delegated workers, dispatched jobs, JSON argument blobs, app-written
briefs, prompts repeated verbatim across many chats), and measures:

* typed messages, chats, median / p90 length, machine messages per typed message;
* status asks ("¿cómo va?", "status?", "ETA") -- each is a poll done by hand;
* repeated corrections by class (premature stop, asking instead of doing, invented
  gates, stale claims, shallow fixes, shrinking the ask, unrequested side quests,
  "still broken", "I already told you"), and the phrases that recur in 3+ chats;
* kickoffs (the first message of a chat) that state when it is done and when to stop;
* loop smells: chats where "still broken" came up twice or numbers were pasted by hand
  twice with no measurement asked for;
* the same prompt typed in 3+ chats (a scheduled task candidate);
* assistant concessions ("Tienes razón", "You're right") per 100 replies -- a
  concession without a durable fix predicts a repeat.

It then picks three suggestions ranked by frequency, each with the number, one short
redacted message of the owner's own as the *before*, an *after*, and the Faustus piece
that removes the need (instincts / project rules for a repeated correction, a Loop Card
for a stuck loop, a scheduled task for a repeated prompt, objectives with done-when and
stop conditions for kickoffs, progress pushed by the run for status asks). Every run
is saved under DATA_DIR/prompt_mirror/<owner>/<timestamp>/ (report.md, stats.json,
suggestions.json) and compared with the previous one; a change counts only when it is
larger than the week-to-week spread of the previous weeks.

Nothing leaves the machine and nothing is applied: it only reads and reports.
Classes and the scorecard follow github/lever's prompt-mirror and steer-once skills (MIT).
"""
from __future__ import annotations

import json
import os
import re
import statistics
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from src import constants

MACHINE_SESSION_RE = re.compile(r"^\s*(\[(task|grp|tarea)\]|swarm|pez\b|worker\b|subagent|hermes\s*[·-]|prueba desde spark)", re.I)
MACHINE_META_KEYS = ("source", "dispatch_id", "group_turn_id", "provenance_origin", "agent_id", "relay")
MACHINE_TEXT_RE = re.compile(r"^\s*(\{.*\}|\[[A-Z ]{6,}\]|.{0,40}'s hoard \(|user request verbatim|continue from where you left off)", re.S | re.I)

STATUS_RE = re.compile(r"(\bstatus\b|\beta\b|¿?\s*c[oó]mo (va|vas)|en qu[eé] (vas|est[aá]s)|ya (est[aá]|has terminado|acabaste)|"
                       r"\bwhere are we\b|\bare you done\b|is it (still )?running|qu[eé] tal va|progreso\?)", re.I)
CLASSES: Dict[str, re.Pattern] = {
    "premature_stop": re.compile(r"(por qu[eé] (te )?paras|por qu[eé] has parado|no pares|why did you stop|don'?t stop|keep going|"
                                 r"sigue hasta|no te detengas|no has terminado)", re.I),
    "over_ask": re.compile(r"(no (me )?preguntes|deja de preguntar|hazlo y ya|don'?t ask|just do it|no hace falta que (me )?preguntes)", re.I),
    "gate": re.compile(r"(no (necesitas|hace falta) (permiso|autorizaci[oó]n|aprobaci[oó]n)|you don'?t need (authori[sz]ation|permission|approval)|"
                       r"sin pedir permiso)", re.I),
    "stale_claim": re.compile(r"(eso no es (verdad|cierto)|no es cierto|that'?s not true|comprueba(lo)? (de verdad|en vivo)|check the live|"
                              r"est[aá]s mirando una versi[oó]n vieja)", re.I),
    "shallow_fix": re.compile(r"(arr[eé]glalo de verdad|actually fix|no lo (desactives|ocultes|silencies)|instead of disabling|"
                              r"parche|ap[aá]ño)", re.I),
    "letter_of_law": re.compile(r"(te ped[ií]|no es lo que (te )?ped[ií]|i asked for|that'?s not what i asked|lo que pedí era)", re.I),
    "scope_creep": re.compile(r"(no te he pedido|nadie te ha pedido|why did you (open|add|change)|no toques|i didn'?t ask)", re.I),
    "still_broken": re.compile(r"(sigue (sin funcionar|igual|roto|fallando|sin ir)|still (broken|slow|failing|the same)|same thing|"
                               r"no funciona (otra vez|todav[ií]a)|sigue sin)", re.I),
    "repeat": re.compile(r"(ya te (lo )?(dije|he dicho)|te lo he dicho|i (already )?told you|otra vez lo mismo|como te dije|again\?)", re.I),
}
DONE_WHEN_RE = re.compile(r"(cuando (est[eé]|termine|pase|funcione)|hasta que|done when|terminad[oa] cuando|criterio|que pasen? (los )?tests|"
                          r"tests? en verde|acceptance|debe (poder|quedar)|el resultado (debe|tiene)|deja(lo)? (funcionando|listo))", re.I)
STOP_RE = re.compile(r"(para si|stop if|si no (puedes|funciona|sale)|como mucho|m[aá]ximo|no m[aá]s de|at most|give up|"
                     r"pregunta(me)? (si|solo si)|escala)", re.I)
MEASURE_RE = re.compile(r"(mide|medir|benchmark|loop card|tarjeta de bucle|test de rendimiento|perf test|baseline|l[ií]nea base|cron[oó]metro)", re.I)
NUMBERS_RE = re.compile(r"\d+(?:[.,]\d+)?\s*(ms|s\b|seg|fps|%|mb|gb|min)", re.I)
CONCESSION_RE = re.compile(r"^\s*(tienes (toda la )?raz[oó]n|you'?re (absolutely )?right|good catch|buena observaci[oó]n|"
                           r"correcto,|cierto,|perd[oó]n|lo siento|i apologi[sz]e)", re.I)

REDACT = [
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "<email>"),
    (re.compile(r"https?://\S+"), "<url>"),
    (re.compile(r"[A-Za-z]:\\[^\s'\"]+|/(?:home|Users|mnt|tmp)/[^\s'\"]+"), "<path>"),
    (re.compile(r"\b[0-9a-f]{16,}\b", re.I), "<hex>"),
    (re.compile(r"\b(sk|ghp|gho|xox[bp]|AKIA)[-_A-Za-z0-9]{10,}"), "<token>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<uuid>"),
]

FIXES = {
    "status_ask": ("Progress you never have to ask for",
                   "Give long work a report contract: milestones with ETA and the evidence that proves each, pushed when "
                   "something changes (night shift morning report, dispatch with verify, the chat's own progress).",
                   "Next time you start a long task, end it with: 'avísame solo al terminar cada hito, con la prueba'."),
    "premature_stop": ("Stop the agent from stopping early", "Write the rule once as an instinct or a project rule "
                       "(instinct tools): 'when an item lands, take the next ready one; stop only at the done-when'.",
                       "Turn the correction into a rule instead of repeating it."),
    "over_ask": ("Fewer questions, more doing", "Record the posture as an instinct: 'decide reversible details yourself; ask "
                 "only about scope, money or irreversible actions'.", "Say it once as a rule."),
    "gate": ("No invented gates", "An instinct or project rule: 'no approval is needed for X in this project'.", "Say it once as a rule."),
    "stale_claim": ("Check the live source", "A project rule: 'verify against the running instance, not a cached copy, before "
                    "claiming a state'.", "Say it once as a rule."),
    "shallow_fix": ("Fix causes, not symptoms", "A project rule plus a test that fails today (bug hunt can write it).",
                    "Ask for the failing test first."),
    "letter_of_law": ("Keep the whole ask", "Put the ask verbatim in the objective (criteria check) so the done-audit reads it.",
                      "Paste the original ask as the objective."),
    "scope_creep": ("No side quests", "A project rule with the boundary, or the touched-files list in the task.",
                    "Name the files or areas it may touch."),
    "still_broken": ("Build the feedback loop", "Create a Loop Card (loop_card_create) with a command the agent runs itself; "
                     "then compare changes with loop_arms or loop_improve instead of reporting by hand.",
                     "'Antes de tocar nada, crea una Loop Card que mida esto y enséñame la línea base.'"),
    "repeat": ("Say it once", "Turn the repeated instruction into an instinct or project rule; re-check in a week whether "
               "it still recurs.", "Write it as a rule."),
    "no_done_when": ("Say when it is done", "Kickoffs with an observable done-when (a test passes, a page shows X) and a stop "
                     "condition; criteria_check can audit it at the end.", "Add: 'terminado cuando … ; para si …'."),
    "repeated_prompt": ("Make it a scheduled task", "The same prompt typed in several chats: a scheduled task (or a recipe) does "
                        "it without you.", "Schedule it once."),
}


def redact(text: str, terms: Iterable[str] = ()) -> str:
    out = text or ""
    for rx, repl in REDACT:
        out = rx.sub(repl, out)
    for t in terms:
        if t and len(t) > 2:
            out = re.sub(re.escape(t), "<redacted>", out, flags=re.I)
    return out


def _short(text: str, words: int = 25) -> str:
    w = (text or "").split()
    return " ".join(w[:words]) + (" …" if len(w) > words else "")


def is_machine(session_name: str, meta: Mapping[str, Any], content: str) -> bool:
    if MACHINE_SESSION_RE.search(session_name or ""):
        return True
    if any(k in (meta or {}) for k in MACHINE_META_KEYS):
        return True
    return bool(MACHINE_TEXT_RE.match(content or ""))


def _load(owner: str, since: datetime, until: datetime) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """(user rows, assistant rows) with session id/name and parsed metadata."""
    from core.database import ChatMessage, Session, get_db_session
    users, assistants = [], []
    with get_db_session() as db:
        q = (db.query(ChatMessage.session_id, ChatMessage.role, ChatMessage.content, ChatMessage.meta_data,
                      ChatMessage.timestamp, Session.name, Session.owner)
             .join(Session, Session.id == ChatMessage.session_id)
             .filter(ChatMessage.timestamp >= since, ChatMessage.timestamp <= until)
             .filter(ChatMessage.role.in_(("user", "assistant"))))
        for sid, role, content, meta, ts, name, sowner in q.all():
            if owner and sowner and str(sowner) != str(owner):
                continue
            try:
                m = json.loads(meta) if meta else {}
            except ValueError:
                m = {}
            row = {"session": sid, "name": name or "", "content": content or "", "meta": m if isinstance(m, dict) else {}, "ts": ts}
            (users if role == "user" else assistants).append(row)
    users.sort(key=lambda r: r["ts"] or datetime.min)
    return users, assistants


def analyse(users: List[Dict[str, Any]], assistants: List[Dict[str, Any]], *, terms: Iterable[str] = (),
            since: Optional[datetime] = None) -> Dict[str, Any]:
    typed, machine = [], 0
    for r in users:
        if is_machine(r["name"], r["meta"], r["content"]):
            machine += 1
        else:
            typed.append(r)
    # A long prompt whose opening recurs in 4+ chats is pasted or generated; drop it as machine traffic
    openings = defaultdict(set)
    for r in typed:
        openings[re.sub(r"\s+", " ", r["content"].strip().lower())[:60]].add(r["session"])
    typed2 = []
    repeated_prompts = Counter()
    for r in typed:
        key = re.sub(r"\s+", " ", r["content"].strip().lower())[:60]
        if len(r["content"]) >= 60 and len(openings[key]) >= 4:
            machine += 1
            repeated_prompts[key] += 1
            continue
        typed2.append(r)
        if len(openings[key]) >= 3:
            repeated_prompts[key] += 1
    typed = typed2
    words = [len(r["content"].split()) for r in typed]
    sessions = sorted({r["session"] for r in typed})
    by_session = defaultdict(list)
    for r in typed:
        by_session[r["session"]].append(r)
    flags: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in typed:
        text = r["content"]
        if STATUS_RE.search(text) and len(text) < 300:
            flags["status_ask"].append(r)
        for cls, rx in CLASSES.items():
            if rx.search(text):
                flags[cls].append(r)
    class_sessions = {cls: len({r["session"] for r in rows}) for cls, rows in flags.items()}
    kick = [rows[0] for rows in by_session.values() if rows]
    long_kick = [r for r in kick if len(r["content"].split()) >= 8]
    done_when = sum(1 for r in long_kick if DONE_WHEN_RE.search(r["content"]))
    stop = sum(1 for r in long_kick if STOP_RE.search(r["content"]))
    smells = []
    for sid, rows in by_session.items():
        broken = sum(1 for r in rows if CLASSES["still_broken"].search(r["content"]))
        pasted = sum(1 for r in rows if len(NUMBERS_RE.findall(r["content"])) >= 2)
        measured = any(MEASURE_RE.search(r["content"]) for r in rows)
        if (broken >= 2 or pasted >= 2) and not measured:
            smells.append({"session": sid, "name": rows[0]["name"][:80], "still_broken": broken, "pasted_numbers": pasted})
    phrase_sessions = defaultdict(set)
    for cls in CLASSES:
        for r in flags.get(cls, []):
            m = CLASSES[cls].search(r["content"])
            if m:
                phrase_sessions[(cls, m.group(0).lower())].add(r["session"])
    repeated_corrections = sorted(({"class": c, "phrase": p, "chats": len(s)} for (c, p), s in phrase_sessions.items() if len(s) >= 3),
                                  key=lambda x: -x["chats"])
    concessions = sum(1 for a in assistants if CONCESSION_RE.search(a["content"] or ""))
    n = max(1, len(typed))
    weekly = defaultdict(lambda: Counter())
    for r in typed:
        if r["ts"]:
            wk = r["ts"].strftime("%G-W%V")
            weekly[wk]["typed"] += 1
            if r in flags["status_ask"]:
                weekly[wk]["status_ask"] += 1
            for cls in CLASSES:
                if r in flags.get(cls, []):
                    weekly[wk]["corrections"] += 1
    stats = {
        "typed": len(typed), "machine": machine, "machine_per_typed": round(machine / n, 2), "chats": len(sessions),
        "words": {"median": statistics.median(words) if words else 0,
                  "p90": sorted(words)[int(0.9 * (len(words) - 1))] if words else 0},
        "rates_pct_of_typed": {"status_ask": round(100 * len(flags["status_ask"]) / n, 1),
                               **{c: round(100 * len(flags.get(c, [])) / n, 1) for c in CLASSES}},
        "counts": {"status_ask": len(flags["status_ask"]), **{c: len(flags.get(c, [])) for c in CLASSES}},
        "class_chats": class_sessions,
        "kickoffs": {"count": len(long_kick), "has_done_when_pct": round(100 * done_when / max(1, len(long_kick)), 1),
                     "has_stop_condition_pct": round(100 * stop / max(1, len(long_kick)), 1)},
        "loop_smells": smells[:20],
        "repeated_corrections": repeated_corrections[:15],
        "automation_candidates": [{"prompt": redact(k, terms), "times": v} for k, v in repeated_prompts.most_common(8)],
        "assistant": {"replies": len(assistants), "concessions": concessions,
                      "concessions_per_100": round(100 * concessions / max(1, len(assistants)), 1)},
        "weekly": {k: dict(v) for k, v in sorted(weekly.items())},
    }
    stats["suggestions"] = suggest(stats, flags, long_kick, terms)
    return stats


def suggest(stats: Mapping[str, Any], flags: Mapping[str, List[Dict[str, Any]]], kick: List[Dict[str, Any]],
            terms: Iterable[str]) -> List[Dict[str, Any]]:
    cands: List[Tuple[float, str, int, List[Dict[str, Any]], str]] = []
    for key in ["status_ask", *CLASSES.keys()]:
        rows = flags.get(key, [])
        chats = len({r["session"] for r in rows})
        if rows and (key == "status_ask" or chats >= 2):
            weight = 1.5 if key in ("still_broken", "premature_stop", "repeat") else 1.0
            cands.append((len(rows) * weight + chats, key, len(rows), rows,
                          f"{len(rows)} messages ({stats['rates_pct_of_typed'][key]} % of typed) in {chats} chats"))
    if stats["kickoffs"]["count"] >= 3 and stats["kickoffs"]["has_done_when_pct"] < 50:
        missing = [r for r in kick if not DONE_WHEN_RE.search(r["content"])]
        cands.append((len(missing), "no_done_when", len(missing), missing,
                      f"{100 - stats['kickoffs']['has_done_when_pct']:.0f} % of {stats['kickoffs']['count']} kickoffs say no done-when"))
    if stats["automation_candidates"]:
        top = stats["automation_candidates"][0]
        cands.append((top["times"] * 1.2, "repeated_prompt", top["times"], [],
                      f"the same prompt typed {top['times']} times: «{top['prompt']}»"))
    if stats["loop_smells"]:
        cands.append((len(stats["loop_smells"]) * 5, "still_broken", len(stats["loop_smells"]), flags.get("still_broken", []),
                      f"{len(stats['loop_smells'])} chats stuck without a measurement"))
    out, seen = [], set()
    for score, key, count, rows, signal in sorted(cands, key=lambda c: -c[0]):
        if key in seen:
            continue
        seen.add(key)
        title, fix, after = FIXES[key]
        before = ""
        for r in rows:
            if len(r["content"]) < 600:
                before = _short(redact(r["content"], terms))
                break
        out.append({"key": key, "title": title, "signal": signal, "count": count, "before": before, "after": after,
                    "fix": fix, "metric": f"rates_pct_of_typed.{key}" if key in stats["rates_pct_of_typed"] else key})
        if len(out) == 3:
            break
    return out


def _dir(owner: str) -> Path:
    return Path(constants.DATA_DIR) / "prompt_mirror" / (re.sub(r"[^\w.-]", "_", owner or "_")[:80] or "_")


def _previous(owner: str) -> Optional[Dict[str, Any]]:
    d = _dir(owner)
    if not d.is_dir():
        return None
    runs = sorted(p for p in d.iterdir() if (p / "stats.json").is_file())
    if not runs:
        return None
    try:
        return json.loads((runs[-1] / "stats.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _trend(prev: Optional[Mapping[str, Any]], cur: Mapping[str, Any]) -> List[Dict[str, Any]]:
    if not prev:
        return []
    out = []
    weeks = list((cur.get("weekly") or {}).values())[-5:-1]
    spread = {}
    for key in ("status_ask", "corrections"):
        rates = [100 * w.get(key, 0) / max(1, w.get("typed", 0)) for w in weeks if w.get("typed")]
        spread[key] = (max(rates) - min(rates)) if len(rates) >= 2 else None
    for s in prev.get("suggestions") or []:
        metric = s.get("metric", "")
        if metric.startswith("rates_pct_of_typed."):
            key = metric.split(".", 1)[1]
            before = (prev.get("rates_pct_of_typed") or {}).get(key)
            after = (cur.get("rates_pct_of_typed") or {}).get(key)
            noise = spread.get("status_ask" if key == "status_ask" else "corrections")
            moved = None if before is None or after is None else after - before
            verdict = "no data" if moved is None else ("within noise" if noise is None or abs(moved) <= noise else
                                                      ("better" if moved < 0 else "worse"))
            out.append({"suggestion": s.get("title"), "metric": key, "before": before, "after": after, "verdict": verdict})
    return out


def run(owner: str, args: Mapping[str, Any]) -> Dict[str, Any]:
    days = max(1, min(365, int(args.get("days") or 30)))
    until = datetime.now(timezone.utc).replace(tzinfo=None)
    since = until - timedelta(days=days)
    terms = [t.strip() for t in str(args.get("redact_terms") or "").split(",") if t.strip()]
    try:
        users, assistants = _load(owner, since, until)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"could not read the chat history: {type(exc).__name__}: {exc}", "error_class": "mirror.no_history"}
    stats = analyse(users, assistants, terms=terms, since=since)
    stats["window"] = {"days": days, "since": since.isoformat(timespec="seconds"), "until": until.isoformat(timespec="seconds")}
    prev = _previous(owner)
    stats["trend"] = _trend(prev, stats)
    if args.get("save", True):
        d = _dir(owner) / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        d.mkdir(parents=True, exist_ok=True)
        (d / "stats.json").write_text(json.dumps(stats, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        (d / "suggestions.json").write_text(json.dumps(stats["suggestions"], ensure_ascii=False, indent=1), encoding="utf-8")
        (d / "report.md").write_text(render(stats), encoding="utf-8")
        stats["saved"] = str(d)
    return stats


def history(owner: str) -> Dict[str, Any]:
    d = _dir(owner)
    rows = []
    if d.is_dir():
        for p in sorted(d.iterdir(), reverse=True):
            try:
                s = json.loads((p / "stats.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            rows.append({"run": p.name, "typed": s.get("typed"), "status_ask": s["rates_pct_of_typed"].get("status_ask"),
                         "suggestions": [x["title"] for x in s.get("suggestions") or []]})
    return {"runs": rows}


def render(stats: Mapping[str, Any]) -> str:
    if stats.get("error"):
        return f"error: {stats['error']}"
    if "runs" in stats:
        return "\n".join(f"{r['run']}: {r['typed']} typed, status asks {r['status_ask']} %, suggested: {', '.join(r['suggestions'])}"
                         for r in stats["runs"]) or "no prompt mirror runs yet"
    r = stats["rates_pct_of_typed"]
    lines = [f"# How you direct Faustus ({stats.get('window', {}).get('days', '?')} days)", "",
             f"- Typed messages: {stats['typed']} in {stats['chats']} chats; machine messages per typed: {stats['machine_per_typed']}",
             f"- Length: median {stats['words']['median']} words, p90 {stats['words']['p90']}",
             f"- Status asks: {r['status_ask']} % of typed",
             f"- Corrections: " + ", ".join(f"{c} {r[c]} %" for c in CLASSES if r.get(c)),
             f"- Kickoffs ({stats['kickoffs']['count']}): done-when {stats['kickoffs']['has_done_when_pct']} %, stop condition "
             f"{stats['kickoffs']['has_stop_condition_pct']} %",
             f"- Chats stuck without measurement: {len(stats['loop_smells'])}",
             f"- Assistant concessions: {stats['assistant']['concessions_per_100']} per 100 replies"]
    if stats.get("repeated_corrections"):
        lines.append("- Repeated in 3+ chats: " + "; ".join(f"«{x['phrase']}» ({x['class']}, {x['chats']} chats)"
                                                             for x in stats["repeated_corrections"][:5]))
    lines += ["", "## Three suggestions", ""]
    for i, s in enumerate(stats.get("suggestions") or [], start=1):
        lines += [f"{i}. **{s['title']}** — {s['signal']}"]
        if s.get("before"):
            lines.append(f"   - before: «{s['before']}»")
        lines += [f"   - after: {s['after']}", f"   - fix: {s['fix']}", f"   - watch: {s['metric']}"]
    if not stats.get("suggestions"):
        lines.append("Nothing recurs often enough to suggest a change.")
    if stats.get("trend"):
        lines += ["", "## Since the last run", ""]
        lines += [f"- {t['suggestion']}: {t['metric']} {t['before']} % → {t['after']} % ({t['verdict']})" for t in stats["trend"]]
    return "\n".join(lines) + "\n"
