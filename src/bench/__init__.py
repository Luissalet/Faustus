"""src/bench — INF-04: the local-inference optimization benchmark.

`suites.py` loads the deterministic test suites and judges quality;
`runner.py` plans, runs and compares benchmark executions;
`profiles.py` manages saved `InferenceProfile`s and the promotion rule.
Nothing in this package starts a model download, an engine install, or a
process restart (CONTRATO_INF04's non-negotiable rules) — it only calls the
same `stream_llm`/`vram_admission.admit` path a normal chat turn already
uses, one case at a time.
"""
