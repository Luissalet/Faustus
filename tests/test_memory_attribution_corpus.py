"""Small hand-labelled EN/ES corpus for both deterministic memory fallbacks.

These are examples, not an estimate of production accuracy or LLM quality.
Spanish free-prose extraction is measured as missing recall, not a true negative.
"""
import pytest

from services.memory.memory_extractor import _fallback_memory_candidates
from src.memory import MemoryManager


# (id, path, role, input, gold output, required recall)
CASES = [
    ("en-name", "facts", "user", "My name is Alex", ["User's name is Alex"], True),
    ("en-place", "facts", "user", "I live in Lisbon", ["User lives in Lisbon"], True),
    ("en-like", "facts", "user", "I like green tea", ["User prefers green tea"], True),
    ("en-dislike", "facts", "user", "I don't like crowded trains", ["User dislikes crowded trains"], True),
    ("es-name", "facts", "user", "Me llamo Álex", ["El usuario se llama Álex"], False),
    ("es-place", "facts", "user", "Vivo en Madrid", ["El usuario vive en Madrid"], False),
    ("es-like", "facts", "user", "Me gusta el té verde", ["Al usuario le gusta el té verde"], False),
    ("es-dislike", "facts", "user", "No me gustan los trenes llenos", ["Al usuario no le gustan los trenes llenos"], False),
    ("en-quoted", "facts", "user", 'My colleague said "I like green tea"', [], True),
    ("es-quoted", "facts", "user", 'Mi colega dijo «I like green tea»', [], True),
    ("en-curly", "facts", "user", 'She wrote “I live in Lisbon”', [], True),
    ("es-curly", "facts", "user", 'Ella escribió ‘I live in Lisbon’', [], True),
    ("en-blockquote", "facts", "user", '> I like green tea', [], True),
    ("es-blockquote", "facts", "user", 'Texto de otra persona:\n> I live in Lisbon', [], True),
    ("en-fence", "facts", "user", '```text\nI like green tea\n```', [], True),
    ("es-fence", "facts", "user", 'Ejemplo:\n~~~text\nI live in Lisbon\n~~~', [], True),
    ("en-inline", "facts", "user", 'Example: `I like green tea`', [], True),
    ("es-inline", "facts", "user", 'Ejemplo: ``I live in Lisbon``', [], True),
    ("en-multiline-quote", "facts", "user", 'She wrote "First line\nI like green tea\nEnd"', [], True),
    ("es-unclosed-fence", "facts", "user", 'Ejemplo:\n```text\nI like green tea', [], True),
    ("en-mixed", "facts", "user", 'She said "I like coffee", but I prefer green tea', ["User prefers green tea"], True),
    ("es-mixed", "facts", "user", 'Ella dijo «I like coffee». I prefer green tea', ["User prefers green tea"], True),
    ("en-translate", "facts", "user", 'Please translate: I like green tea', [], True),
    ("es-translate", "facts", "user", 'Por favor, traduce:\nI live in Lisbon', [], True),
    ("en-rewrite", "facts", "user", 'Rewrite this: My name is Alex', [], True),
    ("es-correct", "facts", "user", 'Corrige este texto: I like green tea', [], True),
    ("en-list", "list", "user", 'Remember these:\n- I like green tea', ["I like green tea"], True),
    ("es-list", "list", "user", 'Recuerda esto:\n- Me gusta el té verde', ["Me gusta el té verde"], True),
    ("en-polite-list", "list", "user", 'Please, remember:\n1. I live in Lisbon', ["I live in Lisbon"], True),
    ("es-polite-list", "list", "user", 'Por favor, guarda en memoria:\n1. Vivo en Madrid', ["Vivo en Madrid"], True),
    ("en-memorize", "list", "user", 'Memorize:\n* My name is Alex', ["My name is Alex"], True),
    ("es-memorize", "list", "user", 'Memoriza:\n* Me llamo Álex', ["Me llamo Álex"], True),
    ("en-task-list", "list", "user", 'Please complete these tasks:\n- Buy milk', [], True),
    ("es-task-list", "list", "user", 'Haz estas tareas:\n- Compra leche', [], True),
    ("en-mentioned-remember", "list", "user", 'Explain what remember means:\n- Buy milk', [], True),
    ("es-mentioned-recuerda", "list", "user", 'Explica qué significa recuerda:\n- Compra leche', [], True),
    ("en-quoted-list", "list", "user", '"Remember:\n- I live in Lisbon"', [], True),
    ("es-fenced-list", "list", "user", '```\nRecuerda:\n- Vivo en Madrid\n```', [], True),
    ("en-assistant", "facts", "assistant", 'I like green tea', [], True),
    ("es-assistant", "list", "assistant", 'Recuerda:\n- Vivo en Madrid', [], True),
    ("en-tool", "facts", "tool", 'My name is Alex', [], True),
    ("es-tool", "list", "tool", 'Memoriza:\n- Me llamo Álex', [], True),
]


def output(case, manager):
    _, path, role, content, _, _ = case
    messages = [{"role": role, "content": content}]
    rows = (_fallback_memory_candidates(messages) if path == "facts"
            else manager.extract_memory_from_chat(messages))
    return {row["text"].rstrip(".") for row in rows}


@pytest.mark.parametrize("case", CASES, ids=[case[0] for case in CASES])
def test_attribution_case(case, tmp_path):
    actual = output(case, MemoryManager(str(tmp_path)))
    gold = set(case[4])
    assert actual <= gold, "Unexpected or incorrectly attributed memory"
    if case[5]:
        assert actual == gold


def test_corpus_metrics(tmp_path):
    manager = MemoryManager(str(tmp_path))
    tp = fp = fn = 0
    for case in CASES:
        actual, gold = output(case, manager), set(case[4])
        tp += len(actual & gold)
        fp += len(actual - gold)
        fn += len(gold - actual)
    print(f"attribution corpus: cases={len(CASES)} TP={tp} FP={fp} FN={fn}")
    assert fp == 0
    assert tp >= 12
