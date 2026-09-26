#!/usr/bin/env python3
"""typos_ab.py — count invented Spanish words per sampler preset.

The local 27B sometimes writes words that do not exist in Spanish
("desarollaron", "enteriormente"). Quantisation is an unlikely cause (Q8_0
keeps ~99 % of BF16); sampling is the likely lever. This script sends the same
fixed batch of Castilian prose prompts to an OpenAI-compatible endpoint under
several sampler presets and counts words that no Spanish frequency list knows,
per 1,000 words. No model judges a model.

    python scripts/typos_ab.py --endpoint http://127.0.0.1:8081/v1 \\
        --model qwen3.8-27b-q8-llamacpp --repeats 2
    python scripts/typos_ab.py --preset 'warm={"temperature":0.8,"min_p":0.05}'
    python scripts/typos_ab.py --think           # with reasoning on (slower)

Needs `wordfreq` (pip install wordfreq): its large Spanish list covers
inflected forms ("hicimos", "déjamelo") that plain dictionaries miss. The
report goes to logs/typos_ab/<timestamp>.json and .md (or --out PREFIX).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

REPO = Path(__file__).resolve().parents[1]

#: Qwen3's model card for thinking mode, then the two variants worth testing.
PRESETS: Dict[str, Dict[str, Any]] = {
    "card": {"temperature": 0.6, "top_p": 0.95, "top_k": 20, "min_p": 0.0},
    "minp01": {"temperature": 0.6, "top_p": 0.95, "top_k": 20, "min_p": 0.1},
    "cool": {"temperature": 0.35, "top_p": 0.9, "top_k": 20, "min_p": 0.1},
}

PROMPTS: List[str] = [
    "Escribe unos 300 palabras explicando a un vecino jubilado cómo funciona la factura de la luz en España: potencia contratada, energía consumida y peajes.",
    "Redacta una carta de unas 300 palabras a la comunidad de propietarios proponiendo instalar placas solares en la azotea, con ventajas, costes aproximados y dudas que habría que resolver.",
    "Cuenta en unas 300 palabras la historia de un panadero de Valladolid que decide abrir un obrador de masa madre, con diálogos breves.",
    "Explica en unas 300 palabras, para un estudiante de bachillerato, qué es la fotosíntesis y por qué importa para el clima.",
    "Escribe unas 300 palabras de consejos prácticos para preparar una entrevista de trabajo en una empresa de logística.",
    "Describe en unas 300 palabras un paseo de otoño por el Retiro de Madrid, con detalles de colores, sonidos y olores.",
    "Resume en unas 300 palabras las ventajas y los inconvenientes del teletrabajo para una empresa pequeña.",
    "Escribe unas 300 palabras explicando cómo se hace una paella valenciana tradicional y los errores más comunes.",
]

_WORD = re.compile(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+(?:-[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+)*")
_CODE = re.compile(r"```.*?```|`[^`]*`", re.S)
_URL = re.compile(r"https?://\S+|www\.\S+")
_SENTENCE_START = re.compile(r"(^|[.!?¡¿:;\n]\s*)$")


def words_to_check(text: str) -> List[str]:
    """The words of `text` a Spanish list should know, lower-cased.

    Code spans, URLs and words shorter than three letters are left out, and so
    is a capitalised word that does not open a sentence: that is a name, and
    a name missing from a word list is not a typo."""
    text = _URL.sub(" ", _CODE.sub(" ", text or ""))
    out: List[str] = []
    for m in _WORD.finditer(text):
        word = m.group(0)
        if len(word) < 3:
            continue
        if word[0].isupper():
            before = text[max(0, m.start() - 3):m.start()]
            if not _SENTENCE_START.search(before) and m.start() != 0:
                continue
            if word.isupper() and len(word) > 1:
                continue  # an acronym
        out.append(word.lower())
    return out


def unknown_words(text: str, known: Callable[[str], bool]) -> Tuple[int, List[str]]:
    """(words checked, the ones `known` rejects), for one answer. A hyphenated
    compound counts as known when every part is."""
    words = words_to_check(text)
    bad = [w for w in words if not (known(w) or ("-" in w and all(known(p) for p in w.split("-") if p)))]
    return len(words), bad


def wordfreq_known(lang: str = "es") -> Callable[[str], bool]:
    try:
        from wordfreq import zipf_frequency
    except ImportError as exc:  # pragma: no cover - reported to the operator
        raise SystemExit("typos_ab needs wordfreq: pip install wordfreq") from exc
    cache: Dict[str, bool] = {}

    def known(word: str) -> bool:
        if word not in cache:
            cache[word] = zipf_frequency(word, lang, wordlist="large") > 0
        return cache[word]

    return known


def parse_preset(raw: str) -> Tuple[str, Dict[str, Any]]:
    name, _, body = raw.partition("=")
    if not name or not body:
        raise argparse.ArgumentTypeError("--preset takes name={json}")
    value = json.loads(body)
    if not isinstance(value, dict):
        raise argparse.ArgumentTypeError("--preset body must be a JSON object")
    return name.strip(), value


def ask(endpoint: str, model: str, prompt: str, sampler: Dict[str, Any], *, think: bool,
        max_tokens: int, timeout: float) -> Dict[str, Any]:
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "stream": False,
        "chat_template_kwargs": {"enable_thinking": bool(think)},
        **sampler,
    }
    req = urllib.request.Request(endpoint.rstrip("/") + "/chat/completions",
                                 data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    message = (data.get("choices") or [{}])[0].get("message") or {}
    usage = data.get("usage") or {}
    return {"text": message.get("content") or "", "seconds": round(time.time() - t0, 1),
            "completion_tokens": usage.get("completion_tokens")}


def summarise(runs: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    runs = list(runs)
    words = sum(r["words"] for r in runs)
    bad: List[str] = [w for r in runs for w in r["unknown"]]
    counts: Dict[str, int] = {}
    for w in bad:
        counts[w] = counts.get(w, 0) + 1
    tokens = sum(r.get("completion_tokens") or 0 for r in runs)
    seconds = sum(r.get("seconds") or 0 for r in runs)
    return {
        "answers": len(runs), "words": words, "unknown": len(bad),
        "per_1000": round(1000 * len(bad) / words, 2) if words else None,
        "top_unknown": sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:25],
        "tokens_per_s": round(tokens / seconds, 1) if seconds and tokens else None,
        "errors": sum(1 for r in runs if r.get("error")),
    }


def render_md(report: Dict[str, Any]) -> str:
    lines = [f"# typos_ab {report['started']}", "",
             f"Model `{report['model']}` at {report['endpoint']}, thinking {'on' if report['think'] else 'off'}, "
             f"{report['repeats']} repeat(s) of {len(PROMPTS)} prompts.", "",
             "| preset | sampler | words | unknown | per 1,000 | tok/s | errors |",
             "| --- | --- | ---: | ---: | ---: | ---: | ---: |"]
    for name, s in report["summary"].items():
        lines.append(f"| {name} | `{json.dumps(report['presets'][name])}` | {s['words']} | {s['unknown']} | "
                     f"{s['per_1000']} | {s['tokens_per_s']} | {s['errors']} |")
    lines.append("")
    for name, s in report["summary"].items():
        top = ", ".join(f"{w} ×{n}" for w, n in s["top_unknown"]) or "none"
        lines.append(f"- **{name}** unknown words: {top}")
    lines.append("")
    lines.append("Unknown means absent from the wordfreq Spanish list: most are invented words, "
                 "some are rare but real. Read the list before trusting the rate.")
    return "\n".join(lines) + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--endpoint", default="http://127.0.0.1:8081/v1")
    ap.add_argument("--model", default="qwen3.8-27b-q8-llamacpp")
    ap.add_argument("--preset", action="append", type=parse_preset, default=[],
                    help="name={json sampler}; repeatable. Default: the built-in presets.")
    ap.add_argument("--only", default="", help="comma-separated preset names to run")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--think", action="store_true")
    ap.add_argument("--max-tokens", type=int, default=900)
    ap.add_argument("--timeout", type=float, default=600)
    ap.add_argument("--out", default="")
    args = ap.parse_args(argv)

    presets = dict(args.preset) if args.preset else dict(PRESETS)
    if args.only:
        wanted = {n.strip() for n in args.only.split(",") if n.strip()}
        presets = {k: v for k, v in presets.items() if k in wanted}
    known = wordfreq_known()
    started = time.strftime("%Y%m%d-%H%M%S")
    report: Dict[str, Any] = {"started": started, "endpoint": args.endpoint, "model": args.model,
                              "think": args.think, "repeats": args.repeats, "presets": presets,
                              "runs": {}, "summary": {}}
    # Interleave presets per prompt so a slow drift of the server (thermal,
    # another client) spreads over every preset instead of landing on one.
    for rep in range(args.repeats):
        for i, prompt in enumerate(PROMPTS):
            for name, sampler in presets.items():
                try:
                    res = ask(args.endpoint, args.model, prompt, sampler, think=args.think,
                              max_tokens=args.max_tokens, timeout=args.timeout)
                    n, bad = unknown_words(res["text"], known)
                    run = {"prompt": i, "repeat": rep, "words": n, "unknown": bad, **res}
                except Exception as exc:  # noqa: BLE001 - one failed call is data, not the end
                    run = {"prompt": i, "repeat": rep, "words": 0, "unknown": [], "error": str(exc)[:300]}
                report["runs"].setdefault(name, []).append(run)
                print(f"[{name}] prompt {i} rep {rep}: {run['words']} words, "
                      f"{len(run['unknown'])} unknown {run.get('error') or ''}", flush=True)
    report["summary"] = {name: summarise(runs) for name, runs in report["runs"].items()}
    prefix = Path(args.out) if args.out else REPO / "logs" / "typos_ab" / started
    prefix.parent.mkdir(parents=True, exist_ok=True)
    prefix.with_suffix(".json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    prefix.with_suffix(".md").write_text(render_md(report), encoding="utf-8")
    print(render_md(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
