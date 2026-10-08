#!/usr/bin/env python3
"""scripts/constrained_choice_bench.py - OBJ-27 phase 1 acceptance measurement.

Compares, on the SAME inputs, the previous way Deep Research picks its report
category (a free-text call parsed by a keyword scan,
``DeepResearcher._classify_category_free_text``) with the constrained one
(``DeepResearcher._classify_category`` -> ``src.constrained_choice.choose_one``)
and reports, per backend:

* (a) whether the output ever falls outside the closed set;
* (b) tokens (prompt and completion, as the server reports them) and
  milliseconds (wall time around the call) of both paths.

NO MODEL IS LOADED. The "server" is a stub that speaks the llama-server
(``grammar``), native Ollama (``format``) and plain chat-completions wire
shapes and that, like the real thing, can only emit a label the constraint
allows. What is MEASURED here and what is MODELLED:

MEASURED (real, from the code under test):
    the exact request bytes and prompt of each path; the prompt token count
    of each (``src.model_context.estimate_tokens``, the heuristic the codebase
    uses: 0.3 per character + 4 per message); whether the grammar / enum
    reached the wire; the label the constrained path finally returns; wall
    time of each call including the simulated server delay below.

MODELLED (assumptions, all printed with the results):
    what an UNCONSTRAINED model would answer on each case (``--styles``:
    clean label / "The category is x." / a sentence / a word outside the set,
    assigned deterministically) - there is no real model, so the old path's
    completion length and its out-of-set rate are a scripted scenario, not a
    measurement; and the server's speed: simulated latency = base +
    prefill_ms_per_token * prompt_tokens + decode_ms_per_token *
    completion_tokens (defaults 5 / 2 / 80 ms, i.e. 12.5 tok/s decode, the
    middle of the 8-15 tok/s the repo records for a local 27B).

The stub always answers with the case's reference label when the constraint
allows it, so ACCURACY of a real model is NOT measured; only legality, tokens
and time are. Real-model numbers are pending (a model lease is needed).

Usage::

    python scripts/constrained_choice_bench.py                  # all modes
    python scripts/constrained_choice_bench.py --json out.json --markdown out.md
    python scripts/constrained_choice_bench.py --serve 8099 --mode llamacpp   # stub only
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import socket
import statistics
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# ---------------------------------------------------------------------------
# The inputs: 24 research questions with a reference category
# ---------------------------------------------------------------------------

CASES: List[Tuple[str, str]] = [
    ("What is the best budget mechanical keyboard for programming in 2026?", "product"),
    ("Mejores auriculares inalámbricos con cancelación de ruido por menos de 150 euros", "product"),
    ("Which robot vacuum should I buy for a home with two cats?", "product"),
    ("Recomiéndame una cámara mirrorless para empezar en vídeo", "product"),
    ("Best standing desk under 400 dollars", "product"),
    ("Qué portátil comprar para entrenar modelos pequeños de IA", "product"),
    ("Postgres vs MySQL for a write-heavy analytics workload", "comparison"),
    ("Comparativa entre Vue y React para un equipo pequeño", "comparison"),
    ("Compare Notion, Obsidian and Logseq for research notes", "comparison"),
    ("Diferencias entre llama.cpp y Ollama para servir modelos locales", "comparison"),
    ("AWS Lambda versus Fargate for bursty APIs", "comparison"),
    ("How do I set up a reverse proxy with Caddy and automatic HTTPS?", "howto"),
    ("Cómo migrar una base de datos SQLite a PostgreSQL sin perder datos", "howto"),
    ("Steps to train a LoRA adapter on a single 24 GB GPU", "howto"),
    ("Cómo configurar copias de seguridad cifradas con restic", "howto"),
    ("How to rotate API keys without downtime", "howto"),
    ("Is it true that humans only use 10 percent of their brains?", "factcheck"),
    ("¿Es verdad que el café deshidrata?", "factcheck"),
    ("Does intermittent fasting actually extend lifespan?", "factcheck"),
    ("Es cierto que los móviles provocan cáncer", "factcheck"),
    ("Did the 2008 crisis really start because of subprime mortgages alone?", "factcheck"),
    ("History of the Roman Empire's decline", "general"),
    ("Qué es la entropía en termodinámica", "general"),
    ("Overview of transformer attention variants", "general"),
]

#: What an unconstrained model is scripted to say, cycling over the cases.
STYLE_CYCLE = ("clean", "preamble", "clean", "verbose", "clean", "invented")
_INVENTED = {"product": "shopping", "comparison": "versus", "howto": "tutorial",
             "factcheck": "verification", "general": "overview"}

MODES = ("llamacpp", "ollama", "plain")


def style_for(index: int, styles: Tuple[str, ...] = STYLE_CYCLE) -> str:
    return styles[index % len(styles)]


def free_text_reply(label: str, style: str) -> str:
    if style == "clean":
        return label
    if style == "preamble":
        return f"The category is {label}."
    if style == "verbose":
        return (f"I would classify this question as **{label}**, because it mainly asks for "
                f"information of that kind rather than anything else.")
    if style == "invented":
        return _INVENTED.get(label, "other")
    return label


# ---------------------------------------------------------------------------
# Token heuristic (the codebase's own)
# ---------------------------------------------------------------------------


def est_prompt_tokens(messages: List[Dict[str, Any]]) -> int:
    try:
        from src.model_context import estimate_tokens
        return int(estimate_tokens(messages))
    except Exception:  # noqa: BLE001 - same formula, standalone
        return int(sum(4 + len(str(m.get("content") or "")) * 0.3 for m in messages))


def est_completion_tokens(text: str) -> int:
    return max(1, int(len(text) * 0.3)) if text else 0


# ---------------------------------------------------------------------------
# The stub server
# ---------------------------------------------------------------------------

_GBNF_LITERAL = re.compile(r'"((?:[^"\\]|\\.)*)"')


def _unescape_gbnf(raw: str) -> str:
    out: List[str] = []
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        nxt = raw[i + 1] if i + 1 < len(raw) else ""
        if nxt == "x":
            out.append(chr(int(raw[i + 2:i + 4], 16)))
            i += 4
        else:
            out.append({"n": "\n", "r": "\r", "t": "\t"}.get(nxt, nxt))
            i += 2
    return "".join(out)


def grammar_labels(grammar: str) -> List[str]:
    """The labels of a ``root ::= "a" | "b"`` grammar (the only shape the
    product sends), parsed independently of the builder under test."""
    body = grammar.split("::=", 1)[1] if "::=" in grammar else grammar
    return [_unescape_gbnf(m) for m in _GBNF_LITERAL.findall(body)]


class StubModel:
    """A fake model server. ``mode``: ``llamacpp`` (honours ``grammar``,
    answers ``/props``), ``ollama`` (native ``/api/chat``, honours ``format``),
    ``plain`` (chat-completions style, ignores unknown fields), ``strict``
    (chat-completions style, 400 on a ``grammar`` field), ``ollama_strict``
    (native Ollama that answers 400 to a ``format``)."""

    def __init__(self, mode: str = "llamacpp", *, base_ms: float = 5.0, prefill_ms: float = 2.0,
                 decode_ms: float = 80.0, styles: Tuple[str, ...] = STYLE_CYCLE,
                 simulate: bool = True, log_path: Optional[str] = None) -> None:
        self.mode = mode
        self.log_path = log_path
        self.base_ms, self.prefill_ms, self.decode_ms = base_ms, prefill_ms, decode_ms
        self.styles = styles
        self.simulate = simulate
        self.log: List[Dict[str, Any]] = []
        self.probes: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        self._questions = {q: (i, label) for i, (q, label) in enumerate(CASES)}

    # -- request handling ---------------------------------------------------
    def _note(self, entry: Dict[str, Any]) -> None:
        # Chat requests go to `log`; the probes a client makes first (GET) are
        # kept apart in `probes` so request counts stay about the model calls.
        with self._lock:
            (self.probes if entry.get("method") == "GET" else self.log).append(entry)
            if self.log_path:
                with open(self.log_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"t": time.time(), **entry}, ensure_ascii=False) + "\n")

    def get(self, path: str) -> Tuple[int, Any]:
        path = path.split("?", 1)[0]
        self._note({"path": path, "method": "GET", "constraint": "none"})
        if path == "/props" and self.mode == "llamacpp":
            return 200, {"default_generation_settings": {"n_ctx": 8192}}
        if path == "/health":
            return 200, {"status": "ok"}
        if path == "/api/version" and self.mode.startswith("ollama"):
            return 200, {"version": "0.0-stub"}
        if path in ("/v1/models", "/models"):
            return 200, {"data": [{"id": "stub-model"}]}
        if path == "/api/tags":
            return 200, {"models": [{"name": "stub-model"}]}
        return 404, {"error": "not found"}

    def post(self, path: str, body: bytes) -> Tuple[int, Any]:
        path = path.split("?", 1)[0]
        if path == "/api/show":
            return 200, {"capabilities": ["completion"], "details": {}}
        if not (path.endswith("/chat/completions") or path == "/api/chat"):
            return 404, {"error": "not found"}
        payload = json.loads(body.decode("utf-8"))
        messages = payload.get("messages") or []
        users = [m for m in messages if m.get("role") == "user"]
        user_text = str((users[-1] if users else {}).get("content") or "")
        prompt_tokens = est_prompt_tokens(messages)
        grammar = payload.get("grammar")
        fmt = payload.get("format")
        if grammar is not None and self.mode == "strict":
            return 400, {"error": {"message": "Unrecognized request argument: grammar"}}
        if fmt is not None and self.mode == "ollama_strict":
            return 400, {"error": "invalid format"}

        allowed: Optional[List[str]] = None
        if grammar is not None and self.mode == "llamacpp":
            allowed = grammar_labels(str(grammar))
        elif isinstance(fmt, dict) and self.mode == "ollama":
            try:
                allowed = list(fmt["enum"])
            except (KeyError, TypeError):
                allowed = None

        m = re.search(r"Question:\s*(.*?)\n\n", user_text + "\n\n", re.DOTALL)
        question = m.group(1).strip() if m else ""
        case = self._questions.get(question)
        is_repair = "not one of the allowed options" in user_text
        if case is None:
            index, label, text_style = -1, "general", "clean"
            generic = "OK."
        else:
            index, label = case
            text_style = "clean" if is_repair else style_for(index, self.styles)
            generic = None

        if allowed is not None:
            pick = label if label in allowed else ("general" if "general" in allowed else allowed[0])
            text = json.dumps(pick, ensure_ascii=False) if (self.mode == "ollama") else pick
        else:
            text = generic if generic is not None else free_text_reply(label, text_style)

        max_tokens = payload.get("max_tokens")
        opts = payload.get("options") or {}
        if max_tokens is None:
            max_tokens = opts.get("num_predict")
        finish = "stop"
        if isinstance(max_tokens, int) and max_tokens > 0 and est_completion_tokens(text) > max_tokens:
            text = text[: int(max_tokens / 0.3)]
            finish = "length"
        completion_tokens = est_completion_tokens(text)
        delay_ms = self.base_ms + self.prefill_ms * prompt_tokens + self.decode_ms * completion_tokens
        if self.simulate:
            time.sleep(delay_ms / 1000.0)
        entry = {
            "path": path, "mode": self.mode, "case": index, "style": text_style if case else "n/a",
            "constraint": "grammar" if grammar is not None else ("format" if fmt is not None else "none"),
            "grammar": grammar, "format": fmt, "honoured": allowed is not None,
            "max_tokens": max_tokens, "request_bytes": len(body), "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens, "delay_ms": round(delay_ms, 1),
            "reply": text, "is_repair": is_repair, "think_off": bool(
                (payload.get("chat_template_kwargs") or {}).get("enable_thinking") is False
                or payload.get("think") is False),
        }
        self._note(entry)
        if path == "/api/chat":
            return 200, {"model": payload.get("model"), "done": True, "done_reason": finish,
                         "message": {"role": "assistant", "content": text},
                         "prompt_eval_count": prompt_tokens, "eval_count": completion_tokens}
        return 200, {"id": "stub", "object": "chat.completion", "model": payload.get("model"),
                     "choices": [{"index": 0, "finish_reason": finish,
                                  "message": {"role": "assistant", "content": text}}],
                     "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                               "total_tokens": prompt_tokens + completion_tokens}}


class _Handler(BaseHTTPRequestHandler):
    stub: StubModel = None  # type: ignore[assignment]

    def log_message(self, *args: Any) -> None:  # silence
        return

    def _send(self, status: int, obj: Any) -> None:
        raw = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        self._send(*self.stub.get(self.path))

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b"{}"
        try:
            self._send(*self.stub.post(self.path, body))
        except Exception as exc:  # noqa: BLE001
            self._send(500, {"error": str(exc)})


def start_stub(stub: StubModel, port: int = 0) -> Tuple[ThreadingHTTPServer, int]:
    handler = type("Handler", (_Handler,), {"stub": stub})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="constrained-choice-stub", daemon=True).start()
    return server, server.server_address[1]


def endpoint_url(mode: str, port: int) -> str:
    return f"http://127.0.0.1:{port}/api/chat" if mode.startswith("ollama") else \
        f"http://127.0.0.1:{port}/v1/chat/completions"


# ---------------------------------------------------------------------------
# The measurement
# ---------------------------------------------------------------------------


def _pct(values: List[float], q: float) -> float:
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))], 1) if ordered else 0.0


def _summary(rows: List[Dict[str, Any]], options: List[str]) -> Dict[str, Any]:
    n = len(rows)
    ms = [r["ms"] for r in rows]
    raw_exact = sum(1 for r in rows if r["raw_replies"] and all(x in options for x in r["raw_replies"][-1:]))
    return {
        "cases": n,
        "requests": sum(r["requests"] for r in rows),
        "prompt_tokens": sum(r["prompt_tokens"] for r in rows),
        "completion_tokens": sum(r["completion_tokens"] for r in rows),
        "request_bytes": sum(r["request_bytes"] for r in rows),
        "ms_total": round(sum(ms), 1), "ms_mean": round(statistics.mean(ms), 1) if ms else 0.0,
        "ms_p50": _pct(ms, 0.5), "ms_p95": _pct(ms, 0.95),
        "final_in_set": sum(1 for r in rows if r["category"] is None or r["category"] in options),
        "final_none": sum(1 for r in rows if r["category"] is None),
        "final_matches_reference": sum(1 for r in rows if r["category"] == r["reference_category"]),
        "last_raw_reply_exactly_an_option": raw_exact,
        "raw_out_of_set": sum(1 for r in rows if r["raw_replies"] and r["raw_replies"][-1] not in options),
    }


async def _measure_mode(mode: str, args: argparse.Namespace) -> Dict[str, Any]:
    from src.deep_research import CATEGORY_PROMPTS, DeepResearcher

    stub = StubModel(mode, base_ms=args.base_ms, prefill_ms=args.prefill_ms, decode_ms=args.decode_ms,
                     styles=tuple(args.styles.split(",")))
    server, port = start_stub(stub)
    url = endpoint_url(mode, port)
    options = list(CATEGORY_PROMPTS) + [DeepResearcher.CATEGORY_NONE_LABEL]
    out: Dict[str, Any] = {"mode": mode, "endpoint": url, "paths": {}, "rows": {}}
    try:
        for label, method in (("previous_free_text", "_classify_category_free_text"),
                              ("constrained", "_classify_category")):
            rows: List[Dict[str, Any]] = []
            for index, (question, reference) in enumerate(CASES):
                r = DeepResearcher.__new__(DeepResearcher)
                r.llm_endpoint, r.llm_model, r.llm_headers = url, "stub-model", {}
                r._failures, r._think_overrides, r._read_overrides = [], None, None
                before = len(stub.log)
                t0 = time.perf_counter()
                category = await getattr(r, method)(question)
                ms = (time.perf_counter() - t0) * 1000
                calls = [e for e in stub.log[before:] if e["path"] != "/api/show"]
                raw: List[str] = []
                for e in calls:
                    text = e["reply"]
                    if e["path"] == "/api/chat" and e["constraint"] == "format":
                        try:
                            text = json.loads(text)
                        except (ValueError, KeyError, TypeError):
                            pass
                    raw.append(text)
                rows.append({
                    "index": index, "question": question, "style": style_for(index, tuple(args.styles.split(","))),
                    "reference_category": None if reference == DeepResearcher.CATEGORY_NONE_LABEL else reference,
                    "category": category, "ms": ms, "requests": len(calls),
                    "prompt_tokens": sum(e["prompt_tokens"] for e in calls),
                    "completion_tokens": sum(e["completion_tokens"] for e in calls),
                    "request_bytes": sum(e["request_bytes"] for e in calls),
                    "constraints": [e["constraint"] for e in calls],
                    "raw_replies": raw,
                    "decision": getattr(r, "category_decision", None),
                })
            out["paths"][label] = _summary(rows, options)
            out["rows"][label] = rows
    finally:
        server.shutdown()
    out["stub_log_sample"] = [{k: v for k, v in e.items() if k not in ("grammar", "format")}
                              for e in stub.log[:6]]
    first_constrained = next((e for e in stub.log if e["constraint"] != "none"), None)
    out["constraint_sample"] = ({"constraint": first_constrained["constraint"],
                                 "grammar": first_constrained["grammar"],
                                 "format": first_constrained["format"]} if first_constrained else None)
    return out


def _delta(old: Dict[str, Any], new: Dict[str, Any], key: str) -> Dict[str, Any]:
    a, b = old[key], new[key]
    return {"previous": a, "constrained": b, "saved": round(a - b, 1),
            "saved_pct": round((a - b) / a * 100, 1) if a else 0.0}


def run(args: argparse.Namespace) -> Dict[str, Any]:
    os.environ.setdefault("FAUSTUS_DATA_DIR", tempfile.mkdtemp(prefix="cc-bench-"))
    os.environ.setdefault("ODYSSEUS_DATA_DIR", os.environ["FAUSTUS_DATA_DIR"])
    report: Dict[str, Any] = {
        "assumptions": {
            "model_loaded": False,
            "unconstrained_replies": "scripted by --styles cycle " + args.styles,
            "simulated_latency_ms": {"base": args.base_ms, "per_prompt_token": args.prefill_ms,
                                     "per_completion_token": args.decode_ms},
            "token_heuristic": "src.model_context.estimate_tokens: 0.3 per char + 4 per message; "
                               "completion = max(1, int(0.3 * chars))",
            "accuracy_measured": False,
        },
        "cases": len(CASES), "modes": {},
    }
    for mode in args.modes:
        res = asyncio.run(_measure_mode(mode, args))
        old, new = res["paths"]["previous_free_text"], res["paths"]["constrained"]
        res["comparison"] = {k: _delta(old, new, k) for k in
                             ("prompt_tokens", "completion_tokens", "ms_total", "ms_mean", "request_bytes",
                              "requests")}
        report["modes"][mode] = res
    return report


def markdown(report: Dict[str, Any]) -> str:
    a = report["assumptions"]
    lat = a["simulated_latency_ms"]
    lines = [
        f"Stub measurement, {report['cases']} cases, NO model loaded. Simulated latency = "
        f"{lat['base']} ms + {lat['per_prompt_token']} ms/prompt token + {lat['per_completion_token']} ms/"
        f"completion token. Unconstrained replies are scripted ({a['unconstrained_replies']}).",
        "",
        "| backend | path | requests | prompt tok | completion tok | request bytes | ms total | ms p50 | ms p95 | "
        "final in set | final None | last raw reply out of set |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for mode, res in report["modes"].items():
        for label, s in res["paths"].items():
            lines.append(f"| {mode} | {label} | {s['requests']} | {s['prompt_tokens']} | {s['completion_tokens']} | "
                         f"{s['request_bytes']} | {s['ms_total']} | {s['ms_p50']} | {s['ms_p95']} | "
                         f"{s['final_in_set']}/{s['cases']} | {s['final_none']} | {s['raw_out_of_set']} |")
    lines += ["", "| backend | completion tokens saved | prompt tokens saved | ms saved (total) | ms saved (%) |",
              "|---|---:|---:|---:|---:|"]
    for mode, res in report["modes"].items():
        c = res["comparison"]
        lines.append(f"| {mode} | {c['completion_tokens']['saved']} ({c['completion_tokens']['saved_pct']}%) | "
                     f"{c['prompt_tokens']['saved']} ({c['prompt_tokens']['saved_pct']}%) | "
                     f"{c['ms_total']['saved']} | {c['ms_total']['saved_pct']}% |")
    return "\n".join(lines) + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--modes", nargs="+", default=list(MODES), choices=list(MODES) + ["strict", "ollama_strict"])
    ap.add_argument("--base-ms", type=float, default=5.0)
    ap.add_argument("--prefill-ms", type=float, default=2.0, help="simulated ms per prompt token")
    ap.add_argument("--decode-ms", type=float, default=80.0, help="simulated ms per completion token")
    ap.add_argument("--styles", default=",".join(STYLE_CYCLE),
                    help="how the scripted UNCONSTRAINED model answers, cycling over the cases: "
                         "clean | preamble | verbose | invented")
    ap.add_argument("--json", help="write the full report here")
    ap.add_argument("--markdown", help="write the summary tables here")
    ap.add_argument("--serve", type=int, metavar="PORT", help="only run the stub on PORT until Ctrl+C")
    ap.add_argument("--mode", default="llamacpp", choices=list(MODES) + ["strict"], help="stub mode for --serve")
    ap.add_argument("--log", help="with --serve: append every request as a JSON line to this file")
    args = ap.parse_args(argv)
    if args.serve is not None:
        stub = StubModel(args.mode, base_ms=args.base_ms, prefill_ms=args.prefill_ms, decode_ms=args.decode_ms,
                         log_path=args.log)
        server, port = start_stub(stub, args.serve)
        print(f"stub '{args.mode}' on {endpoint_url(args.mode, port)}", flush=True)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            server.shutdown()
        return 0
    report = run(args)
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    text = markdown(report)
    if args.markdown:
        Path(args.markdown).write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
