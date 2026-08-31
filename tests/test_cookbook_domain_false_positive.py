"""`server.py` is a filename, not a machine that serves models.

The Cookbook (model-serving) classifier matched a bare "server", so
"añade a server.py una función health()" was treated as a model-serving turn
and unioned in all 13 Cookbook tool schemas. server.py / server.js / server.ts
are among the commonest filenames there are, so this fired constantly on
ordinary coding requests.
"""
from src import agent_loop as al

COOKBOOK = "cookbook"


def _domains(text):
    return set(al._classify_agent_request([{"role": "user", "content": text}], text)
               .get("domains") or set())


def test_a_source_file_named_server_is_not_a_model_serving_turn():
    for text in (
        "añade a server.py una función health() que devuelva ok",
        "arregla el bug de server.js",
        "refactoriza static/js/server.ts",
        "read server.go and tell me what it does",
    ):
        assert COOKBOOK not in _domains(text), text


def test_the_real_model_serving_requests_still_classify():
    for text in (
        "what's running on the server?",
        "serve qwen on the gpu box",
        "stop the model that is serving",
        "download minimax on the workstation",
        "list my cached models",
        "kill the cookbook",
    ):
        assert COOKBOOK in _domains(text), text


def test_a_bare_server_still_counts_as_the_machine():
    assert COOKBOOK in _domains("is the server up?")
    assert COOKBOOK in _domains("restart the server please")


def test_the_continuation_context_regex_agrees():
    # Used to decide whether a terse follow-up may inherit a Cookbook turn.
    assert not al._COOKBOOK_CONTEXT_RE.search("edit server.py")
    assert al._COOKBOOK_CONTEXT_RE.search("check the server")
