"""An attached gallery image routes to the image tools, not to admin tools."""
import asyncio

import pytest

from src import agent_loop as al
from src.reply_language import instruction_text_for_language, language_of

USER = ("Edita la imagen adjunta: ponle un sombrero de vaquero marrón al hombre. "
        "Conserva su cara, ropa, pose y fondo.")
CHROME = ("\n\n[Image: retrato.png]\n[No vision model configured — set one in Settings → Vision]"
          "\n\n[Gallery image ID: fd057a1e-2a69-46d9-b1ec-50a52700961c]")
VL = ("\n\n[Image: retrato.png]\nA man in a grey shirt; the settings of the photo look like a studio"
      "\n\n[Gallery image ID: fd057a1e-2a69-46d9-b1ec-50a52700961c]")


@pytest.mark.parametrize('chrome', [CHROME, VL])
def test_attachment_chrome_is_not_the_instruction(chrome):
    assert instruction_text_for_language(USER + chrome) == USER
    assert not al._detect_admin_intent([{'role': 'user', 'content': USER + chrome}])
    assert language_of(USER + chrome) == 'es'


def test_real_admin_words_still_select_admin_tools():
    assert al._detect_admin_intent([{'role': 'user', 'content': 'configura el endpoint de modelos' + CHROME}])


@pytest.mark.parametrize('text, expected', [
    (USER + CHROME, {'edit_image', 'image_job'}),
    ('Genera una imagen de un gato astronauta', {'generate_image', 'image_job'}),
    ('create an image of a red fox', {'generate_image', 'image_job'}),
    ('¿Qué resolución tiene esta imagen?', set()),
    # A reference someone typed inline is not the chat's attachment line.
    ('mi id es [Gallery image ID: abc] ¿vale?', set()),
    ('Explica esta foto' + '\n\n[Image: a.png]\nA cat creates an image of a dog', set()),
])
def test_media_hot_tools(text, expected):
    assert al._media_hot_tools(text) == expected


def test_real_loop_offers_edit_image_schema_for_attached_gallery_image(monkeypatch, caplog):
    import src.tool_index as ti
    caplog.set_level("INFO", logger="src.agent_loop")
    monkeypatch.setattr(al, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)

    class FakeIndex:
        # What the live retriever returned for this sentence.
        def get_tools_for_query(self, query, k=8, **kwargs):
            return {"inspect_image", "read_file", "write_file"}

        def index_mcp_tools(self, *a, **k):
            return None

    monkeypatch.setattr(ti, "get_tool_index", lambda: FakeIndex())
    monkeypatch.setattr(ti, "tool_rerank_options", lambda owner: {})
    offered = []

    async def provider(_candidates, messages, **kwargs):
        offered.append({(t.get("function") or {}).get("name") or t.get("name") for t in (kwargs.get("tools") or [])})
        yield 'data: {"delta":"Listo."}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", provider, raising=False)

    async def run():
        return [c async for c in al.stream_agent_loop("https://api.openai.com/v1", "gpt-test",
            [{"role": "user", "content": USER + CHROME}], max_rounds=1, relevant_tools=None)]

    asyncio.run(run())
    assert offered and {"edit_image", "image_job"} <= offered[0]
    assert not ({"manage_session", "create_session", "send_to_session", "pipeline"} & offered[0])
    debug = [r.getMessage() for r in caplog.records if "[agent-debug] round=1" in r.getMessage()]
    assert debug and "edit_image" not in debug[0].split("deferred=", 1)[-1]


def test_follow_up_about_an_earlier_attached_image_keeps_the_edit_tool():
    follow = 'Ahora edita la imagen original adjunta: ponle un bigote grande al hombre.'
    assert al._media_hot_tools(follow, [USER + CHROME]) == {'edit_image', 'image_job'}
    assert al._media_hot_tools(follow, ['hola', 'sin imagen']) == set()


def test_real_loop_follow_up_offers_edit_image(monkeypatch):
    import src.tool_index as ti
    monkeypatch.setattr(al, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)

    class FakeIndex:
        def get_tools_for_query(self, query, k=8, **kwargs):
            return {"inspect_image", "read_file"}

        def index_mcp_tools(self, *a, **k):
            return None

    monkeypatch.setattr(ti, "get_tool_index", lambda: FakeIndex())
    monkeypatch.setattr(ti, "tool_rerank_options", lambda owner: {})
    offered = []

    async def provider(_candidates, messages, **kwargs):
        offered.append({(t.get("function") or {}).get("name") or t.get("name") for t in (kwargs.get("tools") or [])})
        yield 'data: {"delta":"Listo."}\\n\\n'
        yield "data: [DONE]\\n\\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", provider, raising=False)
    history = [{"role": "user", "content": USER + CHROME},
               {"role": "assistant", "content": "Hecho."},
               {"role": "user", "content": "Ahora edita la imagen original adjunta: ponle un bigote grande."}]

    async def run():
        return [c async for c in al.stream_agent_loop("https://api.openai.com/v1", "gpt-test",
            history, max_rounds=1, relevant_tools=None)]

    asyncio.run(run())
    assert offered and {"edit_image", "image_job"} <= offered[0]
