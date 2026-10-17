"""Owner/project-scoped dictation vocabulary and recoverable text history.

Native SQLite storage; no audio, model, clipboard or third-party runtime here.
"""
from contextlib import contextmanager
from difflib import SequenceMatcher
from pathlib import Path
import re
import sqlite3
import time
import uuid


@contextmanager
def connect():
    from src.constants import DATA_DIR
    path = Path(DATA_DIR) / "dictation.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=15)
    db.row_factory = sqlite3.Row
    try:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS vocabulary (
          id TEXT PRIMARY KEY, owner TEXT NOT NULL, project TEXT NOT NULL,
          heard TEXT NOT NULL, replacement TEXT NOT NULL, key TEXT NOT NULL,
          UNIQUE(owner, project, key));
        CREATE TABLE IF NOT EXISTS dictations (
          id TEXT PRIMARY KEY, owner TEXT NOT NULL, project TEXT NOT NULL,
          created REAL NOT NULL, raw_text TEXT NOT NULL, text TEXT NOT NULL,
          status TEXT NOT NULL, target TEXT NOT NULL, error TEXT NOT NULL);
        """)
        yield db
        db.commit()
    finally:
        db.close()


def vocabulary(owner=None, project=""):
    with connect() as db:
        return [dict(r) for r in db.execute(
            "SELECT id,project,heard,replacement FROM vocabulary WHERE owner=? AND project IN ('',?) ORDER BY project,heard",
            (owner or "", project))]


def put_vocabulary(heard, replacement, *, owner=None, project=""):
    heard, replacement = heard.strip(), replacement.strip()
    if not heard or not replacement or len(heard) > 120 or len(replacement) > 120:
        raise ValueError("Use a non-empty phrase of at most 120 characters.")
    # Vocabulary is spelling, not changing quantities or executable paths.
    if any(re.search(r"[\d/\\:=+%−]", s) for s in (heard, replacement)):
        raise ValueError("Vocabulary entries cannot change numbers, paths or operators.")
    if set((heard + " " + replacement).casefold().split()) & {"no", "not", "nunca", "jamás", "sin", "never", "nothing", "nada"}:
        raise ValueError("Vocabulary entries cannot replace negations.")
    key = heard.casefold()
    with connect() as db:
        db.execute("""INSERT INTO vocabulary VALUES(?,?,?,?,?,?)
            ON CONFLICT(owner,project,key) DO UPDATE SET heard=excluded.heard,replacement=excluded.replacement""",
            (uuid.uuid4().hex, owner or "", project, heard, replacement, key))
        return dict(db.execute("SELECT id,project,heard,replacement FROM vocabulary WHERE owner=? AND project=? AND key=?",
                               (owner or "", project, key)).fetchone())


def delete_vocabulary(entry_id, owner=None):
    with connect() as db:
        return bool(db.execute("DELETE FROM vocabulary WHERE id=? AND owner=?", (entry_id, owner or "")).rowcount)


def apply_vocabulary(text, owner=None, project=""):
    # Project spellings override global spellings. One substitution pass prevents cascades.
    entries = {e["heard"].casefold(): e for e in vocabulary(owner, project)}
    if not entries:
        return text
    pattern = re.compile(r"(?<![\w/\\])(?:" + "|".join(re.escape(k) for k in sorted(entries, key=len, reverse=True)) + r")(?![\w/\\])", re.I)
    # Quoted content and URLs/paths/code are literal dictation, not vocabulary targets.
    parts = re.split(r'(`[^`]*`|"[^"]*"|“[^”]*”|«[^»]*»|https?://\S+|\S*[\\/]\S+|\b[\w.-]+\.(?:py|js|ts|tsx|json|md|txt|csv|exe)\b)', text)
    return "".join(p if i % 2 else pattern.sub(lambda m: entries[m.group().casefold()]["replacement"], p)
                   for i, p in enumerate(parts))


def remember(raw_text, text, *, owner=None, project="", target="", status="ready", error=""):
    entry_id = uuid.uuid4().hex
    with connect() as db:
        db.execute("INSERT INTO dictations VALUES(?,?,?,?,?,?,?,?,?)",
                   (entry_id, owner or "", project, time.time(), raw_text, text, status, target, error))
    return entry_id


def delivery(entry_id, status, error="", owner=None):
    with connect() as db:
        db.execute("UPDATE dictations SET status=?,error=? WHERE id=? AND owner=?",
                   (status, error, entry_id, owner or ""))


def history(owner=None, project="", before=None, limit=30):
    with connect() as db:
        return [dict(r) for r in db.execute("""SELECT id,project,created,raw_text,text,status,target,error
            FROM dictations WHERE owner=? AND project=? AND created<? ORDER BY created DESC,id DESC LIMIT ?""",
            (owner or "", project, before or float("inf"), max(1, min(limit, 100))))]


_COMMON = set("no sí si not yes the and pero sino nada nunca jamás none nothing all todos todas el la los las un una de del por para en con sin es son era a an to for is are was vale gracias hola adiós okay ok thanks hello".split())


def learn_corrections(original, edited):
    before, after = original.split(), edited.split()
    changes = []
    for tag, a, b, c, d in SequenceMatcher(a=before, b=after, autojunk=False).get_opcodes():
        if tag != "replace" or b - a > 3 or d - c > 3:
            continue
        heard = " ".join(before[a:b]).strip(".,;!?¡¿")
        replacement = " ".join(after[c:d]).strip(".,;!?¡¿")
        words = (heard + " " + replacement).casefold().split()
        if any(w in _COMMON for w in words) or any(not re.fullmatch(r"[^\W\d_]+(?:[-'][^\W\d_]+)*", w) for w in words):
            continue
        if len(replacement) < 3 or SequenceMatcher(a=heard.casefold(), b=replacement.casefold()).ratio() < .5:
            continue
        changes.append((heard, replacement))
    # A rewritten sentence is not a vocabulary correction.
    return changes if len(changes) <= max(1, len(before) // 4) else []


def correct(entry_id, text, *, owner=None, learn=True):
    with connect() as db:
        row = db.execute("SELECT * FROM dictations WHERE id=? AND owner=?", (entry_id, owner or "")).fetchone()
        if row is None:
            raise KeyError(entry_id)
        changes = learn_corrections(row["text"], text) if learn else []
        db.execute("UPDATE dictations SET text=? WHERE id=?", (text, entry_id))
    learned = [put_vocabulary(a, b, owner=owner, project=row["project"]) for a, b in changes]
    return {"id": entry_id, "text": text, "learned": learned}


def delete_history(entry_id, owner=None):
    with connect() as db:
        return bool(db.execute("DELETE FROM dictations WHERE id=? AND owner=?", (entry_id, owner or "")).rowcount)
