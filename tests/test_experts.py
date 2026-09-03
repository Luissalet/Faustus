"""Specialist experts — a local agent with its own corpus (services/experts.py)
and its HTTP API (routes/expert_routes.py).

The points being tested throughout:

* a **page number is never invented** — a PDF that can be paged carries its
  real page into every chunk, and one that cannot carries ``page: None`` plus
  ``page_confidence: "unknown"``, which is what makes a citation checkable;
* **search degrades, it never errors** — with no vector store the answer is
  tier 1 with ``degraded: True``, and with one the two rankings fuse by
  Reciprocal Rank Fusion;
* the context block is **deterministic, budgeted, and its ``[C1]`` markers map
  one-to-one onto ``chunk_ids``** — the review pipeline parses exactly that;
* **nothing raises into a hot path**: a missing directory, a corrupt
  EXPERT.md, a corrupt index or a vector store that blows up cost the block,
  not the turn.
"""

import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import experts  # noqa: E402
from src import memory_engine  # noqa: E402
from src.personal_docs import config as docs_config, split_chunks  # noqa: E402


# ── fixtures ────────────────────────────────────────────────────────────────


class FakeVectors:
    """A healthy vector store that answers with a fixed ranking."""

    healthy = True

    def __init__(self, order=()):
        self.order = list(order)
        self.added = []
        self.removed = []

    def add(self, chunk_id, text):
        self.added.append(chunk_id)

    def remove(self, chunk_id):
        self.removed.append(chunk_id)

    def count(self):
        return len([c for c in self.added if c not in self.removed])

    def search(self, query, k=8):
        return [{"memory_id": cid, "score": 1.0 - i * 0.01}
                for i, cid in enumerate(self.order[:k])]


class ExplodingVectors(FakeVectors):
    """Healthy on paper, raises when asked — the worst case for a hot path."""

    def search(self, query, k=8):
        raise RuntimeError("chromadb went away mid-query")


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """A disposable experts root with the semantic lane explicitly absent.

    ``lanes`` is the per-slug vector store the test controls; leaving a slug
    out of it is exactly "this machine has downloaded nothing".
    """
    monkeypatch.setattr(experts, "DATA_DIR", str(tmp_path))
    # expert_block packs the expert's own learned rules; keep that engine's
    # database disposable too so the suite never touches the real one.
    monkeypatch.setattr(memory_engine, "DATA_DIR", str(tmp_path))
    memory_engine.set_vector_store(None)
    experts.reset_vector_stores()
    lanes = {}
    monkeypatch.setattr(experts, "vector_store",
                        lambda slug: lanes.get(experts._clean_slug(slug)))
    yield SimpleNamespace(root=tmp_path, lanes=lanes)
    experts.reset_vector_stores()
    memory_engine.reset_vector_store()


def _corrector(**kw):
    kw.setdefault("description", "Revisa ritmo, diálogo y punto de vista")
    kw.setdefault("instructions",
                  "Eres un corrector narrativo. NO toques la voz del autor "
                  "ni reescribas el argumento.")
    kw.setdefault("rubric", ["Ritmo de escena", "Diálogo", "Coherencia de POV"])
    return experts.create_expert("Corrector narrativo", **kw)


def _write_pdf(path, pages):
    """A real PDF with one line per page (reportlab already ships with the app)."""
    from reportlab.pdfgen import canvas
    doc = canvas.Canvas(str(path))
    for i, text in enumerate(pages, start=1):
        doc.drawString(72, 720, f"Page {i}: {text}")
        doc.showPage()
    doc.save()


# ── the profile ─────────────────────────────────────────────────────────────


def test_profile_roundtrips_through_expert_md(store):
    made = _corrector(model="qwen2.5:14b", temperature=0.15, top_p=0.9, owner="luis")
    assert made["slug"] == "corrector-narrativo"

    raw = open(experts.profile_path(made["slug"]), encoding="utf-8").read()
    assert raw.startswith("---\n") and "## Rubric" in raw
    assert "NO toques la voz del autor" in raw

    loaded = experts.load_expert(made["slug"])
    assert loaded == made
    assert loaded["rubric"] == ["Ritmo de escena", "Diálogo", "Coherencia de POV"]
    assert loaded["temperature"] == 0.15 and loaded["top_p"] == 0.9
    assert loaded["enabled"] is True and loaded["owner"] == "luis"
    # The corpus folder exists from creation, so a drop-in works immediately.
    assert os.path.isdir(experts.corpus_dir(made["slug"]))


