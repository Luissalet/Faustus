"""Text-stage voice revision evaluation; each turn uses the actual previous draft."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.eval_natural_dictation import matches_expected
from services.stt.natural_dictation import polish

INITIAL = "Hola Ana. Nos vemos el martes a las seis. Lleva 3/4 de litro de leche."
TURNS = [
    ("Cambia martes por jueves y deja lo demás igual.",
     "Hola Ana. Nos vemos el jueves a las seis. Lleva 3/4 de litro de leche."),
    ("No, mejor el viernes. Conserva la hora y lo demás.",
     "Hola Ana. Nos vemos el viernes a las seis. Lleva 3/4 de litro de leche."),
    ("Pon un párrafo nuevo después de Hola Ana. No cambies las palabras.",
     "Hola Ana.\n\nNos vemos el viernes a las seis. Lleva 3/4 de litro de leche."),
    ("Cambia seis por siete. Mantén los párrafos y todo lo demás.",
     "Hola Ana.\n\nNos vemos el viernes a las siete. Lleva 3/4 de litro de leche."),
    ("Quita solo la última frase y conserva el resto tal cual.",
     "Hola Ana.\n\nNos vemos el viernes a las siete."),
    ("Añade al final un nuevo párrafo que diga: No envíes el mensaje todavía.",
     "Hola Ana.\n\nNos vemos el viernes a las siete.\n\nNo envíes el mensaje todavía."),
]


async def evaluate(output: Path, url: str, model: str) -> bool:
    draft = INITIAL
    results = []
    for instruction, expected in TURNS:
        started = time.perf_counter()
        value = await polish(instruction, "revise", draft,
                             resolve=lambda *a, **k: (url, model, {}))
        row = {"turn": len(results) + 1, "draft_before": draft,
               "instruction": instruction, "expected": expected, **value,
               "match": matches_expected(value["text"], [expected]),
               "seconds": round(time.perf_counter() - started, 3)}
        results.append(row)
        draft = value["text"]
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps({"stage": "text_only_revision_chain",
            "model": model, "results": results}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(row, ensure_ascii=True), flush=True)
    return all(row["match"] and row["status"] != "fallback" for row in results)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--url", default="http://127.0.0.1:8081/v1/chat/completions")
    parser.add_argument("--model", default="qwen3.8-27b-q8-llamacpp")
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(evaluate(args.output, args.url, args.model)) else 1)
