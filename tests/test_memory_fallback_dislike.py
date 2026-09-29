"""The fallback memory extractor must not invert dislikes into preferences.

_fallback_memory_candidates matched both positive (prefer/like/love) and
negative (hate/do not like/don't like) sentiment verbs in one alternation but
formatted every hit as "User prefers X.", so "I hate cilantro" was stored as
"User prefers cilantro" -- the opposite of what the user said, then persisted
to memory and re-injected into context. These pin the sentiment.
"""
from services.memory.memory_extractor import _fallback_memory_candidates


def _texts(content):
    cands = _fallback_memory_candidates([{"role": "user", "content": content}])
    return [c["text"].lower() for c in cands]


def test_dislike_is_not_stored_as_preference():
    texts = _texts("I hate cilantro in my food")
    assert not any("prefers cilantro" in t for t in texts)
    assert any("dislikes cilantro" in t for t in texts)


def test_negated_like_is_not_stored_as_preference():
    texts = _texts("I don't like crowded trains")
    assert not any("prefers crowded" in t for t in texts)
    assert any("dislikes crowded" in t for t in texts)


def test_genuine_preference_still_stored():
    texts = _texts("I love spicy ramen noodles")
    assert any("prefers spicy ramen" in t for t in texts)


def test_quoted_first_person_claim_is_not_the_users_preference():
    for quote in ('"I love spicy ramen"', '“I love spicy ramen”', "'I love spicy ramen'"):
        assert _texts(f"My colleague said {quote}, but I prefer green tea") == [
            "user prefers green tea"]