def test_profile_updates_and_deletes(store):
    made = _corrector()
    slug = made["slug"]

    patched = experts.update_expert(slug, {
        "description": "Solo ritmo",
        "rubric": "Ritmo\nDiálogo",
        "enabled": False,
        "temperature": 0.05,
    })
    assert patched["description"] == "Solo ritmo"
    assert patched["rubric"] == ["Ritmo", "Diálogo"]
    # `enabled: false` must survive the frontmatter round-trip.
    assert experts.load_expert(slug)["enabled"] is False
    assert experts.load_expert(slug)["temperature"] == 0.05
    # The slug never moves, so a rename cannot orphan the corpus.
    assert experts.update_expert(slug, {"name": "Otro nombre"})["slug"] == slug

    with pytest.raises(experts.ExpertError):
        experts.update_expert(slug, {"name": "   "})
    assert experts.update_expert("nope", {"name": "x"}) is None

    assert experts.delete_expert(slug) is True
    assert experts.load_expert(slug) is None
    assert experts.delete_expert(slug) is False


def test_slug_collisions_never_clobber_a_corpus(store):
    first = _corrector()
    second = _corrector()
    third = experts.create_expert("corrector NARRATIVO")
    assert [first["slug"], second["slug"], third["slug"]] == [
        "corrector-narrativo", "corrector-narrativo-2", "corrector-narrativo-3"]
    assert sorted(experts.list_expert_slugs()) == sorted(
        [first["slug"], second["slug"], third["slug"]])
    with pytest.raises(experts.ExpertError):
        experts.create_expert("   ")


def test_a_corrupt_expert_md_is_moved_aside_and_rebuilt(store):
    made = _corrector()
    slug = made["slug"]
    open(os.path.join(experts.corpus_dir(slug), "book.md"), "w").write("rhythm matters")
    experts.reindex(slug)

    with open(experts.profile_path(slug), "w", encoding="utf-8") as fh:
        fh.write("\x00\x00 this is not a markdown profile at all")

    recovered = experts.load_expert(slug)
    assert recovered is not None and recovered["slug"] == slug
    assert recovered["rubric"] == [] and recovered["instructions"] == ""
    # The broken copy is kept, and the expensive part — the corpus and its
    # index — survived untouched.
    assert os.path.isfile(experts.profile_path(slug) + ".corrupt")
    assert len(experts.load_index(slug)) >= 1
    # And the rebuilt file is itself loadable.
    assert experts.load_expert(slug) == recovered


def test_owner_filtering_keeps_ownerless_experts_visible(store):
    experts.create_expert("Mine", owner="luis")
    experts.create_expert("Theirs", owner="someone-else")
    experts.create_expert("Unstamped")
    slugs = [e["slug"] for e in experts.list_experts("luis")]
    assert slugs == ["mine", "unstamped"]
    assert len(experts.list_experts()) == 3


# ── ingestion with page-level provenance ────────────────────────────────────


def test_pdf_page_numbers_survive_into_the_index(store, tmp_path):
    made = _corrector()
    slug = made["slug"]
    pdf = tmp_path / "manual.pdf"
    _write_pdf(pdf, ["dialogue rhythm carries the scene",
                     "pacing tightens when the verbs shorten",
                     "point of view must not drift midscene"])

    result = experts.ingest(slug, str(pdf))
    assert result["indexed"] == 1 and result["chunks"] == 3
    assert result["file"] == "manual.pdf"

    index = experts.load_index(slug)
    assert [c["page"] for c in index] == [1, 2, 3]
    assert {c["page_confidence"] for c in index} == {"exact"}
    assert all(c["source"] == "manual.pdf" for c in index)
    assert all(c["tokens"] > 0 for c in index)
    assert "pacing" in [c for c in index if c["page"] == 2][0]["text"]
    # Ingest COPIES into the corpus; the original is untouched and nothing
    # left the machine.
    assert os.path.isfile(os.path.join(experts.corpus_dir(slug), "manual.pdf"))
    assert pdf.exists()


def test_a_document_whose_pages_are_unknown_gets_null_never_a_guess(store, monkeypatch):
    made = _corrector()
    slug = made["slug"]
    open(os.path.join(experts.corpus_dir(slug), "scan.pdf"), "wb").write(b"%PDF-1.4 junk")

    # The library could not page it: one whole-document unit, pages_known False.
    monkeypatch.setattr(experts, "extract_pdf_pages",
                        lambda path: ([(None, "line one\nline two about rhythm\n")], False))
    experts.reindex(slug)

    index = experts.load_index(slug)
    assert len(index) == 1
    assert index[0]["page"] is None
    assert index[0]["page_confidence"] == "unknown"
    # Line ranges are still real, so the citation stays checkable.
    assert index[0]["start_line"] == 1 and index[0]["end_line"] >= 1
    assert experts.citation(slug, index[0]["id"])["page"] is None


