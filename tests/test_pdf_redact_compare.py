"""Real PDF redaction (with verification) and word-level comparison."""
from __future__ import annotations

import asyncio
import json
import os

import pytest
from reportlab.pdfgen import canvas

from src import pdf_compare, pdf_ops, pdf_redact

pdfium = pytest.importorskip("pypdfium2", reason="flatten engine needs pypdfium2")


def _make_pdf(path, pages, *, rotate=0):
    c = canvas.Canvas(str(path))
    for lines in pages:
        y = 700
        for line in lines:
            c.drawString(72, y, line)
            y -= 24
        if rotate:
            c.setPageRotation(rotate)
        c.showPage()
    c.save()
    return str(path)


def _texts(path):
    import pypdf
    return [p.extract_text() or "" for p in pypdf.PdfReader(path).pages]


SECRET_PAGES = [
    ["Contact: Maria Garcia", "Email maria.garcia@example.org", "Public paragraph one"],
    ["Nothing sensitive here", "Plain public text"],
    ["ID 12345678Z and again Maria Garcia"],
]


# ---------------------------------------------------------------------------
# redact — flatten engine
# ---------------------------------------------------------------------------

def test_redact_flatten_removes_text_and_verifies(tmp_path):
    src = _make_pdf(tmp_path / "s.pdf", SECRET_PAGES)
    result = pdf_ops.redact(src, patterns=["Maria Garcia"], regex=[r"\b\d{8}[A-Z]\b"])
    assert result["verified"] is True
    assert result["engine"] == "flatten"
    assert result["flattened_pages"] == [1, 3]
    assert result["matches_removed"] == 3
    out = result["output"]
    assert out and os.path.isfile(out)
    texts = _texts(out)
    assert all("Maria" not in t and "12345678Z" not in t for t in texts)
    # An untouched page keeps its text layer.
    assert "Plain public text" in texts[1]
    assert len(texts) == 3
    # The report never echoes the removed text.
    assert "Maria" not in json.dumps(result) and "12345678" not in json.dumps(result)


def test_redact_flatten_page_is_really_an_image(tmp_path):
    src = _make_pdf(tmp_path / "s.pdf", SECRET_PAGES)
    out = pdf_ops.redact(src, patterns=["Email maria.garcia@example.org"])["output"]
    import pypdf
    page = pypdf.PdfReader(out).pages[0]
    assert "BT" not in page.get_contents().get_data().decode("latin-1")
    assert len(page.images) == 1


def test_redact_painted_area_is_black(tmp_path):
    src = _make_pdf(tmp_path / "s.pdf", [["SECRETWORD and more text"]])
    out = pdf_ops.redact(src, patterns=["SECRETWORD"])["output"]
    doc = pdfium.PdfDocument(out)
    img = doc[0].render(scale=1).to_pil().convert("L")
    # Text baseline at y=700 of 841.9 -> about 142 px from the top; x starts at 72.
    box = img.crop((72, 130, 72 + 60, 150))
    assert min(box.getdata()) == 0


def test_redact_rectangles(tmp_path):
    src = _make_pdf(tmp_path / "s.pdf", [["Top secret line", "Another line"]])
    result = pdf_ops.redact(src, rects=[{"page": 1, "rect": [60, 690, 300, 720]}])
    assert result["verified"] is True
    assert result["rectangles_applied"] == 1
    assert result["flattened_pages"] == [1]
    doc = pdfium.PdfDocument(result["output"])
    img = doc[0].render(scale=1).to_pil().convert("L")
    assert min(img.crop((80, 130, 120, 150)).getdata()) == 0


