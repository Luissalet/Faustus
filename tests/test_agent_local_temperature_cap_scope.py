"""The local agent temperature cap follows coding intent, not workspace binding."""

import asyncio
import json

import pytest

import src.agent_loop as agent_loop


def _events(chunks):
    parsed = []
    for chunk in chunks:
        if chunk.startswith("data: ") and not chunk.startswith("data: [DONE]"):
            try:
                parsed.append(json.loads(chunk[6:]))
            except json.JSONDecodeError:
                pass
    return parsed


def _capture_loop(monkeypatch, tmp_path, messages, endpoint, initial, explicit):
    """Run one real agent-loop turn with a fake provider boundary."""
    captured = {}

    async def fake_provider(candidates, messages, **kwargs):
        captured.update(kwargs)
        yield 'data: {"delta":"Done."}\n\n'
        yield 'data: {"type":"finish","finish_reason":"stop"}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", fake_provider)
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(agent_loop, "estimate_tokens", lambda *args, **kwargs: 10)
    monkeypatch.setattr(agent_loop, "get_setting", lambda key, default=None: 0.4 if key == "agent_local_temperature_cap" else default)
    monkeypatch.setattr(agent_loop, "blocked_tools_for_owner", lambda owner: set())

    async def run():
        stream = agent_loop.stream_agent_loop(
            endpoint,
            "test-model",
            messages,
            temperature=initial,
            temperature_explicit=explicit,
            workspace=str(tmp_path),
            max_rounds=1,
            context_length=32768,
            relevant_tools=set(),
            harness_options={"checkpoints": False, "run_tests": False, "repo_map": False},
        )
        return [chunk async for chunk in stream]

    events = _events(asyncio.run(run()))
    info = next(event for event in events if event.get("type") == "round_info")
    return captured["temperature"], info