def test_extract_pdf_pages_never_raises_on_a_broken_file(store, tmp_path):
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"not a pdf at all")
    pages, known = experts.extract_pdf_pages(str(broken))
    assert pages == [] or all(p is None for p, _ in pages)
    assert known in (True, False)


def test_text_files_are_chunked_with_line_ranges(store):
    made = _corrector()
    slug = made["slug"]
    body = "\n".join(f"line {i} about dialogue and rhythm" for i in range(1, 200))
    open(os.path.join(experts.corpus_dir(slug), "notes.md"), "w").write(body)
    experts.reindex(slug)

    index = experts.load_index(slug)
    assert len(index) > 1
    assert all(c["page"] is None and c["page_confidence"] == "none" for c in index)
    assert index[0]["start_line"] == 1
    for chunk in index:
        assert 1 <= chunk["start_line"] <= chunk["end_line"] <= body.count("\n") + 1
    assert index[-1]["end_line"] == body.count("\n") + 1


def test_chunk_spans_stay_in_step_with_split_chunks(store):
    """The offsets are what turn a chunk into a line range, so they must cut
    exactly where personal_docs.split_chunks cuts."""
    size, overlap = docs_config.CHUNK_SIZE, docs_config.CHUNK_OVERLAP
    for text in ("", "   ", "short", "\n\n  " + ("abcdefghij " * 400) + "  \n",
                 "x" * (size * 3 + 7)):
        spans = experts._chunk_spans(text, size, overlap)
        assert [text[a:b] for a, b in spans] == split_chunks(text, size, overlap)


def test_unsupported_and_unsafe_filenames_are_refused(store):
    made = _corrector()
    slug = made["slug"]
    for bad in ("evil.exe", "../../etc/passwd", "", "..", "notes"):
        with pytest.raises(experts.ExpertError):
            experts.corpus_target_path(slug, bad)
    # A legitimate name is de-duplicated rather than overwritten.
    first = experts.corpus_target_path(slug, "book.pdf")
    open(first, "w").write("x")
    assert os.path.basename(experts.corpus_target_path(slug, "book.pdf")) == "book-2.pdf"


# ── incremental reindex ─────────────────────────────────────────────────────


def test_reindex_is_incremental_by_mtime_and_size(store, tmp_path):
    made = _corrector()
    slug = made["slug"]
    corpus = experts.corpus_dir(slug)
    a = os.path.join(corpus, "a.md")
    b = os.path.join(corpus, "b.md")
    open(a, "w").write("alpha rhythm " * 40)
    open(b, "w").write("beta pacing " * 40)

    first = experts.reindex(slug)
    assert first["indexed"] == 2 and first["skipped"] == 0 and first["removed"] == 0
    chunks_before = {c["id"] for c in experts.load_index(slug)}

    # Nothing changed: both files skipped, the ids are the same ones.
    second = experts.reindex(slug)
    assert second["indexed"] == 0 and second["skipped"] == 2 and second["removed"] == 0
    assert {c["id"] for c in experts.load_index(slug)} == chunks_before

    # An edited file is re-chunked (mtime is bumped explicitly so the test does
    # not depend on the filesystem's timestamp resolution).
    open(a, "w").write("alpha rhythm rewritten with much more about dialogue " * 40)
    os.utime(a, (0, 0))
    third = experts.reindex(slug)
    assert third["indexed"] == 1 and third["skipped"] == 1
    assert any("rewritten" in c["text"] for c in experts.load_index(slug))

    # A removed file drops its chunks and nothing else.
    os.remove(b)
    fourth = experts.reindex(slug)
    assert fourth["removed"] == 1 and fourth["indexed"] == 0 and fourth["skipped"] == 1
    sources = {c["source"] for c in experts.load_index(slug)}
    assert sources == {"a.md"}
    assert fourth["chunks"] == len(experts.load_index(slug))
    assert fourth["seconds"] >= 0


def test_reindex_pushes_and_evicts_vectors_when_a_lane_exists(store):
    made = _corrector()
    slug = made["slug"]
    lane = FakeVectors()
    store.lanes[slug] = lane
    path = os.path.join(experts.corpus_dir(slug), "a.md")
    open(path, "w").write("alpha rhythm " * 40)
    experts.reindex(slug)
    assert lane.added and set(lane.added) == {c["id"] for c in experts.load_index(slug)}

    gone = list(lane.added)
    os.remove(path)
    experts.reindex(slug)
    assert set(lane.removed) == set(gone)


