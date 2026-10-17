"""How the members of a room talk to each other after answering the person.

A room is a group chat: the person writes, every member answers, and then the
members may answer each other. A member hands the floor to a peer with an
@mention ("@qwen, can you check the parser?"); the mentioned peer speaks next
in a follow-up round. After an "all at once" round nobody has read the others
yet, so everyone gets one round to react. A member with nothing new to add
answers ``[PASS]``. Rounds stop when nobody is addressed, everyone passes, the
room's ``discussionRounds`` limit is reached, or the person writes again.

Follow-up rounds are ordinary queued turns of the shared group store whose
``request_id`` starts with ``peer:`` (no new store operation, so the Hub's
canonical repository serves them unchanged). They run one member after the
other so each speaker reads the previous one.
"""
from __future__ import annotations

import re
import uuid

PEER_PREFIX = "peer:"
DEFAULT_ROUNDS = 3
MAX_ROUNDS = 12
PASS_TEXT = "[PASS]"


def rounds_limit(definition) -> int:
    raw = (definition or {}).get("discussionRounds", DEFAULT_ROUNDS)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_ROUNDS
    return max(0, min(MAX_ROUNDS, value))


def is_peer(turn) -> bool:
    return str((turn or {}).get("request_id") or "").startswith(PEER_PREFIX)


def peer_round(turn) -> int:
    """0 for the person's own message, N for the Nth follow-up round."""
    if not is_peer(turn):
        return 0
    parts = str(turn["request_id"]).split(":")
    try:
        return int(parts[2])
    except (IndexError, ValueError):
        return 1


def origin(turn) -> str:
    if not is_peer(turn):
        return str(turn.get("id") or "")
    parts = str(turn["request_id"]).split(":")
    return parts[1] if len(parts) > 1 else ""


def peer_request_id(origin_turn_id: str, round_number: int) -> str:
    return f"{PEER_PREFIX}{origin_turn_id}:{round_number}:{uuid.uuid4().hex[:12]}"


def is_pass(text) -> bool:
    value = str(text or "").strip()
    if not value:
        return True
    if re.match(r"^\W*\[\s*pass\s*\]", value, re.IGNORECASE):
        return True
    return bool(re.fullmatch(r"\W*pass\W*", value, re.IGNORECASE))


def _tokens(name: str) -> list[str]:
    name = str(name or "").strip()
    out = [name.lower()]
    first = re.split(r"[\s\-_.:/()]+", name)[0] if name else ""
    alpha = re.match(r"[A-Za-z]+", name)
    for candidate in (first, alpha.group(0) if alpha else ""):
        candidate = candidate.lower()
        if len(candidate) >= 3 and candidate not in out:
            out.append(candidate)
    return out


def handles(members) -> dict[str, list[str]]:
    """Every member's @handles: the full name plus a short unique prefix."""
    raw = {m["sessionId"]: _tokens(m.get("personaName") or m.get("route", {}).get("model", "")) for m in members}
    counts: dict[str, int] = {}
    for values in raw.values():
        for value in set(values):
            counts[value] = counts.get(value, 0) + 1
    return {sid: [values[0], *[v for v in values[1:] if counts.get(v) == 1]] for sid, values in raw.items()}


def short_handle(member, members) -> str:
    values = handles(members).get(member["sessionId"]) or [member.get("personaName", "")]
    return values[-1] if len(values) > 1 else values[0]


def mentioned(text, members, author_id="") -> list[str]:
    """Members addressed in ``text`` (by @handle, or by name opening a sentence)."""
    found = []
    body = str(text or "")
    for sid, values in handles(members).items():
        if sid == author_id:
            continue
        for value in sorted(values, key=len, reverse=True):
            name = re.escape(value)
            at = re.search(r"(?<![\w@])@" + name + r"(?![\w-]*[\w])", body, re.IGNORECASE)
            vocative = re.search(r"(?:^|[\n.!?¿¡]\s*)" + name + r"\s*[,:]", body, re.IGNORECASE)
            if at or vocative:
                found.append(sid)
                break
    return found


def next_round(turn, steps, members, mode, limit):
    """Who speaks in the follow-up round after ``turn``, or ``None`` to stop."""
    current = peer_round(turn)
    if limit <= 0 or current >= limit:
        return None
    by_id = {m["sessionId"]: m for m in members}
    spoke = [s for s in steps if s.get("status") == "done" and s.get("member_id") in by_id and not is_pass(s.get("text"))]
    if not spoke:
        return None
    targets: list[str] = []
    for step in spoke:
        for sid in mentioned(step.get("text"), members, step.get("member_id")):
            if sid not in targets:
                targets.append(sid)
    if current == 0 and mode == "parallel" and len(spoke) >= 2:
        # Nobody has read the others yet: everyone may react once.
        for step in spoke:
            if step["member_id"] not in targets:
                targets.append(step["member_id"])
    if not targets:
        return None
    ordered = [m["sessionId"] for m in members if m["sessionId"] in targets]
    return {"round": current + 1, "members": ordered, "origin": origin(turn)}


def roster(members) -> str:
    return ", ".join(f"{m.get('personaName') or m['route']['model']} (@{short_handle(m, members)})" for m in members)


def rules(member, members, limit) -> str:
    me = member.get("personaName") or member["route"]["model"]
    others = [m for m in members if m["sessionId"] != member["sessionId"]]
    example = short_handle(others[0], members) if others else "peer"
    lines = [
        f"This is a group chat with the person and {len(others)} other model(s). Members: {roster(members)}. You are {me}.",
        "- Talk like in a real group chat: answer the person, and answer, correct, challenge or build on what your peers said, addressing them directly.",
        f"- To ask a peer something or hand them work, mention them with @handle (for example @{example}); a mentioned peer speaks next.",
        "- Do not repeat what someone already said, do not speak for others, and do not just agree: agreement alone adds nothing.",
        f"- If you have nothing new to add, reply exactly {PASS_TEXT}.",
        "- When the work needs splitting, put it on the project's Agora board with the coordination tools if you have them, and say who takes what.",
    ]
    if limit <= 0:
        lines[2] = "- Address peers by name when you answer them."
    return "\n".join(lines)


def peer_prompt(round_number, limit, names=()) -> str:
    who = f" Your turn: {', '.join(names)}." if names else ""
    return (f"(Group chat, follow-up {round_number}/{limit}.){who} Your peers' new messages are in the shared "
            "discussion above. Reply only if you add something new: answer what you were asked, correct, disagree or "
            f"build on it, addressing them with @handle. Otherwise reply exactly {PASS_TEXT}.")