@pytest.mark.parametrize(
    ("endpoint", "user", "explicit", "initial", "expected", "capped_from", "cap"),
    [
        (
            "http://127.0.0.1:8081/v1",
            "Preview this CSV in the workspace and explain its columns.",
            False,
            0.6,
            0.6,
            None,
            None,
        ),
        (
            "http://127.0.0.1:8081/v1",
            "Crea un proyecto local Atlas con este objetivo, vincula estos archivos y usa solo herramientas MCP.",
            False,
            0.6,
            0.6,
            None,
            None,
        ),
        (
            "http://127.0.0.1:8081/v1",
            "Create a project folder, add a README and link the existing planning documents.",
            False,
            0.6,
            0.6,
            None,
            None,
        ),
        (
            "http://127.0.0.1:8081/v1",
            "Create a document with these sections and format the headings.",
            False,
            0.6,
            0.6,
            None,
            None,
        ),
        (
            "http://127.0.0.1:8081/v1",
            "Write a report about the HomeHoard API.",
            False,
            0.6,
            0.6,
            None,
            None,
        ),
        (
            "http://127.0.0.1:8081/v1",
            "Write a poem about `parse()`. ",
            False,
            0.6,
            0.6,
            None,
            None,
        ),
        (
            "http://127.0.0.1:8081/v1",
            "Escribe un capítulo donde el personaje descubre la función del amuleto.",
            False,
            0.6,
            0.6,
            None,
            None,
        ),
        (
            "http://127.0.0.1:8081/v1",
            "Actualiza el código postal en el documento de la empresa.",
            False,
            0.6,
            0.6,
            None,
            None,
        ),
        (
            "http://127.0.0.1:8081/v1",
            "Crea un documento con las pruebas del examen de la clase de mañana.",
            False,
            0.6,
            0.6,
            None,
            None,
        ),
        *[
            (
                "http://127.0.0.1:8081/v1",
                user,
                False,
                0.6,
                0.6,
                None,
                None,
            )
            for user in (
                "Escribe un correo (urgente) sobre el proyecto.",
                "Revisa el archivo de ventas (2025) y corrige los errores de redacción.",
                "Crea una lista de la compra (semana 3) en la carpeta casa.",
                "Escribe un ensayo sobre la clase (módulo) de filosofía.",
                "Actualiza la presentación de iPhone en la carpeta de marketing.",
                "Corrige la ortografía del archivo notas_clase.txt.",
                "Revisa el archivo mi_curriculum.docx y mejora la redacción.",
                "Escribe un guion para un vídeo corporativo.",
            )
        ],
        (
            "http://127.0.0.1:8081/v1",
            "Añade una página al menú y cambia el texto de sus opciones.",
            False,
            0.6,
            0.6,
            None,
            None,
        ),
        (
            "http://127.0.0.1:8081/v1",
            "Create src/app.py implementing parse().",
            False,
            0.6,
            0.4,
            0.6,
            0.4,
        ),
        (
            "http://127.0.0.1:8081/v1",
            "Escribe una funcion parse en src/app.py.",
            False,
            0.6,
            0.4,
            0.6,
            0.4,
        ),
        (
            "http://127.0.0.1:8081/v1",
            "Edit src/parser.py to fix the failing test.",
            False,
            0.6,
            0.4,
            0.6,
            0.4,
        ),
        (
            "http://127.0.0.1:8081/v1",
            "Implement the API endpoint in src/routes.py and add a pytest regression.",
            False,
            0.6,
            0.4,
            0.6,
            0.4,
        ),
        (
            "http://127.0.0.1:8081/v1",
            "RuntimeError: the parser crashes on an empty input. Fix src/parser.py.",
            False,
            0.6,
            0.4,
            0.6,
            0.4,
        ),
        (
            "http://127.0.0.1:8081/v1",
            "Edit src/parser.py to fix the failing test.",
            True,
            0.9,
            0.9,
            None,
            None,
        ),
        (
            "https://api.example.test/v1",
            "Edit src/parser.py to fix the failing test.",
            False,
            0.6,
            0.6,
            None,
            None,
        ),
        *[
            (
                "http://127.0.0.1:8081/v1",
                user,
                False,
                0.6,
                0.4,
                0.6,
                0.4,
            )
            for user in (
                "Create a script `backup.py` to archive project files.",
                "Crea un script en bash para archivar los archivos del proyecto.",
                "Escribe un script de Python para renombrar archivos.",
                "Update the stylesheet file site.css to fix the layout.",
                "Update the HTML file index.html to fix the heading.",
                "Update the JSON config file app.json to correct the timeout.",
                "Update the YAML config file app.yaml to correct the timeout.",
                "Update the YAML config file app.yml to correct the timeout.",
                "Update the TOML config file pyproject.toml to correct the timeout.",
            )
        ],
    ],
    ids=(
        "workspace-data-task", "local-project-mcp", "project-files", "document-formatting",
        "api-report-prose", "code-poem-prose", "fiction-function-word", "postal-code-document",
        "exam-document",
        "email-parenthetical", "sales-year-parenthetical", "shopping-list-parenthetical",
        "philosophy-class-parenthetical", "iphone-presentation", "snake-case-text-filename",
        "snake-case-docx-filename", "script-prose-guion", "ui-menu", "create-code-file",
        "write-code-function", "local-coding", "api-test-coding", "runtime-error",
        "explicit-temperature", "remote-coding", "create-script-file", "create-script-bash",
        "write-script-python", "stylesheet-css", "document-html", "configuration-json",
        "configuration-yaml", "configuration-yml", "configuration-toml",
    ),
)
def test_stream_loop_temperature_cap_tracks_coding_intent(
    monkeypatch, tmp_path, endpoint, user, explicit, initial, expected, capped_from, cap
):
    """Exercise the real stream loop through its provider boundary, with no LLM."""
    temperature, info = _capture_loop(
        monkeypatch,
        tmp_path,
        [{"role": "user", "content": user}],
        endpoint,
        initial,
        explicit,
    )

    assert temperature == expected
    assert info["temperature"] == expected
    assert info["temperature_capped_from"] == capped_from
    assert info["temperature_cap"] == cap
    # Preserve existing consumers of the legacy field.
    assert info["temperature_capped"] == capped_from


