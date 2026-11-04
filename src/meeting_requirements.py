"""meeting_requirements.py -- from a meeting to requirements and open questions (Hangar proposal E27).

Reads a meeting transcript or notes line by line and sorts what was said into
**decision**, **promise**, **requirement**, **proposal** and **question**,
keeping the speaker and the timestamp of each line so every item can be traced
back to the moment it was said (``[00:12:34]`` lines, as Faustus meeting notes
and Talos transcripts write them). The rules are deliberately conservative:

* conditional or tentative wording ("podríamos", "se podría", "quizá",
  "could", "maybe", "what if") is a **proposal** -- never a promise or a
  requirement, whatever else the sentence says;
* a question mark or an opening "¿" makes a **question**;
* "decidimos", "queda acordado", "we agreed", "decided" make a **decision**;
* first-person future commitments ("te envío", "enviaré", "me encargo",
  "I'll send", "we will deliver") make a **promise**;
* obligations ("tiene que", "debe", "necesitamos", "must", "needs to")
  make a **requirement** candidate.

Nothing is promoted on its own: the commission office keeps the items with
the meeting and a person decides which ones become a decision, a requirement
(a change request of the scope) or a milestone.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Dict, List

KINDS = ("decision", "promise", "requirement", "proposal", "question")

_TS = re.compile(r"^\s*\[?(\d{1,2}:\d{2}(?::\d{2})?)\]?\s*[-–·]?\s*")
_SPEAKER = re.compile(r"^\s*([A-ZÁÉÍÓÚÑ][\wÁÉÍÓÚÑáéíóúñ.\- ]{0,40}?)\s*:\s+(?=\S)")
_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+(?:\[[ xX]\]\s*)?")

_PROPOSAL = re.compile(r"\b(podriamos|podria(?:mos)?|se podria|quizas?|tal vez|a lo mejor|igual (?:se )?podemos|propongo|"
                       r"sugiero|seria buena idea|que tal si|could|might|maybe|perhaps|what if|we could|i suggest|"
                       r"how about|it would be nice)\b")
_DECISION = re.compile(r"\b(decidimos|hemos decidido|se decide|queda(?:mos)? (?:acordado|en que)|acordamos|"
                       r"esta decidido|nos quedamos con|we agreed|agreed to|decided|we will go with|final decision)\b")
_PROMISE = re.compile(r"\b(te (?:envio|mando|paso|preparo)|os (?:envio|mando|paso)|le (?:envio|mando|paso)|enviare|mandare|"
                      r"entregare|preparare|me encargo|me comprometo|lo tengo para|lo tendre|nos encargamos|"
                      r"entregaremos|enviaremos|i'll|i will|we'll|we will|i can send|i'll send)\b")
_REQUIREMENT = re.compile(r"\b(tiene que|tienen que|hay que|tenemos que|tengo que|necesita|necesitan|requiere|requieren|"
                          r"we need|i need|they need|debe|deben|deberia|necesitamos|necesito|hace falta|es imprescindible|"
                          r"obligatorio|must|needs? to|has to|have to|required|should)\b")
_HEADING = re.compile(r"^\s*#{1,6}\s")


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def classify_line(text: str) -> str:
    low = _fold(text)
    if "?" in text or text.strip().startswith("¿"):
        return "question"
    if _PROPOSAL.search(low):
        return "proposal"
    if _DECISION.search(low):
        return "decision"
    if _PROMISE.search(low):
        return "promise"
    if _REQUIREMENT.search(low):
        return "requirement"
    return ""


def extract(text: str) -> List[Dict[str, Any]]:
    """Items with kind, text, speaker, time and line number; other lines are skipped."""
    items: List[Dict[str, Any]] = []
    for n, raw in enumerate((text or "").splitlines(), 1):
        if not raw.strip() or _HEADING.match(raw):
            continue
        line = _BULLET.sub("", raw)
        time = ""
        m = _TS.match(line)
        if m:
            time, line = m.group(1), line[m.end():]
        speaker = ""
        s = _SPEAKER.match(line)
        if s and len(s.group(1).split()) <= 4:
            speaker, line = s.group(1).strip(), line[s.end():]
        line = line.strip()
        if len(line) < 6:
            continue
        for sentence in re.split(r"(?<=[.!?])\s+(?=[A-ZÁÉÍÓÚÑ¿¡])", line):
            sentence = sentence.strip()
            kind = classify_line(sentence)
            if kind:
                items.append({"n": len(items) + 1, "kind": kind, "text": sentence[:500], "speaker": speaker,
                              "time": time, "line": n, "firm": kind in ("decision", "promise", "requirement")})
    return items


def counts(items: List[Dict[str, Any]]) -> Dict[str, int]:
    return {k: sum(1 for i in items if i["kind"] == k) for k in KINDS}
