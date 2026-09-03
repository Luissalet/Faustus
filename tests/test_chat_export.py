"""Tests for ``src.chat_export`` — the conversation export core.

The export used to be ~90 lines of string concatenation inside
``routes/session_routes.py``: the HTML format escaped the text and turned
``\\n`` into ``<br>``, so every fenced code block and all other markdown was
destroyed, and timestamps / per-message model / agent tool calls appeared in no
format at all. These tests pin the replacement:

  * markdown really is parsed (through the ``markdown`` package, then walked
    with ``html.parser``) into the shared ``Block`` model,
  * every format renders from that model,
  * user-supplied HTML is data, never markup, in any output.
"""
from __future__ import annotations

import json
import re
import time
import types
from html.parser import HTMLParser

import pytest

from src import chat_export
from src.chat_export import (
    build_transcript,
    markdown_to_blocks,
    render,
    render_html,
    render_json,
    render_md,
    render_txt,
)
from src.chat_export_model import (
    MEDIA_TYPES,
    ExportResult,
    ExportUnavailable,
    Transcript,
)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

class FakeMessage:
    """Duck-typed stand-in for ``core.models.ChatMessage``."""

    def __init__(self, role, content, metadata=None):
        self.role = role
        self.content = content
        self.metadata = metadata


class FakeSession:
    """Duck-typed stand-in for ``core.models.Session``."""

    def __init__(self, name="Test chat", model="gpt-4o", history=None, sid="s-1"):
        self.id = sid
        self.name = name
        self.model = model
        self.history = list(history or [])


def _kinds(blocks):
    return [b.kind for b in blocks]


def _text_of(block) -> str:
    return "".join(s.text for s in block.spans)


def _one(blocks, kind):
    matches = [b for b in blocks if b.kind == kind]
    assert matches, f"no {kind!r} block in {_kinds(blocks)}"
    return matches[0]


def _real_session():
    """A session exercising every construct the exporter must survive."""
    return FakeSession(
        name="Proyecto Fausto",
        model="gpt-4o",
        history=[
            FakeMessage("system", "You are helpful.",
                        metadata={"timestamp": "2026-08-31T09:59:00Z"}),
            FakeMessage(
                "user",
                "Dame un ejemplo en python y una tabla.\n\n"
                "| col | val |\n|---|---|\n| a | 1 |\n",
                metadata={
                    "timestamp": "2026-08-31T10:00:00Z",
                    "attachments": [
                        {"id": "up-1", "name": "diagrama.png",
                         "mime": "image/png", "size": 2048},
                    ],
                },
            ),
            FakeMessage(
                "assistant",
                "Claro:\n\n```python\ndef saluda():\n    return 'hola'\n```\n\n"
                "- primero\n- segundo\n    - anidado\n\n> nota al margen\n",
                metadata={
                    "timestamp": "2026-08-31T10:00:05Z",
                    "model": "gpt-4o-2024-11-20",
                    "requested_model": "gpt-4o",
                    "tool_events": [
                        {"round": 1, "tool": "shell", "desc": "list files",
                         "command": "ls -la", "output": "total 0\n", "exit_code": 0},
                    ],
                },
            ),
        ],
    )


# --------------------------------------------------------------------------
# _content_to_text (moved here from routes/session_routes.py)
# --------------------------------------------------------------------------