@pytest.mark.parametrize(
    "followup",
    [
        "sigue",
        "hazlo",
        "continue",
        "dale",
        "Dale!",
        "dale ya",
        "go on",
        "vale",
        "seguimos",
        "continuar",
    ],
)
def test_short_followup_inherits_coding_cap_from_same_history(
    monkeypatch, tmp_path, followup
):
    messages = [
        {"role": "user", "content": "Fix the parser bug in src/parser.py and run its pytest."},
        {"role": "assistant", "content": "The test identifies the failing branch."},
        {"role": "user", "content": followup},
    ]
    temperature, info = _capture_loop(
        monkeypatch, tmp_path, messages, "http://127.0.0.1:8081/v1", 0.6, False
    )
    assert temperature == 0.4
    assert info["temperature"] == 0.4
    assert info["temperature_capped_from"] == 0.6
    assert info["temperature_cap"] == 0.4


@pytest.mark.parametrize(
    "coding_request",
    [
        "Create a script `backup.py` to archive project files.",
        "Crea un script en bash para archivar los archivos del proyecto.",
        "Escribe un script de Python para renombrar archivos.",
    ],
)
def test_script_coding_request_seeds_inherited_followup(
    monkeypatch, tmp_path, coding_request
):
    messages = [
        {"role": "user", "content": coding_request},
        {"role": "assistant", "content": "The script plan is ready."},
        {"role": "user", "content": "dale"},
    ]
    temperature, info = _capture_loop(
        monkeypatch, tmp_path, messages, "http://127.0.0.1:8081/v1", 0.6, False
    )
    assert temperature == 0.4
    assert info["temperature"] == 0.4
    assert info["temperature_capped_from"] == 0.6
    assert info["temperature_cap"] == 0.4


def test_consecutive_neutral_followups_keep_the_same_coding_scope(
    monkeypatch, tmp_path
):
    messages = [
        {"role": "user", "content": "Fix the parser bug in src/parser.py and run its pytest."},
        {"role": "assistant", "content": "The failing assertion is fixed."},
        {"role": "user", "content": "continue"},
        {"role": "assistant", "content": "I am checking the related path."},
        {"role": "user", "content": "sigue"},
    ]
    temperature, info = _capture_loop(
        monkeypatch, tmp_path, messages, "http://127.0.0.1:8081/v1", 0.6, False
    )
    assert temperature == 0.4
    assert info["temperature"] == 0.4
    assert info["temperature_capped_from"] == 0.6
    assert info["temperature_cap"] == 0.4


@pytest.mark.parametrize(
    "followup",
    [
        "sigue",
        "hazlo",
        "continue",
        "dale",
        "Dale!",
        "dale ya",
        "go on",
        "vale",
        "seguimos",
        "continuar",
    ],
)
def test_short_followup_without_same_history_does_not_inherit(
    monkeypatch, tmp_path, followup
):
    temperature, info = _capture_loop(
        monkeypatch,
        tmp_path,
        [{"role": "user", "content": followup}],
        "http://127.0.0.1:8081/v1",
        0.6,
        False,
    )
    assert temperature == 0.6
    assert info["temperature"] == 0.6
    assert info["temperature_capped_from"] is None
    assert info["temperature_cap"] is None


def test_untrusted_synthetic_user_context_cannot_seed_followup_scope():
    messages = [
        {
            "role": "user",
            "content": "Fix the bug in src/parser.py and run pytest.",
            "metadata": {"trusted": False},
        },
        {"role": "user", "content": "Continue."},
    ]
    assert not agent_loop._sampling_cap_scope_from_history(messages, "Continue.")


@pytest.mark.parametrize("domain_request", [
    "Write a new document with the project requirements.",
    "Prepare a recipe for dinner with lentils.",
    "Start a new chat about travel plans.",
    "Write a report about the HomeHoard API.",
    "Write a poem about `parse()`.",
    "Escribe un capítulo donde el personaje descubre la función del amuleto.",
    "Actualiza el código postal en el documento de la empresa.",
    "Crea un documento con las pruebas del examen de la clase de mañana.",
])
def test_new_domain_request_breaks_inherited_coding_cap(
    monkeypatch, tmp_path, domain_request
):
    messages = [
        {"role": "user", "content": "Fix the parser bug in src/parser.py and run its pytest."},
        {"role": "assistant", "content": "The parser tests are green."},
        {"role": "user", "content": domain_request},
    ]
    temperature, info = _capture_loop(
        monkeypatch, tmp_path, messages, "http://127.0.0.1:8081/v1", 0.6, False
    )
    assert temperature == 0.6
    assert info["temperature_capped_from"] is None