@pytest.mark.parametrize("degrees", [90, 180, 270])
def test_redact_rotated_page_box_lands_on_the_text(tmp_path, degrees):
    import pypdf
    base = _make_pdf(tmp_path / "base.pdf", [["ROTATEDSECRET text"]])
    writer = pypdf.PdfWriter(clone_from=base)
    writer.pages[0].rotate(degrees)
    src = str(tmp_path / "rot.pdf")
    with open(src, "wb") as fh:
        writer.write(fh)
    before = pdfium.PdfDocument(src)[0].render(scale=1).to_pil().convert("L")
    result = pdf_ops.redact(src, patterns=["ROTATEDSECRET"])
    assert result["verified"] is True
    assert "ROTATEDSECRET" not in "".join(_texts(result["output"]))
    after = pdfium.PdfDocument(result["output"])[0].render(scale=1).to_pil().convert("L")
    assert abs(after.size[0] - before.size[0]) <= 1 and abs(after.size[1] - before.size[1]) <= 1
    # Where the original had dark text pixels, the redacted page is solid black.
    xs = [x for x in range(before.size[0]) for y in range(0, before.size[1]) if before.getpixel((x, y)) < 100]
    ys = [y for x in range(before.size[0]) for y in range(0, before.size[1]) if before.getpixel((x, y)) < 100]
    assert xs, "the source page must show dark text pixels"
    cx, cy = (min(xs) + max(xs)) // 2, (min(ys) + max(ys)) // 2
    assert after.getpixel((cx, cy)) == 0


def test_redact_no_match_writes_nothing(tmp_path):
    src = _make_pdf(tmp_path / "s.pdf", SECRET_PAGES)
    result = pdf_ops.redact(src, patterns=["does not occur anywhere"])
    assert result["output"] is None and result["verified"] is True
    assert not os.path.exists(str(tmp_path / "s.redacted.pdf"))


def test_redact_metadata_is_scrubbed(tmp_path):
    import pypdf
    src = _make_pdf(tmp_path / "s.pdf", SECRET_PAGES)
    reader = pypdf.PdfReader(src)
    writer = pypdf.PdfWriter(clone_from=reader)
    writer.add_metadata({"/Title": "File of Maria Garcia", "/Author": "Someone"})
    tagged = str(tmp_path / "tagged.pdf")
    with open(tagged, "wb") as fh:
        writer.write(fh)
    result = pdf_ops.redact(tagged, patterns=["Maria Garcia"])
    assert result["verified"] is True
    assert result["metadata_fields_cleared"] == 1
    meta = pypdf.PdfReader(result["output"]).metadata
    assert "Maria" not in str(dict(meta))
    assert "Someone" in str(dict(meta))


def test_redact_case_sensitive(tmp_path):
    src = _make_pdf(tmp_path / "s.pdf", [["Alpha alpha ALPHA"]])
    result = pdf_ops.redact(src, patterns=["alpha"], case_sensitive=True)
    assert result["matches_removed"] == 1


# ---------------------------------------------------------------------------
# redact — stream engine (pypdf only) and verification failure path
# ---------------------------------------------------------------------------

def test_redact_stream_engine_removes_text(tmp_path):
    src = _make_pdf(tmp_path / "s.pdf", SECRET_PAGES)
    result = pdf_ops.redact(src, patterns=["Maria Garcia"], engine="stream")
    assert result["engine"] == "stream"
    assert result["verified"] is True
    assert result["rewritten_pages"] == [1, 3]
    assert all("Maria" not in t for t in _texts(result["output"]))
    assert "Plain public text" in _texts(result["output"])[1]


def test_redact_stream_rejects_rects(tmp_path):
    src = _make_pdf(tmp_path / "s.pdf", SECRET_PAGES)
    with pytest.raises(pdf_ops.PdfOpsError, match="flatten"):
        pdf_ops.redact(src, rects=[{"page": 1, "rect": [0, 0, 10, 10]}], engine="stream")


def test_redact_failed_verification_deletes_output(tmp_path, monkeypatch):
    src = _make_pdf(tmp_path / "s.pdf", SECRET_PAGES)
    # A stream engine that "forgets" to remove anything must not leave a file behind.
    monkeypatch.setattr(pdf_redact, "_redact_stream_page", lambda page, compiled: {"pattern#1": 1})
    result = pdf_ops.redact(src, patterns=["Maria Garcia"], engine="stream")
    assert result["verified"] is False
    assert result["output"] is None
    assert result["remaining"]
    assert not os.path.exists(str(tmp_path / "s.redacted.pdf"))


