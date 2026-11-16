"""Fold a long text into five levels of detail, and read it back one level at a time.

A long assistant answer or tool output rarely needs to be read (or kept in a model's
context) whole. This module turns a text into an open, JSON-serialisable document
(`FORMAT`) of numbered paragraphs grouped in sections, and gives every paragraph up to
five views:

1. full     - the original text, never altered;
2. sentences - one to three literal sentences of each paragraph, chosen without a model;
3. key       - the idea and the reason, per paragraph, written by the model;
4. lines     - one line per section, written by the model;
5. topics    - a map of a few topic cards, written by the model.

Levels 1 and 2 are deterministic and always available. Levels 3-5 are generated on
demand with the global language model (the same utility endpoint the rest of Faustus
uses) and cached with the document, so asking for them again costs nothing. When the
model cannot be reached the call fails with `FoldUnavailable`; a deterministic
stand-in is never passed off as a generated level.

Every view names the paragraphs it came from, so a folded fragment can always be
opened back into its original paragraph (`read(..., paragraph="p3")`).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

FORMAT = "faustus.fold/1"
LEVEL_NAMES = {1: "full", 2: "sentences", 3: "key", 4: "lines", 5: "topics"}
GENERATED_LEVELS = (3, 4, 5)
MAX_CHARS = 2_000_000
#: Below this size a text is not worth folding; the UI hides the control.
MIN_FOLD_CHARS = 1200
_BATCH_CHARS = 6000
_RETENTION_SECONDS = 30 * 24 * 3600

LlmFn = Callable[[List[Dict[str, str]]], Awaitable[str]]


class FoldError(ValueError):
    """The request cannot be served (bad level, unknown section, empty text...)."""


class FoldNotFound(FoldError):
    """The document, section, paragraph or artifact does not exist for this owner."""


class FoldUnavailable(RuntimeError):
    """A generated level was asked for and the language model did not answer."""


# --------------------------------------------------------------------------- parsing

_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*\S)\s*$")
_FENCE = re.compile(r"^\s{0,3}(```|~~~)")
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_TABLE_ROW = re.compile(r"^\s*\|")
_SENTENCE_END = re.compile(r"(?<=[.!?…])[\"')\]]*\s+(?=[\"'(\[¿¡]*[A-ZÁÉÍÓÚÑÜ0-9#*`-])")
_WORD = re.compile(r"[^\W\d_]{3,}", re.UNICODE)
_STOP = frozenset("""
the and for are but not you all can had her was one our out has have that this with from they will
would there their what about which when your said each she how them these some into than then its
also been were more other only over such very just like make made may can could should
que los las del por con para una unos unas como pero sus les nos este esta esto estos estas
ese esa eso son fue ser han hay muy mas más sin sobre entre cuando donde también tambien porque
cada todo toda todos todas ante bajo desde hasta hacia segun según
""".split())


def _kind(lines: List[str]) -> str:
    first = lines[0]
    if _FENCE.match(first):
        return "code"
    if _HEADING.match(first):
        return "heading"
    if all(_TABLE_ROW.match(line) for line in lines):
        return "table"
    if all(_LIST_ITEM.match(line) or line.startswith((" ", "\t")) for line in lines):
        return "list"
    return "text"


def _split_blocks(text: str) -> List[Dict[str, Any]]:
    """Blank-line separated paragraphs with their character span. A fenced code block
    stays whole, and a heading is always a paragraph of its own."""
    blocks: List[Dict[str, Any]] = []
    cur: List[tuple] = []
    fence: Optional[str] = None

    def flush() -> None:
        nonlocal cur
        if cur:
            blocks.append({"start": cur[0][0], "end": cur[-1][0] + len(cur[-1][1]),
                           "lines": [line for _pos, line in cur]})
        cur = []

    pos = 0
    for raw in text.splitlines(keepends=True):
        line = raw.rstrip("\r\n")
        opening = _FENCE.match(line)
        if fence:
            cur.append((pos, line))
            if line.strip().startswith(fence):
                fence = None
                flush()
        elif opening:
            flush()
            fence = opening.group(1)
            cur.append((pos, line))
        elif not line.strip():
            flush()
        elif _HEADING.match(line):
            flush()
            cur.append((pos, line))
            flush()
        else:
            cur.append((pos, line))
        pos += len(raw)
    flush()
    return blocks


def _sentences(text: str) -> List[str]:
    flat = re.sub(r"\s*\n\s*", " ", text.strip())
    parts = [p.strip() for p in _SENTENCE_END.split(flat) if p.strip()]
    return parts or ([flat] if flat else [])


def _frequencies(text: str) -> Dict[str, float]:
    counts: Dict[str, int] = {}
    for word in _WORD.findall(text.lower()):
        if word not in _STOP:
            counts[word] = counts.get(word, 0) + 1
    top = max(counts.values(), default=1)
    return {word: n / top for word, n in counts.items()}


def _keep_count(n: int) -> int:
    return 1 if n <= 2 else 2 if n <= 5 else 3


def _extract(paragraph: Dict[str, Any], freq: Dict[str, float]) -> str:
    """Level 2 of one paragraph: literal sentences, in their original order."""
    text, kind = paragraph["text"], paragraph["kind"]
    if kind == "heading":
        return text
    if kind in ("code", "list", "table"):
        lines = text.splitlines()
        if len(lines) <= 4:
            return text
        shown = lines[:3] + [f"[… {len(lines) - 3} líneas más]"]
        if kind == "code":  # keep the fence balanced so the fragment still renders as code
            shown.append(_FENCE.match(lines[0]).group(1))
        return "\n".join(shown)
    if len(text) <= 160:
        return text
    sentences = _sentences(text)
    if len(sentences) <= 1:
        return text
    scored = []
    for index, sentence in enumerate(sentences):
        words = [w for w in _WORD.findall(sentence.lower()) if w not in _STOP]
        score = sum(freq.get(w, 0.0) for w in words) / math.sqrt(max(len(words), 1))
        if index == 0:
            score *= 1.5
        elif index == len(sentences) - 1:
            score *= 1.15
        scored.append((score, index))
    keep = sorted(index for _score, index in sorted(scored, key=lambda s: (-s[0], s[1]))[:_keep_count(len(sentences))])
    return " ".join(sentences[i] for i in keep)


def build(text: str) -> Dict[str, Any]:
    """The deterministic document of `text`: paragraphs, sections and level 2."""
    if not isinstance(text, str) or not text.strip():
        raise FoldError("text is empty")
    if len(text) > MAX_CHARS:
        raise FoldError(f"text is over {MAX_CHARS} characters")
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    paragraphs: List[Dict[str, Any]] = []
    sections: List[Dict[str, Any]] = []
    section: Optional[Dict[str, Any]] = None
    for block in _split_blocks(text):
        raw = text[block["start"]:block["end"]]
        kind = _kind(block["lines"])
        if section is None or kind == "heading":
            heading = _HEADING.match(block["lines"][0]) if kind == "heading" else None
            section = {"id": f"s{len(sections) + 1}", "title": heading.group(2).strip() if heading else "",
                       "level": len(heading.group(1)) if heading else 0, "paragraphs": []}
            sections.append(section)
        pid = f"p{len(paragraphs) + 1}"
        paragraphs.append({"id": pid, "section": section["id"], "kind": kind, "start": block["start"],
                           "end": block["end"], "text": raw, "levels": {}})
        section["paragraphs"].append(pid)
    freq = _frequencies(text)
    for paragraph in paragraphs:
        paragraph["levels"]["2"] = _extract(paragraph, freq)
    return {"format": FORMAT, "id": "fold_" + sha[:24], "sha256": sha, "chars": len(text),
            "levels": {str(k): v for k, v in LEVEL_NAMES.items()}, "sections": sections,
            "paragraphs": paragraphs, "topics": None, "generated": []}


# --------------------------------------------------------------------------- storage

def _store_dir(owner: str) -> str:
    from src.constants import DATA_DIR
    key = hashlib.sha256((owner or "").encode("utf-8")).hexdigest()[:16]
    return os.path.join(DATA_DIR, "text_fold", key)


def _path(owner: str, doc_id: str) -> str:
    if not re.fullmatch(r"fold_[0-9a-f]{24}", doc_id or ""):
        raise FoldNotFound("unknown fold id")
    return os.path.join(_store_dir(owner), doc_id + ".json")


def _prune(directory: str) -> None:
    cutoff = time.time() - _RETENTION_SECONDS
    try:
        for name in os.listdir(directory):
            full = os.path.join(directory, name)
            if name.endswith(".json") and os.path.getmtime(full) < cutoff:
                os.unlink(full)
    except OSError:
        pass


def save(owner: str, doc: Dict[str, Any]) -> None:
    directory = _store_dir(owner)
    os.makedirs(directory, exist_ok=True)
    target = _path(owner, doc["id"])
    tmp = target + f".{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, ensure_ascii=False)
    os.replace(tmp, target)
    _prune(directory)


def load(owner: str, doc_id: str) -> Dict[str, Any]:
    try:
        with open(_path(owner, doc_id), "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        raise FoldNotFound("unknown fold id") from None


def open_document(owner: str, text: str) -> Dict[str, Any]:
    """The cached document of `text` for `owner`, built and stored when new."""
    probe = "fold_" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]
    try:
        return load(owner, probe)
    except FoldNotFound:
        pass
    doc = build(text)
    save(owner, doc)
    return doc


# --------------------------------------------------------------------------- generation

_LANG = "Escribe en el mismo idioma que el texto original. No añadas nada que el texto no diga."


def _prompt_key(items: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    body = "\n\n".join(f"[{p['id']}] {p['text']}" for p in items)
    return [
        {"role": "system", "content": (
            "Resumes párrafos de un texto largo. Para CADA párrafo numerado escribe una sola línea con la idea "
            "principal y, tras ' — ', el porqué (la razón o consecuencia que da el texto). Máximo 220 caracteres. "
            "Formato exacto, una línea por párrafo: `[pN] idea — porqué`. Sin viñetas ni comentarios. " + _LANG)},
        {"role": "user", "content": body},
    ]


def _prompt_lines(doc: Dict[str, Any]) -> List[Dict[str, str]]:
    parts = []
    for section in doc["sections"]:
        text = " ".join(next(p for p in doc["paragraphs"] if p["id"] == pid)["levels"]["2"] for pid in section["paragraphs"])
        parts.append(f"[{section['id']}] {section['title'] or '(sin título)'}\n{text[:1500]}")
    return [
        {"role": "system", "content": (
            "Resumes cada sección de un texto en UNA línea de como máximo 140 caracteres. "
            "Formato exacto, una línea por sección: `[sN] resumen`. Sin viñetas ni comentarios. " + _LANG)},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


def _prompt_topics(doc: Dict[str, Any]) -> List[Dict[str, str]]:
    parts = [f"[{s['id']}] {s['title'] or '(sin título)'}: {s.get('line') or ''}" for s in doc["sections"]]
    return [
        {"role": "system", "content": (
            "Dibuja el mapa de temas de un texto: entre 2 y 8 tarjetas. Responde SOLO un array JSON de objetos "
            '{"title": "tema en pocas palabras", "gist": "una frase", "sections": ["s1", "s2"]} usando los '
            "identificadores de sección dados; cada sección aparece en una tarjeta. " + _LANG)},
        {"role": "user", "content": "\n".join(parts)},
    ]


def _tagged_lines(answer: str, valid: set) -> Dict[str, str]:
    found: Dict[str, str] = {}
    for line in answer.splitlines():
        match = re.match(r"^\s*(?:[-*]\s*)?\[?([ps]\d+)\]?[:.)\s-]+(.+\S)\s*$", line)
        if match and match.group(1) in valid and match.group(1) not in found:
            found[match.group(1)] = match.group(2).strip()
    return found


def _json_array(answer: str) -> List[Any]:
    match = re.search(r"\[.*\]", answer, re.S)
    try:
        value = json.loads(match.group(0)) if match else None
    except ValueError:
        value = None
    if not isinstance(value, list):
        raise FoldUnavailable("the model did not return the topic map")
    return value


async def default_llm(owner: str) -> LlmFn:
    """The global language model (utility endpoint) as a chat-completion function."""
    from src.endpoint_resolver import resolve_endpoint
    from src.llm_core import llm_call_async
    url, model, headers = resolve_endpoint("utility", owner=owner or None)
    if not url:
        raise FoldUnavailable("no language model endpoint is configured")

    async def call(messages: List[Dict[str, str]]) -> str:
        return await llm_call_async(url, model, messages, temperature=0.2, max_tokens=2000,
                                    headers=headers, timeout=120)
    return call


_LOCKS: Dict[str, asyncio.Lock] = {}


async def ensure_level(owner: str, doc: Dict[str, Any], level: int, llm: Optional[LlmFn] = None) -> Dict[str, Any]:
    """Make sure `level` exists in `doc` (generating and caching it when needed)."""
    if level not in LEVEL_NAMES:
        raise FoldError("level must be 1 to 5")
    if level not in GENERATED_LEVELS or level in doc.get("generated", []):
        return doc
    lock = _LOCKS.setdefault(f"{owner}:{doc['id']}", asyncio.Lock())
    async with lock:
        try:
            doc = load(owner, doc["id"])
        except FoldNotFound:
            pass
        if level in doc.get("generated", []):
            return doc
        try:
            call = llm or await default_llm(owner)
            if level == 3:
                await _generate_key(doc, call)
            else:
                await _generate_lines(doc, call)
                if level == 5:
                    await _generate_topics(doc, call)
        except FoldUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - any model failure is "unavailable", never a silent stand-in
            raise FoldUnavailable(f"the language model did not answer ({type(exc).__name__})") from exc
        save(owner, doc)
        return doc


def _index(doc: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {p["id"]: p for p in doc["paragraphs"]}


async def _generate_key(doc: Dict[str, Any], call: LlmFn) -> None:
    pending = [p for p in doc["paragraphs"] if p["kind"] in ("text", "list") and len(p["text"]) > 160]
    batches: List[List[Dict[str, Any]]] = []
    size = 0
    for paragraph in pending:
        if not batches or size + len(paragraph["text"]) > _BATCH_CHARS:
            batches.append([])
            size = 0
        batches[-1].append(paragraph)
        size += len(paragraph["text"])
    answers = await asyncio.gather(*(call(_prompt_key(batch)) for batch in batches))
    got: Dict[str, str] = {}
    for batch, answer in zip(batches, answers):
        got.update(_tagged_lines(answer, {p["id"] for p in batch}))
    if pending and not got:
        raise FoldUnavailable("the model did not return any key line")
    for paragraph in doc["paragraphs"]:
        # Paragraphs the model skipped (and the short, code or heading ones) keep their literal level 2.
        paragraph["levels"]["3"] = got.get(paragraph["id"]) or paragraph["levels"]["2"]
    doc["generated"] = sorted(set(doc.get("generated", [])) | {3})


async def _generate_lines(doc: Dict[str, Any], call: LlmFn) -> None:
    if 4 in doc.get("generated", []):
        return
    got = _tagged_lines(await call(_prompt_lines(doc)), {s["id"] for s in doc["sections"]})
    if not got:
        raise FoldUnavailable("the model did not return any section line")
    for section in doc["sections"]:
        section["line"] = got.get(section["id"]) or _first_words(section, doc)
    doc["generated"] = sorted(set(doc.get("generated", [])) | {4})


def _first_words(section: Dict[str, Any], doc: Dict[str, Any]) -> str:
    text = _index(doc)[section["paragraphs"][-1] if section["title"] else section["paragraphs"][0]]["levels"]["2"]
    return (section["title"] or text)[:140]


async def _generate_topics(doc: Dict[str, Any], call: LlmFn) -> None:
    cards = []
    known = {s["id"] for s in doc["sections"]}
    for raw in _json_array(await call(_prompt_topics(doc))):
        if not isinstance(raw, dict) or not str(raw.get("title") or "").strip():
            continue
        sections = [s for s in (raw.get("sections") or []) if s in known]
        cards.append({"title": str(raw["title"]).strip()[:80], "gist": str(raw.get("gist") or "").strip()[:240],
                      "sections": sections})
    if not cards:
        raise FoldUnavailable("the model did not return any topic")
    covered = {s for card in cards for s in card["sections"]}
    leftover = [s["id"] for s in doc["sections"] if s["id"] not in covered]
    if leftover:
        cards.append({"title": "Otros", "gist": "", "sections": leftover})
    doc["topics"] = cards
    doc["generated"] = sorted(set(doc.get("generated", [])) | {5})


# --------------------------------------------------------------------------- reading

def summary(doc: Dict[str, Any]) -> Dict[str, Any]:
    """The document without paragraph bodies: what a reader needs to choose a level."""
    return {"format": doc["format"], "id": doc["id"], "chars": doc["chars"], "levels": doc["levels"],
            "generated": doc.get("generated", []),
            "sections": [{"id": s["id"], "title": s["title"], "paragraphs": s["paragraphs"]} for s in doc["sections"]],
            "paragraph_count": len(doc["paragraphs"]),
            "chars_by_level": {str(n): view_chars(doc, n) for n in (1, 2) + tuple(g for g in GENERATED_LEVELS if g in doc.get("generated", []))}}


def view_chars(doc: Dict[str, Any], level: int) -> int:
    return len("\n\n".join(item["text"] for item in _items(doc, level, None, None)))


def _items(doc: Dict[str, Any], level: int, section: Optional[str], paragraph: Optional[str]) -> List[Dict[str, Any]]:
    paragraphs = doc["paragraphs"]
    if paragraph:
        found = _index(doc).get(paragraph)
        if not found:
            raise FoldNotFound("unknown paragraph")
        return [{"paragraph": found["id"], "section": found["section"], "kind": found["kind"], "text": found["text"]}]
    sections = doc["sections"]
    if section:
        sections = [s for s in sections if s["id"] == section]
        if not sections:
            raise FoldNotFound("unknown section")
    ids = {s["id"] for s in sections}
    if level == 5:
        cards = doc.get("topics") or []
        return [{"topic": i + 1, "title": c["title"], "sections": c["sections"],
                 "text": f"{c['title']}: {c['gist']}".rstrip(": ")}
                for i, c in enumerate(cards) if not section or ids & set(c["sections"])]
    if level == 4:
        return [{"section": s["id"], "title": s["title"], "text": s.get("line") or ""} for s in sections]
    out = []
    for p in paragraphs:
        if p["section"] not in ids:
            continue
        text = p["text"] if level == 1 else p["levels"].get(str(level)) or p["levels"]["2"]
        out.append({"paragraph": p["id"], "section": p["section"], "kind": p["kind"], "text": text})
    return out


async def read(owner: str, doc_id: str, level: int = 1, section: Optional[str] = None,
               paragraph: Optional[str] = None, llm: Optional[LlmFn] = None) -> Dict[str, Any]:
    """One level (optionally of one section, or the original of one paragraph) of a stored document."""
    try:
        level = int(level)
    except (TypeError, ValueError):
        raise FoldError("level must be 1 to 5") from None
    doc = await ensure_level(owner, load(owner, doc_id), level, llm)
    items = _items(doc, 1 if paragraph else level, section, paragraph)
    text = "\n\n".join(item["text"] for item in items)
    return {"format": doc["format"], "id": doc["id"], "level": 1 if paragraph else level,
            "name": LEVEL_NAMES[1 if paragraph else level], "section": section, "paragraph": paragraph,
            "chars": len(text), "original_chars": doc["chars"], "items": items, "text": text}


# --------------------------------------------------------------------------- sources

def text_of_artifact(owner: str, artifact_id: str, text_pointer: Optional[str] = None) -> str:
    """The text of a stored artifact (e.g. an offloaded tool result) for `owner`. A JSON
    object result folds on its longest string field unless `text_pointer` names one."""
    from src.tool_result_offload import ArtifactAccessDenied, read_artifact_range
    try:
        stored = read_artifact_range(artifact_id, owner=owner)["text"]
    except ArtifactAccessDenied:
        raise FoldNotFound("artifact not found") from None
    try:
        value = json.loads(stored)
    except ValueError:
        return stored
    if text_pointer:
        for token in [t.replace("~1", "/").replace("~0", "~") for t in text_pointer.split("/")[1:]]:
            value = value[int(token)] if isinstance(value, list) else value[token]
        if not isinstance(value, str):
            raise FoldError("text_pointer must select a string")
        return value
    if isinstance(value, dict):
        strings = [v for v in value.values() if isinstance(v, str)]
        if strings:
            return max(strings, key=len)
    return stored
