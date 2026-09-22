# -*- coding: utf-8 -*-
"""Memory must not keep a snapshot of the workspace as a fact about the user.

The two entries that prompted this, both stored and both already pulled into
three prompts before anyone looked:

    "En la raiz del workspace hay 2 ficheros: `datos.json` y `NOTAS.md`."
    "Hay 2 ficheros en la raiz del workspace."

The second half of this file matters more than the first: a guard that eats
good facts is worse than the problem it solves, so every real memory that
was in the store at the time is asserted to survive.
"""

import pytest

from services.memory.volatile_facts import is_volatile, volatile_reason

REJECTED = [
    "En la raiz del workspace hay 2 ficheros: `datos.json` y `NOTAS.md`.",
    "En la raíz del workspace hay 2 ficheros: `datos.json` y `NOTAS.md`.",
    "Hay 2 ficheros en la raiz del workspace.",
    "There are 3 files in the workspace root.",
    "The workspace root contains 4 folders.",
    "El directorio actual tiene 12 archivos.",
    "The user is currently working on the login screen.",
    "Ahora mismo el usuario esta revisando el informe.",
]

KEPT = [
    "User's GitHub account is named Luissalet.",
    "User lives in or is located in Mostoles.",
    "User communicates in Spanish.",
    "User prefers working in millimeters (mm) for physical dimensions.",
    "User's pipeline converts PNG to watertight STL with SVG as single source of truth.",
    "User uses Python 3.13 on Windows for development.",
    "User is developing a 3D printing pipeline project named Faustus.",
    "User prefers implementation to follow a strict microtask-based execution plan.",
    "User prefers daily weather updates at 8:00 AM.",
    "When you make changes in code, you never commit, I will commit myself.",
    # Durable and about files, but about how the user works, not a snapshot.
    "User keeps every deliverable in a folder named exports.",
]


@pytest.mark.parametrize("text", REJECTED)
def test_snapshots_are_rejected(text):
    reason = volatile_reason(text)
    assert reason, f"should have been rejected: {text}"
    assert isinstance(reason, str) and reason


@pytest.mark.parametrize("text", KEPT)
def test_real_memories_survive(text):
    assert volatile_reason(text) is None, f"wrongly rejected: {text}"


def test_empty_input_is_not_volatile():
    assert is_volatile("") is False
    assert is_volatile(None) is False
