"""Exercise the real local decision adapter; never calls a hosted model.

Small Spanish/English fixture set, diagnostic only. Saves timings, coverage
and errors so a fast but wrong classifier is not enabled on speed alone.
"""
import asyncio
import argparse
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src import typed_decision as td
from src.freshness import FRESHNESS_QUESTION

CASES = [
    ("¿Quién es el presidente de Francia hoy?", "yes"),
    ("¿Qué tiempo hará mañana en Madrid?", "yes"),
    ("Busca las últimas noticias de tecnología.", "yes"),
    ("¿Cuánto cuesta ahora una RTX 5090?", "yes"),
    ("¿Cuál es la versión más reciente de Python?", "yes"),
    ("¿Quién ganó el partido de ayer?", "yes"),
    ("¿Sigue abierto este restaurante?", "yes"),
    ("Dame el tipo de cambio de hoy.", "yes"),
    ("What is the weather tomorrow in Madrid?", "yes"),
    ("Who is the current CEO of this company?", "yes"),
    ("Explica el teorema de Pitágoras.", "no"),
    ("Traduce al inglés: mañana iré al mercado.", "no"),
    ("Escribe un poema sobre la lluvia.", "no"),
    ("Calcula 12 por 15.", "no"),
    ("Ordena esta lista: 5, 1, 3.", "no"),
    ("Resume este texto: el gato duerme en el sofá.", "no"),
    ("Corrige las faltas de esta frase.", "no"),
    ("¿Cómo funciona una búsqueda binaria?", "no"),
    ("Translate this sentence into Spanish: Good morning.", "no"),
    ("Write a Python function to reverse a list.", "no"),
]

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="laya:multilingual")
    parser.add_argument("--output", default="local-decisions-benchmark-2026-10-03.json")
    args = parser.parse_args()
    td._setting = lambda key: {**td.DEFAULTS, "typed_decision_provider": "ollaya",
        "typed_decision_ollaya_model": args.model, "typed_decision_timeout_ms": 4000}.get(key)
    results = []
    for text, expected in CASES:
        d = (await td.decide(text, [td.Field("needs_web", FRESHNESS_QUESTION, "bool")]))["needs_web"]
        results.append({"text": text, "expected": expected, "value": d.value,
            "best": d.best, "confidence": d.confidence, "ms": round(d.ms, 2), "reason": d.reason})
    known = [r for r in results if r["value"] is not None]
    report = {"model": args.model, "device": "cpu", "cases": len(results),
        "known": len(known), "correct": sum(r["value"] == r["expected"] for r in results),
        "wrong_high_confidence": sum(r["value"] != r["expected"] for r in known),
        "median_ms": round(statistics.median(r["ms"] for r in results), 2), "results": results}
    path = ROOT / "docs" / "adaptations" / Path(args.output).name
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "results"}, ensure_ascii=False))

if __name__ == "__main__":
    asyncio.run(main())
