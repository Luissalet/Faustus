"""src/swarm — "swarm map": one instruction applied to N items (40
companies, 30 files, 100 URLs) in parallel, as wide as the backend really
serves, then an optional reduce over the collected results.

How it differs from its neighbours:
  * ``src.fanout`` races the SAME prompt across several MODELS and keeps the
    best answer; the swarm sends DIFFERENT items through ONE route and keeps
    every answer.
  * ``delegate_agents`` splits one job into a handful (max 4) of distinct
    tasks with their own instructions; the swarm is the wide, uniform case,
    and its ``agent`` mode reuses that tool's worker for each item.

Modules:
  * ``capacity`` — effective parallelism of an endpoint (llama.cpp slots,
    the Ollama/API settings, the global caps).
  * ``lane``     — lets a swarm item use a local server's parallel slots
    without taking the one-pipe lock the chat waits on.
  * ``render``   — items -> prompts, replies -> rows, rows -> MD/CSV/JSONL.
  * ``store``    — the checkpoint under ``DATA_DIR/swarm/<run_id>/``.
  * ``runner``   — runs the pending items, retries, reduce, exports.
  * ``service``  — start/status/results/cancel/resume for tools and routes.

Kept import-free: ``src.llm_core`` imports ``src.swarm.lane`` on its hot
path.

See ``docs/recipes/swarm.md``.
"""
