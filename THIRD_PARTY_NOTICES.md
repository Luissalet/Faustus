# Third-Party Notices

This file is a machine-checkable companion to
[`ACKNOWLEDGMENTS.md`](ACKNOWLEDGMENTS.md) — its structured mirror is
[`docs/adaptations/provenance.json`](docs/adaptations/provenance.json),
validated by `tests/test_adp02_provenance.py` (JSON shape + every
`destination` path actually existing in this repo). `ACKNOWLEDGMENTS.md`
remains the narrative, human-facing credits page; this file exists so an
adaptation's repo/commit/license/destination is recorded as DATA an
automated check can verify (ADP-02), not only as prose someone might
forget to keep in sync with the code.

## Upstream

- **[Odysseus](https://github.com/odysseus-dev/odysseus)** — Faustus is a
  personal fork of Odysseus. **AGPL-3.0-or-later**, the same license as
  this repository (see [`LICENSE`](LICENSE)). The codebase before
  Faustus's own changes is upstream Odysseus source; internal names
  (`odysseus`, `ODYSSEUS_*`, existing API paths, storage keys) are
  retained for compatibility rather than renamed — see `README.md`.

## Adapted pieces (technique/idea, not vendored code)

- **[aigraphstudio](https://github.com/gcjordi/aigraphstudio)** by
  **gcjordi**. **MIT.** The source research that identified this
  adaptation cites `/blob/main/...` links, not a commit-pinned URL, so no
  commit hash is recorded — `docs/adaptations/provenance.json` carries the
  literal string `"commit": "unknown"` for these three entries rather than
  a guessed value (same discipline this repo uses for an unpriced model:
  an explicit unknown, never a fabricated placeholder).

  No aigraphstudio source file is vendored or transliterated anywhere in
  this repo. The three destinations below each reimplement a TECHNIQUE
  described in aigraphstudio's `ARCHITECTURE.md` (and, for two of them,
  its `assets/js/analyzer.js` heuristics) against Faustus's own data
  model — see each module's own docstring for exactly what was and was
  not carried over, and `docs/adaptations/provenance.json` for the
  structured per-file entries:

  - `src/agent_profile_lint.py` — Tarjan-SCC cycle detection, orphan-node
    detection, and "effectful node with no human-approval ancestor"
    bypass detection, applied to Faustus's own
    `src.contracts.workflow.WorkflowDefinition` and agent-profile
    catalogue.
  - `src/topology_export.py` — exporting a workflow (and, for Faustus, a
    branching-futures tree) as a Mermaid diagram, applied to Faustus's
    own workflow/branching-futures data.
  - `src/workflow_cost_estimate.py` — the executions × loop-iterations ×
    per-model-price cost-estimation shape, applied to Faustus's own
    `WorkflowDefinition` and pricing data. Faustus's own invariant that an
    unpriced model is reported as `unknown`, never `0`, is not something
    asserted about the source.
  - `src/workflows/interchange.py` — the workflow-graph import/export shape
    (`schemaVersion`, typed nodes, layout coordinates) described in
    `ARCHITECTURE.md` §6.2, applied to Faustus's own `WorkflowDefinition`
    (its sibling `src/workflows/preflight.py`, added in the same lote, is a
    zero-effect preflight report that carries no aigraphstudio-derived
    shape of its own). The assumed aigraphstudio JSON shape was
    reconstructed from that description alone and was **not** verified
    against a real aigraphstudio export (no external fetch was made for
    this lote) — treat `import_external`'s aigraphstudio-shape handling as
    unverified until checked against real output. Only six node types
    (`Start`/`Input`/`Output`/`Tool`/`Human Approval`/`Router`) map to a
    real, checked handler; every other type becomes `design_only`, and a
    cyclic or unknown-typed graph is never marked executable.

- **[agent-desktop](https://github.com/lahfir/agent-desktop)** by
  **lahfir**. **Apache-2.0.** `src/desktop_semantics/` adapts this
  project's CONTRACT VOCABULARY only — the snapshot/ref/precondition/
  delivery-disposition shape documented in its README and
  `crates/core/src/adapter_error.rs` error taxonomy — not its macOS
  accessibility backend, which is not ported anywhere in this repo. No
  Rust source is vendored or transliterated; Faustus's `Ref` format,
  typed errors (`STALE_REF`/`AMBIGUOUS_TARGET`/`WRONG_SESSION`), and the
  real backend (an optional, lazily-imported `pywinauto`-based Windows
  implementation in `windows_uia.py`) are original to this repo.

- **[Aider](https://github.com/Aider-AI/aider)** by **Aider-AI**.
  **Apache-2.0.** `src/repo_map.py::symbols_for` adapts the TECHNIQUE of
  Aider's repo map — ranking symbols by definitions weighed against
  reference counts, under a character budget — without copying
  `aider/repomap.py`. The ranking runs over Faustus's own
  `src/context_engine/code_index.py` symbol/edge store and Faustus's own
  budget/degradation vocabulary (`parser: regex` when tree-sitter is
  absent); no graph-ranking library was introduced.

*(Faustus descends from Odysseus, whose own `ACKNOWLEDGMENTS.md` already
lists several vendored/adapted pieces — Diogenes, opencode, llmfit, Tongyi
DeepResearch, bundled front-end JS libraries, Docker Compose images,
fonts. Those entries are unchanged by this file; see `ACKNOWLEDGMENTS.md`
for the full narrative list, including which of those already carry a
full license text under `licenses/`. This file starts a second,
schema-checked record — for now, just aigraphstudio — that each future
adaptation should extend rather than replace.)*

## How to add an entry

1. Add one object to the `entries` array in
   [`docs/adaptations/provenance.json`](docs/adaptations/provenance.json)
   (`schema_version: 1`):
   `{id, source_repo, commit, path, blob_sha?, license, notice_kept,
   destination, reason, modifications}`. Every field is required except
   `blob_sha`. Use the exact commit hash when you have one; if the source
   only points at an unpinned branch ref, use the literal string
   `"unknown"` rather than guessing. `destination` must be a real path in
   THIS repo — `tests/test_adp02_provenance.py` checks that it exists.
2. Add a short matching entry HERE, under "Adapted pieces" (or a new
   "Upstream" / "Bundled" section if the piece doesn't fit "adapted") —
   repo link, author, license, and which destination file(s) it fed.
3. If the license requires shipping its own text verbatim (the GPL
   family, or a permissive license whose text you're redistributing
   as-is), add the file under `licenses/` — see the ones already there
   (`licenses/DeepResearch-Apache-2.0.txt`, `licenses/KaTeX-MIT-LICENSE.txt`,
   `licenses/Mermaid-MIT-LICENSE.txt`) — and link it from your entry, the
   way `ACKNOWLEDGMENTS.md` already does for those three.
4. Never replace an upstream copyleft (AGPL/GPL) notice with a permissive
   one, and never delete an existing entry outright — a piece later
   removed from the codebase keeps its historical entry (note the removal
   date/commit in `modifications` instead of deleting the record).
5. Run `python3 -m pytest tests/test_adp02_provenance.py -q` before
   committing — it validates the JSON shape and that every `destination`
   still exists.

## Note for whoever wires the desktop-semantics adaptation (ADP-08/09)

Done — see the `agent-desktop` entry under "Adapted pieces" above and the
matching `agent-desktop-semantic-contract` entry in
`docs/adaptations/provenance.json` (added by the integration lote that
also wrote this note).