def test_a_lane_that_appears_later_back_fills_itself(store):
    """ChromaDB may only come up after the corpus was indexed; the next
    reindex must not leave it blind to files it never saw."""
    made = _corrector()
    slug = made["slug"]
    open(os.path.join(experts.corpus_dir(slug), "a.md"), "w").write("alpha rhythm " * 40)
    experts.reindex(slug)                      # no lane at all yet
    assert experts.load_index(slug)

    lane = FakeVectors()
    store.lanes[slug] = lane
    experts.reindex(slug)                      # every file is "skipped" now
    assert set(lane.added) == {c["id"] for c in experts.load_index(slug)}

    # And once it is populated, an unchanged reindex re-embeds nothing.
    before = len(lane.added)
    experts.reindex(slug)
    assert len(lane.added) == before


def test_reindex_skips_a_file_it_cannot_extract(store, monkeypatch):
    made = _corrector()
    slug = made["slug"]
    open(os.path.join(experts.corpus_dir(slug), "good.md"), "w").write("rhythm " * 40)
    open(os.path.join(experts.corpus_dir(slug), "bad.md"), "w").write("pacing " * 40)

    real = experts._extract_units

    def flaky(path):
        if path.endswith("bad.md"):
            raise OSError("device fell off the bus")
        return real(path)

    monkeypatch.setattr(experts, "_extract_units", flaky)
    result = experts.reindex(slug)
    assert result["indexed"] == 1 and result["skipped"] == 1
    assert {c["source"] for c in experts.load_index(slug)} == {"good.md"}


def test_a_corrupt_index_is_moved_aside_and_rebuilt(store):
    made = _corrector()
    slug = made["slug"]
    open(os.path.join(experts.corpus_dir(slug), "a.md"), "w").write("rhythm " * 40)
    experts.reindex(slug)

    with open(experts.index_path(slug), "w", encoding="utf-8") as fh:
        fh.write("{ not json at all")
    assert experts.load_index(slug) == []
    assert os.path.isfile(experts.index_path(slug) + ".corrupt")
    # The next reindex simply builds it again from the corpus that is still there.
    assert experts.reindex(slug)["chunks"] > 0


# ── two-tier search ─────────────────────────────────────────────────────────


@pytest.fixture()
def indexed(store, tmp_path):
    made = _corrector()
    slug = made["slug"]
    _write_pdf(tmp_path / "manual.pdf",
               ["dialogue rhythm carries the scene",
                "pacing tightens when the verbs shorten",
                "point of view must not drift midscene"])
    experts.ingest(slug, str(tmp_path / "manual.pdf"))
    open(os.path.join(experts.corpus_dir(slug), "notes.md"), "w").write(
        "Rhythm is the first thing a reader feels.\nDialogue must carry weight.\n")
    experts.reindex(slug)
    return SimpleNamespace(slug=slug, lanes=store.lanes, root=store.root)


def test_tier_one_still_answers_with_no_vector_store(indexed):
    result = experts.search(indexed.slug, "pacing verbs", k=3)
    assert result["tier"] == "lexical"
    # The lane is missing and the caller can SEE it — this is a degradation,
    # never an error.
    assert result["degraded"] is True
    assert result["hits"], "a Faustus that has downloaded nothing still searches"
    top = result["hits"][0]
    assert top["source"] == "manual.pdf" and top["page"] == 2
    assert set(top) == {"chunk_id", "source", "page", "start_line", "end_line",
                        "text", "score", "tier"}
    assert top["tier"] == "lexical" and top["score"] > 0


def test_hybrid_fuses_the_two_rankings_by_rrf(indexed):
    index = experts.load_index(indexed.slug)
    by_page = {c["page"]: c["id"] for c in index if c["source"] == "manual.pdf"}
    # The semantic lane likes page 3, which the lexical lane does not rank at
    # all for this query: RRF must pull it up without unseating the exact hit.
    indexed.lanes[indexed.slug] = FakeVectors([by_page[3], by_page[2], by_page[1]])

    result = experts.search(indexed.slug, "pacing verbs", k=3)
    assert result["tier"] == "hybrid" and result["degraded"] is False
    order = [h["page"] for h in result["hits"]]
    assert order[0] == 2, "the lexical hit is in both lists, so it stays first"
    assert 3 in order, "the semantic-only hit is fused in, not dropped"
    assert all(h["tier"] == "hybrid" for h in result["hits"])

    # The score IS the RRF sum: page 2 is lexical rank 1 and semantic rank 2.
    expected = 1.0 / (experts.RRF_K + 1) + 1.0 / (experts.RRF_K + 2)
    assert result["hits"][0]["score"] == pytest.approx(expected, abs=1e-6)