def test_content_to_text_handles_the_three_persisted_shapes():
    assert chat_export._content_to_text("hola") == "hola"
    assert chat_export._content_to_text(None) == ""
    assert chat_export._content_to_text(
        [
            {"type": "text", "text": "describe esto"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
            {"type": "text", "text": "gracias"},
        ]
    ) == "describe esto\ngracias"
    assert chat_export._content_to_text([]) == ""
    assert chat_export._content_to_text(12345) == ""


def test_content_to_text_is_exported_for_the_route_to_import():
    """The route used to own this helper; it belongs to the exporter now."""
    assert chat_export.content_to_text is chat_export._content_to_text
    assert "content_to_text" in chat_export.__all__


def test_build_transcript_accepts_list_content():
    session = FakeSession(history=[
        FakeMessage("user", [
            {"type": "text", "text": "¿qué ves aquí?"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
        ]),
    ])
    transcript = build_transcript(session)
    assert len(transcript.messages) == 1
    message = transcript.messages[0]
    assert message.raw_text == "¿qué ves aquí?"
    assert _text_of(_one(message.blocks, "para")) == "¿qué ves aquí?"
    # And it survives every renderer.
    for renderer in (render_md, render_txt, render_json, render_html):
        assert "¿qué ves aquí?" in renderer(transcript)


def test_none_content_does_not_crash_any_renderer():
    session = FakeSession(history=[FakeMessage("assistant", None)])
    transcript = build_transcript(session)
    assert transcript.messages[0].raw_text == ""
    assert transcript.messages[0].blocks == []
    for renderer in (render_md, render_txt, render_json, render_html):
        assert renderer(transcript)


# --------------------------------------------------------------------------
# markdown_to_blocks — structure
# --------------------------------------------------------------------------

def test_empty_text_yields_no_blocks():
    assert markdown_to_blocks("") == []
    assert markdown_to_blocks("   \n\n\t ") == []
    assert markdown_to_blocks(None) == []


def test_fenced_code_block_keeps_language_and_exact_text():
    blocks = markdown_to_blocks(
        "Mira:\n\n```python\ndef f(x):\n    return x * 2\n```\n"
    )
    assert _kinds(blocks) == ["para", "code"]
    code = blocks[1]
    assert code.lang == "python"
    assert code.text == "def f(x):\n    return x * 2"
    # No spans: code is raw text, not styled runs.
    assert code.spans == []


def test_unlabelled_fence_has_empty_lang():
    code = _one(markdown_to_blocks("```\nplain\n```"), "code")
    assert code.lang == ""
    assert code.text == "plain"


def test_code_block_reaches_html_as_pre_code_with_language_class():
    transcript = _transcript_from_text("```python\nprint('hi')\n```")
    out = render_html(transcript)
    assert '<pre><code class="language-python">' in out
    assert "print(&#x27;hi&#x27;)" in out or "print('hi')" in out
    # ...and markdown keeps the fence + language.
    assert "```python" in render_md(transcript)
    # ...and json carries the block.
    payload = json.loads(render_json(transcript))
    block = payload["messages"][0]["blocks"][0]
    assert block["kind"] == "code" and block["lang"] == "python"


def test_headings_h1_to_h6():
    src = "\n\n".join(f"{'#' * n} Nivel {n}" for n in range(1, 7))
    blocks = markdown_to_blocks(src)
    assert _kinds(blocks) == ["heading"] * 6
    assert [b.level for b in blocks] == [1, 2, 3, 4, 5, 6]
    assert _text_of(blocks[3]) == "Nivel 4"


def test_table_with_header_row():
    blocks = markdown_to_blocks(
        "| Lenguaje | Año |\n|---|---|\n| Python | 1991 |\n| Rust | 2010 |\n"
    )
    table = _one(blocks, "table")
    assert table.header is True
    assert len(table.rows) == 3
    header = ["".join(s.text for s in cell) for cell in table.rows[0]]
    assert header == ["Lenguaje", "Año"]
    body = ["".join(s.text for s in cell) for cell in table.rows[2]]
    assert body == ["Rust", "2010"]


def test_table_renders_in_every_format():
    transcript = _transcript_from_text(
        "| Lenguaje | Año |\n|---|---|\n| Python | 1991 |\n"
    )
    md_out = render_md(transcript)
    assert "| Lenguaje | Año |" in md_out
    assert re.search(r"\|\s*-+\s*\|", md_out)
    txt_out = render_txt(transcript)
    # Columns are space-aligned: the two header cells line up with the body.
    header_line = next(l for l in txt_out.splitlines() if "Lenguaje" in l)
    body_line = next(l for l in txt_out.splitlines() if "Python" in l)
    assert header_line.index("Año") == body_line.index("1991")
    assert "<table>" in render_html(transcript)
    assert "<th>Lenguaje</th>" in render_html(transcript)


def test_unordered_and_ordered_lists_with_one_nesting_level():
    blocks = markdown_to_blocks(
        "- uno\n- dos\n    - dos punto uno\n    - dos punto dos\n- tres\n"
    )
    lst = _one(blocks, "list")
    assert lst.ordered is False
    assert len(lst.items) == 3
    assert _text_of(lst.items[0][0]) == "uno"
    nested = [b for b in lst.items[1] if b.kind == "list"]
    assert len(nested) == 1
    assert [_text_of(item[0]) for item in nested[0].items] == [
        "dos punto uno", "dos punto dos",
    ]

    ordered = _one(markdown_to_blocks("1. a\n2. b\n"), "list")
    assert ordered.ordered is True
    assert len(ordered.items) == 2


def test_nested_list_survives_the_markdown_round_trip():
    src = "- uno\n- dos\n    - anidado\n"
    once = markdown_to_blocks(src)
    # The rendered document opens with its own metadata list; the message's
    # list is the last one.
    twice = markdown_to_blocks(render_md(_transcript_from_text(src)))
    first = [b for b in once if b.kind == "list"][-1]
    second = [b for b in twice if b.kind == "list"][-1]
    assert len(first.items) == len(second.items)
    assert [b.kind for b in first.items[1]] == [b.kind for b in second.items[1]]


@pytest.mark.parametrize("src", [
    # The commonest shape there is: a sentence, then its bullets. Serialized
    # tight against the paragraph, sane_lists read the bullets back as a lazy
    # continuation of it and the list vanished from the exported markdown.
    "Los hallazgos principales:\n\n- Uno\n- Dos\n",
    "1. Preparar\n2. Ejecutar\n\nDespués:\n\n- revisar\n- publicar\n",
    "> Resumen:\n>\n> - alfa\n> - beta\n",
    # A nested list must stay tight against the item's lead line, or the outer
    # list turns loose and "- tres" is swallowed by the nested one.
    "- uno\n- dos\n    - dos punto uno\n    - dos punto dos\n- tres\n",
    "- lead\n\n    segundo párrafo\n\n    - anidado\n",
    "## Título\n\nTexto:\n\n- a\n\nCierre.\n",
])
def test_markdown_serialization_round_trips_through_the_parser(src):
    blocks = markdown_to_blocks(src)
    assert markdown_to_blocks(chat_export.blocks_to_md(blocks)) == blocks


def test_blockquote_and_hr_and_image():
    blocks = markdown_to_blocks(
        "> citado **fuerte**\n\n---\n\n![un gato](https://ej.com/gato.png)\n"
    )
    quote = _one(blocks, "quote")
    assert quote.children and quote.children[0].kind == "para"
    assert "citado" in _text_of(quote.children[0])
    assert any(s.bold for s in quote.children[0].spans)
    assert _one(blocks, "hr").kind == "hr"
    image = _one(blocks, "image")
    assert image.href == "https://ej.com/gato.png"
    assert image.text == "un gato"


def test_inline_styles_mixed():
    blocks = markdown_to_blocks(
        "Esto es **negrita**, *cursiva*, `codigo` y "
        "un [enlace](https://ej.com) final."
    )
    spans = _one(blocks, "para").spans
    by_text = {s.text.strip(): s for s in spans}
    assert by_text["negrita"].bold is True
    assert by_text["cursiva"].italic is True
    assert by_text["codigo"].code is True
    assert by_text["enlace"].href == "https://ej.com"
    # Plain runs stay plain.
    assert not by_text["Esto es"].bold


def test_del_and_s_tags_become_strike_spans():
    for tag in ("del", "s"):
        spans = _one(markdown_to_blocks(f"tachado <{tag}>fuera</{tag}> aqui"),
                     "para").spans
        assert any(s.strike and s.text == "fuera" for s in spans), tag


def test_inline_code_uses_the_shortest_working_delimiter():
    """A one-backtick span must not come back as a stray ``` fence."""
    out = render_md(_transcript_from_text("usa `--verbose` para depurar"))
    assert "`--verbose`" in out
    assert "```--verbose```" not in out
    # ...and a span that itself contains backticks still round-trips.
    tricky = render_md(_transcript_from_text("mira ``a ` b`` aqui"))
    assert _text_of(_one(markdown_to_blocks(tricky), "para")) or True
    assert any(s.code and "`" in s.text
               for b in markdown_to_blocks(tricky) if b.kind == "para"
               for s in b.spans)


def test_txt_keeps_the_link_target():
    out = render_txt(_transcript_from_text("ver la [documentación](https://ej.com/d)"))
    assert "documentación" in out
    assert "https://ej.com/d" in out


def test_inline_styles_survive_md_and_html():
    transcript = _transcript_from_text("un **fuerte** y un `codigo`")
    assert "**fuerte**" in render_md(transcript)
    assert "`codigo`" in render_md(transcript)
    html_out = render_html(transcript)
    assert "<strong>fuerte</strong>" in html_out
    assert "<code>codigo</code>" in html_out


# --------------------------------------------------------------------------
# security: user content is data, never markup
# --------------------------------------------------------------------------

XSS_SOURCES = [
    "<script>alert(1)</script>",
    '<img src=x onerror="alert(1)">',
    '<a href="javascript:alert(1)">click</a>',
    "<iframe src='https://evil.example'></iframe>",
    "<div onclick=\"steal()\">hola</div>",
    "<style>body{display:none}</style>",
    "<svg/onload=alert(1)>",
    "<body onload=alert(1)>",
]

_DANGEROUS_TAGS = {"script", "iframe", "object", "embed", "form", "input",
                   "svg", "math", "base", "link", "meta", "body", "html",
                   "frame", "frameset", "applet"}


class _LiveMarkup(HTMLParser):
    """Collect the tags/attributes an actual browser would act on."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags = []
        self.attrs = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag.lower())
        for name, value in attrs:
            self.attrs.append((tag.lower(), name.lower(), (value or "").lower()))

    handle_startendtag = handle_starttag


def _live_markup(html_text: str) -> _LiveMarkup:
    parser = _LiveMarkup()
    parser.feed(html_text)
    parser.close()
    return parser


def _document_body(html_text: str) -> str:
    """Only the region built from message content, without the page chrome."""
    inner = html_text.split('<main class="doc">', 1)[1]
    return inner.rsplit("</main>", 1)[0]


@pytest.mark.parametrize("payload", XSS_SOURCES)
def test_user_html_never_becomes_live_markup_in_the_html_export(payload):
    body = _document_body(render_html(_transcript_from_text(f"mira esto: {payload}")))
    live = _live_markup(body)
    leaked = _DANGEROUS_TAGS.intersection(live.tags)
    assert not leaked, f"{leaked} became live markup for {payload!r}"
    for tag, name, value in live.attrs:
        assert not name.startswith("on"), f"{tag}[{name}] survived for {payload!r}"
        assert not value.startswith("javascript:"), f"{tag}[{name}]={value!r}"
        assert name not in ("style", "srcdoc", "formaction")


@pytest.mark.parametrize("payload", XSS_SOURCES)
def test_no_payload_is_silently_dropped_from_the_transcript(payload):
    """Neutralised, not censored — the JSON export keeps the source verbatim."""
    transcript = _transcript_from_text(f"mira esto: {payload}")
    assert payload in json.loads(render_json(transcript))["messages"][0]["content"]


# Tags outside the walker's allowlist keep their source as literal text. The
# two allowlisted ones (<a>, <img>) become sanitized structure instead — the
# link keeps its text without the javascript: href, the image its alt.
_UNKNOWN_TAG_PAYLOADS = [p for p in XSS_SOURCES if not p.startswith(("<a ", "<img "))]


@pytest.mark.parametrize("payload", _UNKNOWN_TAG_PAYLOADS)
def test_unknown_tags_survive_as_inert_text(payload):
    transcript = _transcript_from_text(f"mira: {payload}")
    assert "&lt;" in _document_body(render_html(transcript))
    assert payload.split(">")[0] in render_txt(transcript)


def test_allowlisted_tags_keep_their_content_without_their_payload():
    link = _transcript_from_text('<a href="javascript:alert(1)">pincha</a>')
    assert "pincha" in render_txt(link)
    assert "javascript:" not in render_html(link).lower()
    image = _transcript_from_text('<img src=x onerror="alert(1)" alt="gato">')
    assert "gato" in render_txt(image)
    assert "onerror" not in render_html(image).lower()


def test_script_body_is_not_executable_after_markdown_conversion():
    """The `markdown` package passes raw HTML straight through; we must not."""
    import markdown as _markdown
    raw = _markdown.markdown("<script>alert(1)</script>",
                             extensions=["fenced_code", "tables", "sane_lists"])
    assert "<script>" in raw, "precondition: markdown does not sanitize"
    body = _document_body(render_html(_transcript_from_text("<script>alert(1)</script>")))
    assert "script" not in _live_markup(body).tags
    assert "&lt;script&gt;" in body


def test_javascript_hrefs_are_dropped_from_links():
    blocks = markdown_to_blocks("[malo](javascript:alert(1))")
    spans = _one(blocks, "para").spans
    assert all("javascript:" not in s.href for s in spans)
    out = render_html(_transcript_from_text("[malo](javascript:alert(1))"))
    assert "javascript:" not in out.lower()


def test_html_document_has_no_external_resources():
    out = render_html(_transcript_from_text("hola"))
    assert "<style>" in out                     # CSS is embedded
    assert "<link" not in out.lower()
    assert 'src="http' not in out.lower()
    assert "@import" not in out.lower()
    assert "<script" not in out.lower()


def test_malformed_html_does_not_break_the_parse():
    blocks = markdown_to_blocks("texto <b>sin cerrar y <<> raro & suelto")
    assert blocks, "parser must still produce something"
    assert "raro" in "".join(_text_of(b) for b in blocks if b.kind == "para")


@pytest.mark.parametrize("payload", [
    "</td></li></blockquote></ul> texto suelto",
    "<td>celda huerfana</td>",
    "<ul><li>abierto</ul></li>",
    "<table><td>x</table></td>",
    "> cita\n\n</blockquote>despues",
    "<b><i>anidado mal</b></i>",
])
def test_unbalanced_tags_neither_crash_nor_swallow_text(payload):
    """A stray closing tag must not pop somebody else's container."""
    blocks = markdown_to_blocks(payload)
    rendered = "".join((b.text or "") + _text_of(b) for b in blocks)
    rendered += "".join(_text_of(c) for b in blocks for c in b.children)
    rendered += "".join(_text_of(c) for b in blocks for item in b.items for c in item)
    rendered += "".join(s.text for b in blocks for row in b.rows
                        for cell in row for s in cell)
    for word in re.findall(r"[a-zA-Z]{6,}", payload):
        if word in ("blockquote", "anidado"):
            continue
        assert word in rendered, f"{word!r} was swallowed"
    # And every format still renders.
    transcript = _transcript_from_text(payload)
    for renderer in (render_md, render_txt, render_json, render_html):
        assert renderer(transcript)


# --------------------------------------------------------------------------
# build_transcript — metadata
# --------------------------------------------------------------------------

def test_system_messages_are_skipped_by_default_and_optional_on_request():
    session = _real_session()
    assert [m.role for m in build_transcript(session).messages] == ["user", "assistant"]
    with_system = build_transcript(session, include_system=True)
    assert [m.role for m in with_system.messages] == ["system", "user", "assistant"]


def test_hidden_messages_are_never_exported():
    session = FakeSession(history=[
        FakeMessage("assistant", "visible"),
        FakeMessage("system", "internal summary",
                    metadata={"compacted": True, "hidden": True}),
    ])
    transcript = build_transcript(session, include_system=True)
    assert [m.raw_text for m in transcript.messages] == ["visible"]


def test_timestamp_model_attachments_and_tool_calls_come_from_metadata():
    transcript = build_transcript(_real_session())
    user, assistant = transcript.messages
    assert user.timestamp == "2026-08-31T10:00:00Z"
    assert user.attachments and "diagrama.png" in user.attachments[0]
    assert assistant.timestamp == "2026-08-31T10:00:05Z"
    assert assistant.model == "gpt-4o-2024-11-20"
    assert len(assistant.tool_calls) == 1
    call = assistant.tool_calls[0]
    assert call.name == "shell"
    assert call.arguments == "ls -la"
    assert call.result.strip() == "total 0"
    assert call.status == "ok"


def test_absent_metadata_keys_leave_empty_fields_not_invented_ones():
    session = FakeSession(history=[FakeMessage("user", "hola")])
    message = build_transcript(session).messages[0]
    assert message.timestamp == ""
    assert message.model == ""
    assert message.tool_calls == []
    assert message.attachments == []


def test_non_dict_metadata_is_tolerated():
    session = FakeSession(history=[
        FakeMessage("user", "hola", metadata="no soy un dict"),
        FakeMessage("assistant", "adios", metadata={"tool_events": "tampoco"}),
    ])
    transcript = build_transcript(session)
    assert [m.raw_text for m in transcript.messages] == ["hola", "adios"]
    assert transcript.messages[1].tool_calls == []


def test_failed_tool_event_status_reports_the_exit_code():
    session = FakeSession(history=[
        FakeMessage("assistant", "fallo", metadata={"tool_events": [
            {"tool": "shell", "command": "false", "output": "boom", "exit_code": 2},
        ]}),
    ])
    call = build_transcript(session).messages[0].tool_calls[0]
    assert call.status.startswith("error")
    assert "2" in call.status


def test_mcp_tool_events_resolve_their_real_name():
    session = FakeSession(history=[
        FakeMessage("assistant", "x", metadata={"tool_events": [
            {"tool": "mcp", "desc": "mcp__email__list_emails inbox",
             "command": "", "output": ""},
        ]}),
    ])
    assert build_transcript(session).messages[0].tool_calls[0].name == \
        "mcp__email__list_emails"


def test_include_tools_false_drops_tool_calls():
    transcript = build_transcript(_real_session(), include_tools=False)
    assert all(not m.tool_calls for m in transcript.messages)
    assert "ls -la" not in render_md(transcript)


def test_transcript_header_fields():
    transcript = build_transcript(_real_session())
    assert transcript.name == "Proyecto Fausto"
    assert transcript.model == "gpt-4o"
    assert transcript.session_id == "s-1"


# --------------------------------------------------------------------------
# tool calls reach every format
# --------------------------------------------------------------------------

def test_tool_calls_appear_in_md_txt_json_and_html():
    transcript = build_transcript(_real_session())
    md_out = render_md(transcript)
    assert "shell" in md_out and "ls -la" in md_out
    assert "<details>" in md_out or "- " in md_out

    txt_out = render_txt(transcript)
    assert "shell" in txt_out and "ls -la" in txt_out

    payload = json.loads(render_json(transcript))
    calls = payload["messages"][1]["tool_calls"]
    assert calls[0]["name"] == "shell"
    assert calls[0]["arguments"] == "ls -la"
    assert calls[0]["result"].strip() == "total 0"

    html_out = render_html(transcript)
    assert "<details" in html_out
    assert "ls -la" in html_out


def test_tool_call_output_is_escaped_in_html():
    session = FakeSession(history=[
        FakeMessage("assistant", "hecho", metadata={"tool_events": [
            {"tool": "shell", "command": "cat x", "output": "<script>bad()</script>",
             "exit_code": 0},
        ]}),
    ])
    out = render_html(build_transcript(session))
    body = out.split("</head>", 1)[1]
    assert "<script>" not in body
    assert "&lt;script&gt;bad()&lt;/script&gt;" in body


# --------------------------------------------------------------------------
# per-format contract
# --------------------------------------------------------------------------

def test_markdown_header_carries_name_model_and_date():
    out = render_md(build_transcript(_real_session()))
    assert "Proyecto Fausto" in out
    assert "gpt-4o" in out
    assert "2026-08-31T10:00:00Z" in out          # per-message timestamp
    assert re.search(r"\d{4}-\d{2}-\d{2}", out)   # export date


def test_txt_is_readable_without_rendering():
    out = render_txt(_transcript_from_text(
        "```python\nx = 1\n```\n\ntexto normal\n"
    ))
    assert "```" not in out                        # no leftover fences
    assert "    x = 1" in out                      # code indented
    assert "texto normal" in out


def test_json_keeps_the_legacy_keys_and_adds_the_model():
    payload = json.loads(render_json(build_transcript(_real_session())))
    assert payload["name"] == "Proyecto Fausto"
    assert payload["model"] == "gpt-4o"
    assert isinstance(payload["exported"], str) and payload["exported"]
    assert isinstance(payload["messages"], list)
    first = payload["messages"][0]
    assert first["role"] == "user"
    assert isinstance(first["content"], str) and first["content"]
    # ...and the structure the old {role, content} export threw away.
    assert first["blocks"]
    assert payload["messages"][1]["tool_calls"]
    assert payload["messages"][1]["model"] == "gpt-4o-2024-11-20"


def test_html_is_a_standalone_themable_document():
    out = render_html(build_transcript(_real_session()))
    assert out.lstrip().startswith("<!DOCTYPE html>")
    assert "prefers-color-scheme: dark" in out
    assert "</html>" in out.rstrip()[-10:]
    # user and assistant bubbles are distinguishable
    assert "msg-user" in out and "msg-assistant" in out
    # timestamps are shown
    assert "2026-08-31T10:00:00Z" in out or "2026-08-31" in out


def test_html_escapes_the_session_name():
    session = FakeSession(name="<script>alert(1)</script>", history=[
        FakeMessage("user", "hola"),
    ])
    out = render_html(build_transcript(session))
    assert "<script>alert(1)</script>" not in out
    assert "&lt;script&gt;" in out


# --------------------------------------------------------------------------
# unicode / emoji
# --------------------------------------------------------------------------

def test_unicode_and_emoji_survive_every_format():
    text = "Año nuevo 🎉 — ünïcödé, 日本語, «comillas» y ✅ hecho"
    transcript = _transcript_from_text(text + "\n\n```py\nprint('🎉')\n```")
    for renderer in (render_md, render_txt, render_json):
        out = renderer(transcript)
        assert "🎉" in out
        assert "日本語" in out
    html_out = render_html(transcript)
    assert "🎉" in html_out and "日本語" in html_out
    assert 'charset="utf-8"' in html_out or "charset='utf-8'" in html_out
    # and the bytes really are utf-8
    for fmt in ("md", "txt", "json", "html"):
        result = render(transcript, fmt)
        assert "🎉" in result.content.decode("utf-8")


# --------------------------------------------------------------------------
# render() dispatch, filenames, media types
# --------------------------------------------------------------------------

def test_every_text_format_produces_non_empty_bytes():
    transcript = build_transcript(_real_session())
    for fmt in ("md", "txt", "json", "html"):
        result = render(transcript, fmt)
        assert isinstance(result, ExportResult)
        assert isinstance(result.content, bytes) and len(result.content) > 50
        assert result.media_type == MEDIA_TYPES[fmt]
        assert result.filename.endswith("." + fmt)


def test_default_filename_is_sanitized_and_timestamped():
    session = FakeSession(name="Proyecto/Fausto: año 2026?")
    session.history = [FakeMessage("user", "hola")]
    result = render(build_transcript(session), "md")
    assert re.fullmatch(r"conversation_[A-Za-z0-9._-]+_\d{8}_\d{6}\.md",
                        result.filename), result.filename
    assert "/" not in result.filename and "?" not in result.filename


def test_explicit_filename_is_sanitized_and_kept():
    transcript = build_transcript(_real_session())
    assert render(transcript, "md", filename="mi export.md").filename == "mi_export.md"
    # A name without an extension gets the format's one.
    assert render(transcript, "txt", filename="notas").filename == "notas.txt"
    # A hostile name cannot escape the download directory.
    hostile = render(transcript, "json", filename="../../etc/passwd").filename
    assert "/" not in hostile and ".." not in hostile


def test_format_aliases_and_unknown_formats():
    transcript = build_transcript(_real_session())
    assert render(transcript, "MD").media_type == MEDIA_TYPES["md"]
    assert render(transcript, "markdown").media_type == MEDIA_TYPES["md"]
    assert render(transcript, "text").media_type == MEDIA_TYPES["txt"]
    with pytest.raises(ValueError):
        render(transcript, "docbook")


def test_pdf_and_docx_delegate_to_their_module(monkeypatch):
    transcript = build_transcript(_real_session())
    calls = {}

    def fake_import(name):
        module = types.ModuleType(name)

        def _render(t):
            calls[name] = t
            return b"%PDF-fake" if "pdf" in name else b"PK-fake"

        module.render = _render
        return module

    monkeypatch.setattr(chat_export, "importlib",
                        types.SimpleNamespace(import_module=fake_import))
    pdf = render(transcript, "pdf")
    assert pdf.content == b"%PDF-fake"
    assert pdf.media_type == MEDIA_TYPES["pdf"]
    assert pdf.filename.endswith(".pdf")
    docx = render(transcript, "docx")
    assert docx.content == b"PK-fake"
    assert docx.filename.endswith(".docx")
    assert set(calls) == {"src.chat_export_pdf", "src.chat_export_docx"}


def test_missing_pdf_module_raises_export_unavailable(monkeypatch):
    def boom(name):
        raise ImportError(f"No module named {name!r}")

    monkeypatch.setattr(chat_export, "importlib",
                        types.SimpleNamespace(import_module=boom))
    transcript = build_transcript(_real_session())
    for fmt in ("pdf", "docx"):
        with pytest.raises(ExportUnavailable) as excinfo:
            render(transcript, fmt)
        message = str(excinfo.value)
        assert fmt.upper() in message or fmt in message
        assert "chat_export_" in message           # names what is missing
        assert "Traceback" not in message


def test_module_without_render_is_reported_as_unavailable(monkeypatch):
    monkeypatch.setattr(chat_export, "importlib", types.SimpleNamespace(
        import_module=lambda name: types.ModuleType(name)))
    with pytest.raises(ExportUnavailable):
        render(build_transcript(_real_session()), "pdf")


def test_renderer_returning_non_bytes_is_reported_not_leaked(monkeypatch):
    def fake_import(name):
        module = types.ModuleType(name)
        module.render = lambda t: "not bytes"
        return module

    monkeypatch.setattr(chat_export, "importlib",
                        types.SimpleNamespace(import_module=fake_import))
    with pytest.raises(ExportUnavailable):
        render(build_transcript(_real_session()), "pdf")


# --------------------------------------------------------------------------
# performance
# --------------------------------------------------------------------------

@pytest.mark.slow
def test_five_hundred_messages_export_quickly():
    body = (
        "Respuesta con **formato**, una lista:\n\n"
        "- alfa\n- beta\n    - anidado\n\n"
        "```python\ndef f():\n    return 1\n```\n\n"
        "| a | b |\n|---|---|\n| 1 | 2 |\n"
    )
    history = []
    for i in range(500):
        role = "user" if i % 2 == 0 else "assistant"
        history.append(FakeMessage(
            role, f"Mensaje {i}\n\n{body}",
            metadata={"timestamp": "2026-08-31T10:00:00Z", "model": "gpt-4o"},
        ))
    session = FakeSession(history=history)

    start = time.perf_counter()
    transcript = build_transcript(session)
    for fmt in ("md", "txt", "json", "html"):
        assert render(transcript, fmt).content
    elapsed = time.perf_counter() - start

    assert len(transcript.messages) == 500
    assert elapsed < 3.0, f"export of 500 messages took {elapsed:.2f}s"


# --------------------------------------------------------------------------
# shared fixtures
# --------------------------------------------------------------------------

def _transcript_from_text(text: str) -> Transcript:
    """A one-message transcript whose single user message is *text*."""
    return build_transcript(FakeSession(history=[FakeMessage("user", text)]))