def test_redact_needs_pdfium_for_flatten(tmp_path, monkeypatch):
    src = _make_pdf(tmp_path / "s.pdf", SECRET_PAGES)
    monkeypatch.setattr(pdf_redact, "_have_pdfium", lambda: False)
    with pytest.raises(pdf_ops.PdfOpsError, match="pypdfium2"):
        pdf_ops.redact(src, patterns=["Maria"], engine="flatten")
    # auto mode falls back to the stream engine
    result = pdf_ops.redact(src, patterns=["Maria Garcia"])
    assert result["engine"] == "stream"


# ---------------------------------------------------------------------------
# redact — argument validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kwargs, message", [
    ({}, "nothing to redact"),
    ({"patterns": [""]}, "empty"),
    ({"regex": ["("]}, "valid regular expression"),
    ({"regex": ["a*"]}, "empty string"),
    ({"rects": [{"page": 9, "rect": [0, 0, 5, 5]}]}, "out of range"),
    ({"rects": [{"page": 1, "rect": [0, 0, 0, 5]}]}, "zero area"),
    ({"rects": "nope"}, "list"),
])
def test_redact_rejects_bad_input(tmp_path, kwargs, message):
    src = _make_pdf(tmp_path / "s.pdf", SECRET_PAGES)
    with pytest.raises(pdf_ops.PdfOpsError, match=message):
        pdf_ops.redact(src, **kwargs)


def test_redact_never_overwrites_input_by_default(tmp_path):
    src = _make_pdf(tmp_path / "s.pdf", SECRET_PAGES)
    with pytest.raises(pdf_ops.PdfOpsError, match="same as an input"):
        pdf_ops.redact(src, src, patterns=["Maria"])


def test_redact_missing_and_encrypted(tmp_path):
    with pytest.raises(pdf_ops.PdfOpsError):
        pdf_ops.redact(str(tmp_path / "nope.pdf"), patterns=["x"])
    import pypdf
    src = _make_pdf(tmp_path / "s.pdf", SECRET_PAGES)
    writer = pypdf.PdfWriter(clone_from=src)
    writer.encrypt("pw")
    enc = str(tmp_path / "enc.pdf")
    with open(enc, "wb") as fh:
        writer.write(fh)
    with pytest.raises(pdf_ops.PdfOpsError, match="encrypted"):
        pdf_ops.redact(enc, patterns=["x"])


# ---------------------------------------------------------------------------
# compare
# ---------------------------------------------------------------------------

def test_compare_reports_runs_and_summary(tmp_path):
    a = _make_pdf(tmp_path / "a.pdf", [
        ["The quick brown fox jumps over the lazy dog"],
        ["Same page text on both sides"],
    ])
    b = _make_pdf(tmp_path / "b.pdf", [
        ["The quick red fox jumps high over the lazy dog today"],
        ["Same page text on both sides"],
    ])
    result = pdf_ops.compare(a, b)
    s = result["summary"]
    assert s["pages_identical"] == 1 and s["pages_changed"] == 1
    assert s["identical"] is False
    page1 = result["pages"][0]
    kinds = {r["type"] for r in page1["runs"]}
    assert kinds == {"changed", "inserted"}
    changed = next(r for r in page1["runs"] if r["type"] == "changed")
    assert changed["a"] == "brown" and changed["b"] == "red"
    assert s["words_changed"] >= 1 and s["words_inserted"] >= 2
    assert result["pages"][1]["status"] == "identical"


def test_compare_identical(tmp_path):
    a = _make_pdf(tmp_path / "a.pdf", [["one two three"]])
    b = _make_pdf(tmp_path / "b.pdf", [["one two three"]])
    assert pdf_ops.compare(a, b)["summary"]["identical"] is True