def test_a_vector_store_that_raises_degrades_instead_of_failing(indexed):
    indexed.lanes[indexed.slug] = ExplodingVectors()
    result = experts.search(indexed.slug, "pacing verbs", k=3)
    assert result["tier"] == "lexical" and result["degraded"] is True
    assert result["hits"]


def test_search_is_defensive_about_everything_else(store, indexed):
    assert experts.search("no-such-expert", "anything") == {
        "hits": [], "tier": "lexical", "degraded": False}
    assert experts.search(indexed.slug, "")["hits"] == []
    assert experts.search(indexed.slug, "   ")["hits"] == []
    assert experts.search(indexed.slug, "zzzz-nothing-matches-this")["hits"] == []
    assert experts.search(indexed.slug, "rhythm", k="nonsense")["hits"]
    empty = experts.create_expert("Empty")
    assert experts.search(empty["slug"], "rhythm")["hits"] == []


# ── citations ───────────────────────────────────────────────────────────────


def test_citation_resolves_back_to_the_page(indexed):
    hit = experts.search(indexed.slug, "pacing verbs", k=1)["hits"][0]
    cite = experts.citation(indexed.slug, hit["chunk_id"])
    assert cite["source"] == "manual.pdf" and cite["page"] == 2
    assert cite["page_confidence"] == "exact"
    assert "pacing" in cite["excerpt"]
    assert cite["file_url"] == f"/api/experts/{indexed.slug}/corpus/manual.pdf"
    # The file never left the machine, so the absolute path is the honest field.
    assert os.path.isfile(cite["file_path"])
    assert experts.citation(indexed.slug, "c-nope") is None
    assert experts.citation("no-such-expert", hit["chunk_id"]) is None


def test_render_page_says_so_when_no_renderer_is_installed(indexed):
    out = experts.render_page(indexed.slug, "manual.pdf", 1)
    assert out["available"] in (True, False)
    if not out["available"]:
        # No dependency was added for a nicety: the UI links to the file.
        assert out["reason"] and out["file_url"].endswith("/corpus/manual.pdf")
    for bad in [("nope.pdf", 1), ("notes.md", 1), ("manual.pdf", 0),
                ("manual.pdf", "seven")]:
        assert experts.render_page(indexed.slug, *bad)["available"] is False


# ── the context block ───────────────────────────────────────────────────────


def test_expert_block_markers_map_onto_chunk_ids_in_order(indexed):
    block = experts.expert_block(indexed.slug, "dialogue rhythm pacing", 2500)
    assert block["chunk_ids"]
    assert block["degraded"] is True          # no vector store in this fixture
    for i, chunk_id in enumerate(block["chunk_ids"], start=1):
        marker = f"[C{i}]"
        assert marker in block["text"]
        cite = experts.citation(indexed.slug, chunk_id)
        # The marker line names the same source the chunk id resolves to.
        line = next(ln for ln in block["text"].splitlines() if ln.startswith(marker))
        assert cite["source"] in line
        if cite["page"] is not None:
            assert f"p.{cite['page']}" in line
        else:
            assert f"L{cite['start_line']}-{cite['end_line']}" in line
    # One marker per chunk id, no more.
    assert f"[C{len(block['chunk_ids']) + 1}]" not in block["text"]
    # The instructions and the rubric are what the expert IS; both are there.
    assert "NO toques la voz del autor" in block["text"]
    assert "Coherencia de POV" in block["text"]


def test_expert_block_is_deterministic_and_respects_its_budget(indexed):
    for budget in (300, 800, 2500, 9000):
        first = experts.expert_block(indexed.slug, "dialogue rhythm", budget)
        second = experts.expert_block(indexed.slug, "dialogue rhythm", budget)
        assert first == second, "same corpus + same query must be byte-identical"
        assert len(first["text"]) <= budget, budget
    # A budget too small for any excerpt still yields the profile, and claims
    # no citations it did not include.
    tiny = experts.expert_block(indexed.slug, "dialogue rhythm", 200)
    assert len(tiny["text"]) <= 200
    for i in range(1, 9):
        if f"[C{i}]" in tiny["text"]:
            assert len(tiny["chunk_ids"]) >= i


def test_expert_block_packs_the_experts_own_learned_rules(indexed):
    memory_engine.add_item("Nunca reescribas el diálogo entero",
                           owner="", project=f"expert:{indexed.slug}",
                           level="procedural", trust_class="human_explicit")
    memory_engine.add_item("Esto pertenece a otro proyecto",
                           owner="", project="expert:otro",
                           level="procedural", trust_class="human_explicit")
    block = experts.expert_block(indexed.slug, "dialogue rhythm", 3000)
    assert "Nunca reescribas el diálogo entero" in block["text"]
    assert "otro proyecto" not in block["text"]