def test_neutral_followup_after_domain_change_does_not_reach_older_code_turn(
    monkeypatch, tmp_path
):
    messages = [
        {"role": "user", "content": "Fix the parser bug in src/parser.py and run its pytest."},
        {"role": "assistant", "content": "The parser tests are green."},
        {"role": "user", "content": "Write a new document with the project requirements."},
        {"role": "assistant", "content": "The document outline is ready."},
        {"role": "user", "content": "Continue."},
    ]
    temperature, info = _capture_loop(
        monkeypatch, tmp_path, messages, "http://127.0.0.1:8081/v1", 0.6, False
    )
    assert temperature == 0.6
    assert info["temperature_capped_from"] is None


def test_followup_history_does_not_leak_across_chat_calls(monkeypatch, tmp_path):
    endpoint = "http://127.0.0.1:8081/v1"
    coding_history = [
        {"role": "user", "content": "Fix the parser bug in src/parser.py."},
        {"role": "assistant", "content": "I found the regression."},
        {"role": "user", "content": "continue"},
    ]
    capped_temperature, capped_info = _capture_loop(
        monkeypatch, tmp_path, coding_history, endpoint, 0.6, False
    )
    separate_chat_temperature, separate_chat_info = _capture_loop(
        monkeypatch,
        tmp_path,
        [{"role": "user", "content": "continue"}],
        endpoint,
        0.6,
        False,
    )
    assert capped_temperature == 0.4
    assert capped_info["temperature"] == 0.4
    assert separate_chat_temperature == 0.6
    assert separate_chat_info["temperature"] == 0.6


def test_explicit_and_remote_temperature_still_win_for_inherited_followup(
    monkeypatch, tmp_path
):
    messages = [
        {"role": "user", "content": "Fix the parser bug in src/parser.py."},
        {"role": "assistant", "content": "I found the regression."},
        {"role": "user", "content": "go ahead"},
    ]
    explicit_temperature, explicit_info = _capture_loop(
        monkeypatch, tmp_path, messages, "http://127.0.0.1:8081/v1", 0.9, True
    )
    remote_temperature, remote_info = _capture_loop(
        monkeypatch, tmp_path, messages, "https://api.example.test/v1", 0.6, False
    )
    assert explicit_temperature == 0.9
    assert explicit_info["temperature"] == 0.9
    assert explicit_info["temperature_capped_from"] is None
    assert remote_temperature == 0.6
    assert remote_info["temperature"] == 0.6
    assert remote_info["temperature_capped_from"] is None


@pytest.mark.parametrize("user", [
    "Crea un proyecto local Atlas con este objetivo, vincula estos archivos y usa solo herramientas MCP.",
    "Añade una página al menú y cambia el texto de sus opciones.",
])
def test_sampling_cap_signal_is_narrower_than_workspace_routing(user):
    # Keep the established broad routing decision for tools/retrieval while
    # requiring a separate positive technical signal for sampling.
    assert agent_loop._looks_like_workspace_coding_request(user)
    assert not agent_loop._has_concrete_programming_signal(user)


@pytest.mark.parametrize("user", [
    "Write a function parseConfig for the report.",
    "Fix function parseConfig () in the report.",
])
def test_camel_case_names_need_code_delimiters(user):
    assert not agent_loop._has_concrete_programming_signal(user)


@pytest.mark.parametrize("user", [
    "Create src/app.py implementing parse().",
    "Escribe una funcion parse en src/app.py.",
    "Implement the API endpoint in src/routes.py and add a pytest regression.",
    "RuntimeError: the parser crashes on an empty input. Fix src/parser.py.",
    "Fix the failing test in tests/test_parser.py.",
    "Arregla el código de src/app.py que falla.",
    "Revisa el codigo de la funcion parse.",
    "fix the failing test in tests/test_x.py.",
])

def test_sampling_cap_recognizes_concrete_code_and_failure_evidence(user):
    assert agent_loop._looks_like_workspace_coding_request(user)
    assert agent_loop._has_concrete_programming_signal(user)
