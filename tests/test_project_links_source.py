"""`ProjectLinksSource` — the manifest in every turn, the content only on ask.

The failure this adapter exists to prevent is the obvious implementation:
"the project has six linked documents, so put six documents in the prompt".
That spends the window before the question has been read, and it spends it on
whatever the user happened to attach last month. So the properties pinned here
are the ones that separate a catalogue from a dump:

* **the manifest is one line per link and carries no source text.** Every
  enabled link produces exactly one candidate, in `project_rules`, whose body
  is `- ctx_… [role, kind, policy] label` and nothing else;

* **content arrives only for a query**, and only from links whose
  `retrieval_policy` is `auto` or `pinned_summary`. `on_demand` and `disabled`
  stay out unless the caller named them in `explicit_refs` — the one channel
  by which a person, not a model, overrides a policy;

* **`disabled` never appears at all**, not even as a manifest line: the
  vocabulary's own definition of it is "may not enter a prompt";

* **another project's links are not reachable**, because the isolation is an
  argument to the store's query and not a filter over a global answer;

* **`index_status="queued"` is still readable** — §12's immediate
  consistency — and every candidate that comes out of that read says
  `degraded=True` with a note, because "I read this file" and "this is
  indexed" are different claims;

* **`source_ref` round-trips**, so provenance survives the packet.
"""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import projects as projects_mod  # noqa: E402
from services.projects import ProjectStore  # noqa: E402
from src.context_engine import candidates as C  # noqa: E402
from src.context_engine.adapters import project_links as PL  # noqa: E402
from src.context_engine.contracts import (  # noqa: E402
    ContextActor, ContextCandidate, ContextExecution, ContextPolicy,
    ContextRequest, ContextTask,
)

OWNER = "luis"
BODY = ("# Product requirements\n"
        "\n"
        "The exporter must emit TOON.\n"
        "Nothing else may change the envelope.\n")


# ── scaffolding ────────────────────────────────────────────────────────────


@pytest.fixture()
def store(tmp_path, monkeypatch):
    st = ProjectStore(str(tmp_path / "data"))
    monkeypatch.setattr(projects_mod, "_store", st)
    return st