def test_compare_different_page_counts(tmp_path):
    a = _make_pdf(tmp_path / "a.pdf", [["alpha"], ["beta"]])
    b = _make_pdf(tmp_path / "b.pdf", [["alpha"]])
    result = pdf_ops.compare(a, b)
    assert result["pages"][1]["status"] == "only_in_a"
    assert result["pages"][1]["deleted"] == 1
    assert result["summary"]["identical"] is False


def test_compare_page_without_text_is_not_identical(tmp_path):
    c = canvas.Canvas(str(tmp_path / "blank.pdf"))
    c.showPage()
    c.save()
    blank = str(tmp_path / "blank.pdf")
    result = pdf_ops.compare(blank, blank)
    assert result["pages"][0]["status"] == "no_text"
    assert result["summary"]["pages_without_text"] == 1


def test_compare_html_report_is_escaped(tmp_path):
    a = _make_pdf(tmp_path / "a.pdf", [["hello <script>alert(1)</script> world"]])
    b = _make_pdf(tmp_path / "b.pdf", [["hello world"]])
    result = pdf_ops.compare(a, b, str(tmp_path / "report.html"))
    html_text = open(result["report"], encoding="utf-8").read()
    assert "<script>" not in html_text
    assert "&lt;script&gt;" in html_text


def test_compare_run_cap():
    a = [f"a{i}" for i in range(2000)]
    b = [f"b{i}" for i in range(2000)]
    d = pdf_compare.diff_words(a[::2] + ["x"], b[::2] + ["y"])
    assert len(d["runs"]) <= pdf_compare.MAX_RUNS_PER_PAGE


def test_compare_bad_input(tmp_path):
    a = _make_pdf(tmp_path / "a.pdf", [["x"]])
    with pytest.raises(pdf_ops.PdfOpsError):
        pdf_ops.compare(a, str(tmp_path / "missing.pdf"))
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a pdf")
    with pytest.raises(pdf_ops.PdfOpsError, match="could not open"):
        pdf_ops.compare(a, str(bad))


# ---------------------------------------------------------------------------
# tool layer
# ---------------------------------------------------------------------------

def _run_tool(args):
    from src.agent_tools.pdf_ops_tool import PdfOpsTool
    return asyncio.run(PdfOpsTool().execute(json.dumps(args), {}))


def test_tool_redact_and_compare(tmp_path):
    src = _make_pdf(tmp_path / "s.pdf", SECRET_PAGES)
    res = _run_tool({"op": "redact", "input": src, "patterns": ["Maria Garcia"]})
    assert res["exit_code"] == 0 and res["verified"] is True
    cmp_res = _run_tool({"op": "compare", "input": src, "other": res["output"]})
    assert cmp_res["exit_code"] == 0
    assert cmp_res["summary"]["pages_changed"] >= 1


def test_tool_redact_unverified_is_an_error(tmp_path, monkeypatch):
    src = _make_pdf(tmp_path / "s.pdf", SECRET_PAGES)
    monkeypatch.setattr(pdf_redact, "_redact_stream_page", lambda page, compiled: {"pattern#1": 1})
    res = _run_tool({"op": "redact", "input": src, "patterns": ["Maria"], "engine": "stream"})
    assert res["exit_code"] == 1 and res["error_class"] == "pdf_ops.redact_unverified"


def test_tool_compare_requires_other(tmp_path):
    src = _make_pdf(tmp_path / "s.pdf", SECRET_PAGES)
    res = _run_tool({"op": "compare", "input": src})
    assert res["exit_code"] == 1 and "other" in res["error"]


def test_schema_lists_new_ops():
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    schema = next(t for t in FUNCTION_TOOL_SCHEMAS if t["function"]["name"] == "pdf_ops")
    enum = schema["function"]["parameters"]["properties"]["op"]["enum"]
    assert "redact" in enum and "compare" in enum
    props = schema["function"]["parameters"]["properties"]
    assert {"patterns", "regex", "rects", "other", "html_output"} <= set(props)
