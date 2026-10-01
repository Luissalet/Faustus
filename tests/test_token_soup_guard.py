"""Word salad across writing systems aborts the stream within a window.

24-09-2026: after another process loaded a large model onto the same GPUs,
llama-server answered every request with fragments like the SOUP below from
the first token. Only 14% of its letters were non-Latin, so the
script-fraction rule never fired; the turn waited fifteen minutes for the
server's own HTTP 500.
"""
import pytest

import src.llm_core as _llm_core
from src.llm_core import _DegenerateStreamGuard, _token_soup_reason


def _degenerate():
    # Looked up when the test runs: other tests reload src.llm_core, which
    # rebinds the class the guard raises inside the same module namespace.
    return _llm_core.DegenerateOutput

SOUP = (
    "orque多大wanysofarotre成果 compens狼癞wanyotre Jus控eel Jusubinularyolderswany狼eitasofar大义"
    "ratvern_MANYafcotrevernehucontrawanyeelrat**),念eelakarOLDchalahuieelograminting丽丝ääneel狼eel徉"
    "pillarายนeel狼wany成果ahui vila:Systemorque狼狼modifiablealone本场徉-status狼orque连城влаeelotreogram"
    "腾丽丝eeleitairus多大.projorque狼afceita狼wanyvern芬eel韬多大祥卧龙控vernehuorqueجن狼 fetrat狼手eel"
    "iftersofar成果enu选择不lom念eitaogramafari**),orque狼olders Clarke多大rat狼afari**),**),orque狼卧龙"
)


def _feed(text, chunk=3, reasoning=True):
    guard = _DegenerateStreamGuard("m")
    for i in range(0, len(text), chunk):
        piece = text[i:i + chunk]
        (guard.check_reasoning if reasoning else guard.check)(piece)


def test_the_live_soup_is_caught_in_the_reasoning_channel():
    with pytest.raises(_degenerate()) as exc:
        _feed(SOUP)
    assert "word salad" in exc.value.reason
    assert "restarting it" in exc.value.reason


def test_and_in_the_content_channel():
    with pytest.raises(_degenerate()):
        _feed(SOUP, reasoning=False)


LEGIT = [
    # Spanish prose with accents
    ("La interpretación de los números con formato español ocurre en una sola función, "
     "que decide si una columna es numérica y reescribe la consulta. ") * 6,
    # Chinese / English technical mix (one foreign script)
    ("使用 Python 的 requests 库发送 HTTP 请求，然后用 json() 解析返回的数据。"
     "如果 status_code 不是 200，就记录错误并重试三次。") * 4,
    # Japanese with kanji, hiragana and katakana (all one group)
    ("これはテストです。ファイルを読み込んで、データを確認してください。"
     "エラーが発生した場合は、ログを参照してください。") * 5,
    # Code and JSON
    ('{"name": "ventas", "rows": 1500, "columns": ["fecha", "importe"], "ok": true}\n' * 6),
    ("def f(x):\n    return [i * 2 for i in range(x) if i % 3 == 0]\n" * 6),
    # Greek letters in maths
    ("Sea α el ángulo y β la pendiente; entonces tan(α) = β / γ cuando γ ≠ 0. ") * 6,
    # A quote in Russian inside English
    ("The Russian phrase «Всё хорошо, спасибо» means everything is fine, thanks. ") * 5,
]


@pytest.mark.parametrize("text", LEGIT)
def test_real_text_is_not_word_salad(text):
    # Every window the guard could look at. (The repeated samples would trip
    # the separate sentence-loop and script-fraction rules, which is not what
    # this test is about.)
    for start in range(0, max(1, len(text) - 300), 7):
        assert _token_soup_reason(text[start:start + 300]) is None, text[start:start + 300]


def test_a_run_of_one_symbol_points_at_the_server_not_the_sampler():
    import json as _json
    exc = _degenerate()("repeated unit '/' 120 times (120 chars)", "qwen-test")
    chunk = _llm_core._degenerate_output_error_chunk(exc)
    data = _json.loads(chunk.split("data: ", 1)[1])
    assert "restarting the server" in data["error"]
    word = _llm_core._degenerate_output_error_chunk(_degenerate()("repeated unit 'ok ' 40 times", "m"))
    assert "lower temperature" in word