def test_expert_block_never_raises_into_the_turn(store, monkeypatch):
    assert experts.expert_block("no-such-expert", "x") == {
        "text": "", "chunk_ids": [], "degraded": False}
    made = _corrector()
    assert experts.expert_block(made["slug"], "x", 0)["text"] == ""
    assert experts.expert_block(made["slug"], "x", "nonsense")["text"]
    monkeypatch.setattr(experts, "search",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert experts.expert_block(made["slug"], "x") == {
        "text": "", "chunk_ids": [], "degraded": False}


def test_the_block_budget_comes_from_the_setting(store, monkeypatch):
    made = _corrector()
    monkeypatch.setattr("src.settings.get_setting",
                        lambda key, default=None: 700 if key == "agent_expert_context_chars"
                        else default)
    assert experts.context_budget() == 700
    assert len(experts.expert_block(made["slug"], "rhythm")["text"]) <= 700


# ── counters and Thompson selection ─────────────────────────────────────────


def test_feedback_counters_accumulate_in_the_sidecar(store):
    made = _corrector()
    slug = made["slug"]
    assert experts.load_usage(slug) == {"invocations": 0, "accepted": 0,
                                        "rejected": 0, "last_used": None}
    experts.record_feedback(slug, 3, 1)
    experts.record_feedback(slug, accepted=True)
    experts.record_invocation(slug)
    usage = experts.load_usage(slug)
    assert usage["accepted"] == 4 and usage["rejected"] == 1
    assert usage["invocations"] == 1 and usage["last_used"]
    # EXPERT.md must NOT churn on a counter change.
    assert "accepted" not in open(experts.profile_path(slug), encoding="utf-8").read()

    # Garbage in, zeros out — never an exception on the review path.
    experts.record_feedback(slug, "many", None)
    assert experts.load_usage(slug)["accepted"] == 4
    assert experts.record_feedback("no-such-expert", 1, 0)["accepted"] == 0

    with open(experts.usage_path(slug), "w", encoding="utf-8") as fh:
        fh.write("[not an object]")
    assert experts.load_usage(slug)["accepted"] == 0
    assert os.path.isfile(experts.usage_path(slug) + ".corrupt")


def test_suggest_is_deterministic_under_a_seed(store):
    experts.create_expert("Corrector")
    experts.create_expert("Maestria IA")
    experts.record_feedback("corrector", 20, 0)

    first = experts.suggest("ritmo del diálogo", k=2, seed=7)
    second = experts.suggest("ritmo del diálogo", k=2, seed=7)
    assert first == second
    assert [r["slug"] for r in first] == [r["slug"] for r in
                                          experts.suggest("otra consulta", k=2, seed=7)]
    assert set(first[0]) >= {"slug", "name", "score", "accepted", "rejected",
                             "relevance", "invocations"}
    assert experts.suggest("x", k=1, seed=7) == first[:1]


def test_suggest_never_starves_a_new_expert(store):
    experts.create_expert("Veterano")
    experts.create_expert("Novato")
    experts.record_feedback("veterano", 30, 1)

    winners = [experts.suggest("cualquier consulta", k=1, seed=s)[0]["slug"]
               for s in range(60)]
    # A never-used expert has a flat Beta(1,1), so it is always reachable —
    # that is the whole point of sampling instead of taking the running mean.
    assert "novato" in winners
    # And the proven one still wins most of the time.
    assert winners.count("veterano") > winners.count("novato")


def test_suggest_ignores_disabled_experts_and_an_empty_shelf(store):
    assert experts.suggest("anything") == []
    made = experts.create_expert("Apagado")
    experts.update_expert(made["slug"], {"enabled": False})
    assert experts.suggest("anything") == []


# ── defensive entry points ──────────────────────────────────────────────────


def test_every_entry_point_survives_a_missing_directory(store, tmp_path, monkeypatch):
    monkeypatch.setattr(experts, "DATA_DIR", str(tmp_path / "does" / "not" / "exist"))
    assert experts.list_expert_slugs() == []
    assert experts.list_experts() == []
    assert experts.load_expert("ghost") is None
    assert experts.load_index("ghost") == []
    assert experts.corpus_files("ghost") == []
    assert experts.reindex("ghost")["chunks"] == 0
    assert experts.search("ghost", "q")["hits"] == []
    assert experts.expert_block("ghost", "q")["text"] == ""
    assert experts.citation("ghost", "c1") is None
    assert experts.suggest("q") == []
    assert experts.summary("ghost") is None
    assert experts.detail_payload("ghost") is None
    assert experts.delete_corpus_file("ghost", "a.md") is False
    assert experts.render_page("ghost", "a.pdf", 1)["available"] is False
    assert experts.list_payload()["experts"] == []


def test_a_blank_slug_can_never_escape_the_experts_root(store):
    for bad in ("", "   ", "..", "../..", "/etc", None):
        assert experts.load_expert(bad) is None
        assert experts.load_index(bad) == []
        assert experts.search(bad, "q")["hits"] == []
    # A traversal attempt is slugified into a harmless direct child name.
    assert "/" not in experts._clean_slug("../../etc/passwd")
    assert "\\" not in experts._clean_slug("..\\..\\windows")


def test_settings_exist_and_are_described(store):
    from src.agent_settings_schema import schema_keys, schema_problems
    from src.settings import DEFAULT_SETTINGS

    assert DEFAULT_SETTINGS["agent_experts"] is True
    assert DEFAULT_SETTINGS["agent_expert_context_chars"] == 2500
    assert "agent_experts" in schema_keys()
    assert "agent_expert_context_chars" in schema_keys()
    assert schema_problems() == []
    assert experts.experts_enabled() is True
    assert experts.context_budget() == 2500


# ── the HTTP API ────────────────────────────────────────────────────────────


@pytest.fixture()
def client(store, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core.middleware import require_admin
    from routes import expert_routes

    monkeypatch.setattr(expert_routes, "effective_user", lambda request: "luis")
    app = FastAPI()
    app.include_router(expert_routes.setup_expert_routes())
    app.dependency_overrides[require_admin] = lambda: None
    return TestClient(app)


def test_api_creates_reads_patches_and_deletes(client, store):
    created = client.post("/api/experts", json={
        "name": "Corrector narrativo",
        "description": "Revisa ritmo y diálogo",
        "instructions": "NO toques la voz del autor.",
        "rubric": ["Ritmo", "Diálogo"],
        "model": "qwen2.5:14b",
    })
    assert created.status_code == 200
    expert = created.json()["expert"]
    assert expert["slug"] == "corrector-narrativo" and expert["owner"] == "luis"
    assert expert["rubric"] == ["Ritmo", "Diálogo"]

    listed = client.get("/api/experts").json()
    assert [e["slug"] for e in listed["experts"]] == ["corrector-narrativo"]
    row = listed["experts"][0]
    assert set(row) == {"slug", "name", "description", "model", "enabled", "owner",
                        "corpus_files", "chunks", "indexed_at", "invocations",
                        "accepted", "rejected", "updated_at"}
    assert row["corpus_files"] == 0 and row["chunks"] == 0
    assert listed["enabled"] is True and listed["context_chars"] == 2500

    detail = client.get("/api/experts/corrector-narrativo").json()
    assert detail["expert"]["instructions"] == "NO toques la voz del autor."
    assert detail["files"] == [] and detail["usage"]["accepted"] == 0
    assert detail["collection"] == "odysseus_expert_corrector-narrativo"

    patched = client.patch("/api/experts/corrector-narrativo",
                           json={"rubric": "Ritmo\nPOV", "enabled": False})
    assert patched.json()["expert"]["rubric"] == ["Ritmo", "POV"]
    assert client.get("/api/experts/corrector-narrativo").json()["expert"]["enabled"] is False

    assert client.delete("/api/experts/corrector-narrativo").json()["deleted"] is True
    assert client.get("/api/experts").json()["experts"] == []


def test_api_reports_bad_input_without_a_500(client, store):
    assert client.post("/api/experts", json={"name": "   "}).status_code == 400
    assert client.get("/api/experts/nope").status_code == 404
    assert client.patch("/api/experts/nope", json={"name": "x"}).status_code == 404
    assert client.delete("/api/experts/nope").status_code == 404
    assert client.post("/api/experts/nope/reindex").status_code == 404
    assert client.get("/api/experts/nope/search?q=x").status_code == 404
    assert client.get("/api/experts/nope/block?q=x").status_code == 404
    made = client.post("/api/experts", json={"name": "E"}).json()["expert"]
    assert client.patch(f"/api/experts/{made['slug']}",
                        json={"name": "  "}).status_code == 400
    assert client.get(f"/api/experts/{made['slug']}/citation/c-nope").status_code == 404
    assert client.delete(f"/api/experts/{made['slug']}/corpus/nope.md").status_code == 404
    assert client.get(f"/api/experts/{made['slug']}/corpus/nope.md").status_code == 404


def test_api_uploads_reindexes_searches_and_cites(client, store, tmp_path):
    slug = client.post("/api/experts", json={
        "name": "Corrector", "instructions": "No toques la voz.",
        "rubric": ["Ritmo"]}).json()["expert"]["slug"]

    _write_pdf(tmp_path / "manual.pdf",
               ["dialogue rhythm carries the scene",
                "pacing tightens when the verbs shorten"])
    with open(tmp_path / "manual.pdf", "rb") as pdf:
        uploaded = client.post(f"/api/experts/{slug}/corpus", files=[
            ("files", ("manual.pdf", pdf.read(), "application/pdf")),
            ("files", ("notes.md", b"Rhythm is what a reader feels first.\n", "text/markdown")),
            ("files", ("evil.exe", b"MZ", "application/octet-stream")),
        ])
    body = uploaded.json()
    assert sorted(body["uploaded"]) == ["manual.pdf", "notes.md"]
    assert [r["name"] for r in body["rejected"]] == ["evil.exe"]
    assert body["indexed"] == 2 and body["chunks"] == 3
    assert {f["name"] for f in body["files"]} == {"manual.pdf", "notes.md"}
    assert next(f for f in body["files"] if f["name"] == "manual.pdf")["pages"] == 2

    again = client.post(f"/api/experts/{slug}/reindex").json()
    assert again["indexed"] == 0 and again["skipped"] == 2 and again["indexed_at"]

    found = client.get(f"/api/experts/{slug}/search?q=pacing%20verbs&k=2").json()
    assert found["tier"] == "lexical" and found["degraded"] is True
    top = found["hits"][0]
    assert top["source"] == "manual.pdf" and top["page"] == 2

    cite = client.get(f"/api/experts/{slug}/citation/{top['chunk_id']}").json()["citation"]
    assert cite["page"] == 2 and cite["file_url"].endswith("/corpus/manual.pdf")
    served = client.get(cite["file_url"])
    assert served.status_code == 200 and served.content[:4] == b"%PDF"

    block = client.get(f"/api/experts/{slug}/block?q=pacing&chars=1200").json()
    assert "[C1]" in block["text"] and len(block["chunk_ids"]) >= 1
    assert block["chars"] == len(block["text"]) <= 1200 and block["budget"] == 1200

    page = client.get(f"/api/experts/{slug}/page?source=manual.pdf&page=1").json()["render"]
    assert page["available"] in (True, False)

    removed = client.delete(f"/api/experts/{slug}/corpus/manual.pdf").json()
    assert removed["deleted"] is True
    assert {f["name"] for f in removed["files"]} == {"notes.md"}
    assert removed["chunks"] == 1


def test_api_records_feedback_and_suggests(client, store):
    a = client.post("/api/experts", json={"name": "Veterano"}).json()["expert"]["slug"]
    client.post("/api/experts", json={"name": "Novato"})
    usage = client.post(f"/api/experts/{a}/feedback?accepted=5&rejected=1").json()["usage"]
    assert usage["accepted"] == 5 and usage["rejected"] == 1
    assert usage["invocations"] == 0 and usage["last_used"]
    assert client.get(f"/api/experts/{a}").json()["usage"]["accepted"] == 5

    first = client.get("/api/experts/suggest?q=ritmo&k=2&seed=3").json()["suggestions"]
    assert [s["slug"] for s in first] == [
        s["slug"] for s in
        client.get("/api/experts/suggest?q=ritmo&k=2&seed=3").json()["suggestions"]]
    assert {s["slug"] for s in first} == {"veterano", "novato"}
    # `/suggest` must not be read as a slug.
    assert client.get("/api/experts/suggest").status_code == 200


def test_api_answers_in_robot_mode(client, store):
    slug = client.post("/api/experts", json={
        "name": "Corrector", "description": "Ritmo y diálogo"}).json()["expert"]["slug"]
    open(os.path.join(experts.corpus_dir(slug), "notes.md"), "w").write(
        "Rhythm is what a reader feels first.\n" * 20)
    client.post(f"/api/experts/{slug}/reindex")

    listed = client.get("/api/experts?robot=1").json()
    rows = listed["data"]["experts"]
    assert set(rows[0]) == {"slug", "name", "description", "enabled",
                            "files", "chunks", "accepted", "rejected"}
    assert rows[0]["chunks"] >= 1

    found = client.get(f"/api/experts/{slug}/search?q=rhythm&robot=1").json()
    data = found["data"]
    assert data["tier"] == "lexical" and data["degraded"] is True
    assert set(data["hits"][0]) == {"chunk_id", "source", "page", "lines",
                                    "score", "text"}
    assert data["hits"][0]["page"] is None


def test_the_lean_projections_never_cost_the_answer():
    from routes.expert_routes import lean_experts, lean_search
    assert lean_experts({"experts": "not a list"})["experts"] == []
    assert lean_search({"hits": [None, 3]})["hits"] == []
    assert lean_experts({})["experts"] == []
    assert lean_search({})["hits"] == []