@pytest.fixture()
def project(store, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    return store.create("Faustus", folder="Faustus", workspace=str(ws),
                        owner=OWNER)


@pytest.fixture()
def other_project(store, tmp_path):
    ws = tmp_path / "other-ws"
    ws.mkdir()
    return store.create("Otro", folder="Otro", workspace=str(ws), owner=OWNER)


@pytest.fixture()
def note(tmp_path):
    path = tmp_path / "requirements.md"
    path.write_text(BODY, encoding="utf-8")
    return str(path)


def attach(store, project, path, **over):
    """A stored link, straight through the store the adapter reads."""
    link = {
        "id": over.pop("id", "") or f"ctx_{os.urandom(5).hex()}",
        "kind": "file",
        "path": path,
        "label": over.pop("label", os.path.basename(path)),
        "role": over.pop("role", "requirements"),
        "retrieval_policy": over.pop("retrieval_policy", "auto"),
        "index_status": over.pop("index_status", "ready"),
        "content_revision": over.pop("content_revision", "file:rev-1"),
        "enabled": over.pop("enabled", True),
    }
    link.update(over)
    saved, _ = store.upsert_link(project["id"], link, owner=OWNER)
    return saved


def _request(*, query="", project_id="", owner=OWNER, session_id="s1",
             workspace="", refs=(), **policy):
    return ContextRequest(
        actor=ContextActor(agent_id="worker-1", model="test-model"),
        execution=ContextExecution(owner=owner, project_id=project_id,
                                   workspace=workspace, session_id=session_id),
        task=ContextTask(intent="implement", phase="act", query=query),
        policy=ContextPolicy(**policy),
        explicit_refs=tuple(refs),
    )


def _round(request, *, sections=(), limit=8, lanes=(), refs=()):
    return C.RetrievalRequest(request=request, query=request.task.query,
                              sections=tuple(sections), limit=limit,
                              lanes=tuple(lanes), explicit_refs=tuple(refs))


def _search(req):
    return list(asyncio.run(PL.ProjectLinksSource().search(req)))


def _manifest(rows):
    return [c for c in rows if (c.meta or {}).get("manifest")]


def _excerpts(rows):
    return [c for c in rows if not (c.meta or {}).get("manifest")]


# ── the manifest ───────────────────────────────────────────────────────────


def test_the_manifest_is_one_line_per_link_and_carries_no_content(
        store, project, note):
    attach(store, project, note, label="Product requirements v4",
           role="requirements", retrieval_policy="auto", index_status="ready")

    rows = _search(_round(_request(project_id=project["id"])))
    assert len(rows) == 1

    entry = rows[0]
    assert entry.section == "project_rules"
    assert entry.lanes == ("mandatory",)
    assert entry.body.count("\n") == 0
    assert entry.body.startswith("- ctx_")
    assert "[requirements, file, auto] Product requirements v4" in entry.body
    # The line names the source; it never quotes it.
    assert "TOON" not in entry.body
    assert BODY.strip() not in entry.body
    # What the adapter did, recorded where a receipt can read it.
    assert entry.meta["transformation"] == "reference"
    assert entry.degraded is False

    # And it survives the contract it will be parsed by three layers later.
    assert ContextCandidate.parse(entry.to_dict()).source_ref == entry.source_ref


def test_the_manifest_names_every_link_and_the_source_ref_parses(
        store, project, note, tmp_path):
    second = tmp_path / "decisions.md"
    second.write_text("# Decisions\n\nWe chose TOON.\n", encoding="utf-8")
    first = attach(store, project, note, label="Requirements")
    other = attach(store, project, str(second), label="Decisions",
                   role="decision", retrieval_policy="pinned_summary")

    rows = _search(_round(_request(project_id=project["id"])))
    assert len(rows) == 2
    ids = []
    for candidate in rows:
        link_id, location = PL.parse_link_ref(candidate.source_ref)
        assert location == {}
        assert link_id == candidate.meta["link_id"]
        ids.append(link_id)
    assert set(ids) == {first["id"], other["id"]}


def test_a_disabled_link_never_appears_at_all(store, project, note, tmp_path):
    """Two different "off" switches, and neither may reach a prompt.

    `enabled=False` is "this link is not in play"; `retrieval_policy="disabled"`
    is the vocabulary's own "may not enter a prompt". A manifest line is a
    prompt, so both are excluded from it, not merely from the search.
    """
    switched_off = tmp_path / "off.md"
    switched_off.write_text("secret plan\n", encoding="utf-8")
    never = tmp_path / "never.md"
    never.write_text("also secret\n", encoding="utf-8")

    keep = attach(store, project, note, label="Kept")
    attach(store, project, str(switched_off), label="Switched off", enabled=False)
    attach(store, project, str(never), label="Never", retrieval_policy="disabled")

    for query in ("", "secret"):
        rows = _search(_round(_request(query=query, project_id=project["id"])))
        bodies = " ".join(c.body for c in rows)
        titles = " ".join(c.title for c in rows)
        assert "Switched off" not in bodies + titles
        assert "Never" not in bodies + titles
        assert "secret" not in bodies
        assert keep["id"] in bodies


def test_another_projects_links_are_not_reachable(store, project, other_project,
                                                  note, tmp_path):
    """Isolation is an argument to the query (§12), and the observable
    consequence is that nothing of the neighbour's ever arrives."""
    theirs = tmp_path / "theirs.md"
    theirs.write_text("their requirements\n", encoding="utf-8")
    mine = attach(store, project, note, label="Mine")
    yours = attach(store, other_project, str(theirs), label="Theirs")

    rows = _search(_round(_request(query="requirements",
                                   project_id=project["id"])))
    refs = {c.source_ref for c in rows}
    assert any(mine["id"] in ref for ref in refs)
    assert not any(yours["id"] in ref for ref in refs)
    assert "Theirs" not in " ".join(c.title for c in rows)

    # And `_fetch` will not reopen it across the boundary either.
    reopened = asyncio.run(PL.ProjectLinksSource().fetch(
        PL.link_ref(yours["id"]), _round(_request(project_id=project["id"]))))
    assert reopened is None


# ── content, only when the query asks ──────────────────────────────────────


def test_a_query_brings_excerpts_only_from_auto_and_pinned_summary(
        store, project, tmp_path):
    """The four policies, one query, and only two of them answer.

    `on_demand` is not "off": the agent can still open it with a tool. What it
    is not is *automatic*, and an adapter that ignored the difference would
    make the setting decorative.
    """
    paths = {}
    for policy in ("auto", "pinned_summary", "on_demand", "disabled"):
        path = tmp_path / f"{policy}.md"
        path.write_text(f"# {policy}\n\nThe exporter must emit TOON.\n",
                        encoding="utf-8")
        paths[policy] = attach(store, project, str(path), label=policy.upper(),
                               retrieval_policy=policy, index_status="ready")

    rows = _search(_round(_request(query="TOON", project_id=project["id"]),
                          sections=("project_rules", "code_map")))
    excerpts = _excerpts(rows)
    assert excerpts, "a query with matching links produced no content at all"

    quoted = {c.meta["link_id"] for c in excerpts}
    assert quoted == {paths["auto"]["id"], paths["pinned_summary"]["id"]}

    for candidate in excerpts:
        assert candidate.section == "code_map"     # a linked file is code_map
        assert "TOON" in candidate.body
        assert candidate.lanes == ("lexical",)
        assert candidate.trust_class == "observed"
        assert candidate.meta["retrieval_reason"] == "keyword+role"
        link_id, location = PL.parse_link_ref(candidate.source_ref)
        assert link_id == candidate.meta["link_id"]
        assert location["line"] == str(candidate.meta["location"]["line"])


def test_without_a_query_nothing_is_read_from_any_source(store, project, note):
    attach(store, project, note, retrieval_policy="auto")
    rows = _search(_round(_request(project_id=project["id"])))
    assert _excerpts(rows) == []
    assert "TOON" not in " ".join(c.body for c in rows)


def test_an_explicit_reference_overrides_the_policy(store, project, tmp_path):
    """The one channel by which a *person* reaches past a policy.

    `explicit_refs` comes off the runtime, never out of the query text — a
    reference the runtime did not put there is a reference a model chose.
    """
    path = tmp_path / "manual.md"
    path.write_text("# Manual\n\nThe exporter must emit TOON.\n", encoding="utf-8")
    link = attach(store, project, str(path), label="Manual",
                  retrieval_policy="on_demand", index_status="ready")

    silent = _search(_round(_request(query="TOON", project_id=project["id"]),
                            sections=("project_rules", "code_map")))
    assert _excerpts(silent) == []

    named = _search(_round(_request(query="TOON", project_id=project["id"]),
                           sections=("project_rules", "code_map"),
                           refs=(PL.link_ref(link["id"]),)))
    quoted = _excerpts(named)
    assert [c.meta["link_id"] for c in quoted] == [link["id"]]
    assert quoted[0].lanes == ("explicit", "lexical")
    assert quoted[0].meta["retrieval_reason"] == "explicit_reference"


def test_every_excerpt_carries_the_whole_provenance(store, project, note):
    """§15: link, kind, ref, revision and location, in the candidate and in
    the ref, so "which version did you read?" survives the packet."""
    link = attach(store, project, note, label="Requirements",
                  role="requirements", retrieval_policy="auto",
                  index_status="ready")
    rows = _excerpts(_search(_round(_request(query="TOON",
                                             project_id=project["id"]),
                                    sections=("code_map",))))
    assert rows
    got = rows[0]
    assert got.meta["link_id"] == link["id"]
    assert got.meta["kind"] == "file"
    assert got.meta["role"] == "requirements"
    assert got.meta["path"] == note
    assert got.meta["revision"] and got.meta["revision"].startswith("file:")
    assert got.meta["location"]["line"] >= 1
    assert got.project_id == project["id"]
    assert got.owner == OWNER
    # The candidate the compiler will parse, not just the one we built.
    assert ContextCandidate.parse(got.to_dict()).meta["link_id"] == link["id"]


# ── immediate consistency ──────────────────────────────────────────────────


def test_a_queued_link_is_readable_and_says_it_is_not_indexed(store, project,
                                                              note):
    """§12. Indexing may be asynchronous; usefulness may not wait for it.

    The half that matters as much: the candidate does not pretend. A packet
    that said "retrieved from the project index" about a direct read would be
    unfalsifiable, and the first person to ask "why didn't it find X" would
    have no way to tell an index gap from a ranking one.
    """
    link = attach(store, project, note, label="Fresh", retrieval_policy="auto",
                  index_status="queued")

    rows = _excerpts(_search(_round(_request(query="TOON",
                                             project_id=project["id"]),
                                    sections=("code_map",))))
    assert rows, "a queued link became unreadable, which is the bug §12 names"
    got = rows[0]
    assert "TOON" in got.body
    assert got.degraded is True
    assert got.meta["index_status"] == "queued"
    assert "read directly" in got.meta["note"]
    assert "queued" in got.meta["note"]
    assert link["id"] == got.meta["link_id"]

    # An indexed one makes no such disclaimer.
    ready = attach(store, project, note, id="ctx_readyready",
                   label="Ready", retrieval_policy="auto",
                   index_status="ready", version_policy="pinned",
                   pinned_version=1)
    settled = [c for c in _excerpts(_search(
        _round(_request(query="TOON", project_id=project["id"]),
               sections=("code_map",)))) if c.meta["link_id"] == ready["id"]]
    assert settled and all(c.degraded is False for c in settled)
    assert all("note" not in (c.meta or {}) for c in settled)


# ── reopening a ref ────────────────────────────────────────────────────────


def test_the_source_ref_round_trips_through_fetch(store, project, note):
    """Rule 4 of `contracts.py`: a `source_ref` is only provenance if
    something can still resolve it."""
    link = attach(store, project, note, label="Requirements",
                  retrieval_policy="auto", index_status="ready")
    round_ = _round(_request(query="TOON", project_id=project["id"]),
                    sections=("code_map",))
    quoted = _excerpts(_search(round_))[0]

    source = PL.ProjectLinksSource()
    again = asyncio.run(source.fetch(quoted.source_ref, round_))
    assert again is not None
    assert "TOON" in again.body
    assert again.lanes == ("exact",)
    assert again.meta["link_id"] == link["id"]

    # The bare ref answers with the manifest entry, not with content.
    pointer = asyncio.run(source.fetch(PL.link_ref(link["id"]), round_))
    assert pointer is not None
    assert pointer.meta["manifest"] is True
    assert "TOON" not in pointer.body

    # And an id nobody minted answers None rather than raising.
    assert asyncio.run(source.fetch("link:ctx_nothing", round_)) is None
    assert asyncio.run(source.fetch("mem:1", round_)) is None


def test_a_match_inside_a_linked_folder_reopens_the_file_not_the_listing(
        store, project, tmp_path):
    """A folder's own `read()` answers with its listing; the match was inside
    one of its files, and containment is re-checked before the child is
    opened."""
    tree = tmp_path / "tree"
    (tree / "docs").mkdir(parents=True)
    (tree / "docs" / "spec.md").write_text(
        "# Spec\n\nline two\nThe exporter must emit TOON.\nline four\n",
        encoding="utf-8")
    attach(store, project, str(tree), kind="folder", label="Docs tree",
           retrieval_policy="auto", index_status="ready")

    round_ = _round(_request(query="TOON", project_id=project["id"]),
                    sections=("code_map",))
    quoted = _excerpts(_search(round_))
    assert quoted
    got = quoted[0]
    assert got.meta["location"]["path"].endswith("spec.md")

    reopened = asyncio.run(PL.ProjectLinksSource().fetch(got.source_ref, round_))
    assert reopened is not None
    assert "The exporter must emit TOON." in reopened.body
    # A window of the file, with its neighbours — not the folder's file list.
    assert "line two" in reopened.body and "line four" in reopened.body
    assert "\t" not in reopened.body


def test_the_ref_scheme_survives_a_path_with_a_delimiter_in_it():
    ref = PL.link_ref("ctx_a1", {"path": "a,b=c/d.md", "line": 7})
    link_id, location = PL.parse_link_ref(ref)
    assert link_id == "ctx_a1"
    assert location == {"path": "a,b=c/d.md", "line": "7"}
    assert PL.parse_link_ref("doc:x#chunk1") == ("", {})
    assert PL.parse_link_ref("link:") == ("", {})
    assert PL.parse_link_ref("") == ("", {})


# ── policy is applied before the store is consulted ────────────────────────


def test_an_incognito_turn_never_reaches_a_projects_links(store, project, note):
    """`planner.PROJECT_SOURCE_IDS` is pinned to the three ids whose rows the
    ranker would refuse anyway, and this source also produces `document` rows
    the ranker would let through — so the flag is enforced in `_gate`, on the
    event loop, before the store is consulted. "Consulted and discarded" is
    not the same as "never consulted"."""
    attach(store, project, note, retrieval_policy="auto")
    source = PL.ProjectLinksSource()

    incognito = _round(_request(query="TOON", project_id=project["id"],
                                allow_project_sources=False))
    assert source._gate(incognito) == "policy.allow_project_sources is false"
    assert _search(incognito) == []

    allowed = _round(_request(query="TOON", project_id=project["id"]))
    assert source._gate(allowed) == ""


def test_a_round_that_wants_none_of_our_sections_is_not_woken(store, project,
                                                              note):
    attach(store, project, note, retrieval_policy="auto")
    source = PL.ProjectLinksSource()
    chatty = _round(_request(query="TOON", project_id=project["id"]),
                    sections=("recent_messages", "retrieved_memory"))
    assert source._gate(chatty) == "no section this source fills was requested"
    assert _search(chatty) == []


def test_a_request_with_no_project_answers_with_nothing(store, note):
    source = PL.ProjectLinksSource()
    assert source.available() is True
    assert _search(_round(_request(query="TOON", session_id=""))) == []


def test_the_declared_sections_and_handles_are_the_ones_the_planner_plans_for():
    from src.context_engine import planner

    source = PL.ProjectLinksSource()
    assert source.source_id == "project_links"
    assert source.handles == ("link:",)
    assert tuple(source.sections) == planner.SOURCE_SECTIONS["project_links"]
    from src.context_engine.adapters import all_sources
    assert source.source_id in {s.source_id for s in all_sources()}
