#!/usr/bin/env python3
"""preview_research_perspectives.py — run ONLY the planning step of deep
research (src/deep_research.py) and print the perspective-labeled questions
it produced, with no web search and no synthesis.

Talks to a local OpenAI-compatible chat endpoint (llama.cpp server, Ollama,
LM Studio, ...) — nothing else is touched. Works unmodified on Windows,
macOS or Linux; run it with the project's own Python (the venv that has
`aiohttp`/`httpx` installed, whatever `src.llm_core` needs).

Example (Windows, local llama.cpp server on 127.0.0.1:8082, model alias
"qwen2.5-3b-helper" — the model this project's free/local tier expects):

    python scripts\\preview_research_perspectives.py ^
        --endpoint http://127.0.0.1:8082/v1 ^
        --model qwen2.5-3b-helper ^
        --question "Should this city switch its bus fleet to electric?"

Or import `preview_perspectives` directly and call it from another script:

    import asyncio
    from scripts.preview_research_perspectives import preview_perspectives

    asyncio.run(preview_perspectives(
        question="Should this city switch its bus fleet to electric?",
        endpoint="http://127.0.0.1:8082/v1",
        model="qwen2.5-3b-helper",
    ))
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.deep_research import DeepResearcher  # noqa: E402


async def preview_perspectives(question: str, endpoint: str, model: str,
                                max_perspectives: int = 3,
                                timeout: int = 60) -> DeepResearcher:
    """Run `_create_plan` alone — no `_search_and_extract`, no synthesis,
    no `bg_jobs` slot held — and return the `DeepResearcher` with
    `.perspective_plan` and `.subquestions` filled in.

    This is the "planning step only" entry point: it calls exactly the two
    model requests `_create_plan` can make (the base plan, then perspectives
    if the perspectives call returns something usable) against `endpoint`.
    """
    researcher = DeepResearcher(
        llm_endpoint=endpoint,
        llm_model=model,
        planning_timeout=timeout,
        research_perspectives=True,
        research_perspectives_max=max_perspectives,
    )
    researcher._progress = lambda event: None  # no UI to report to here
    await researcher._create_plan(question)
    return researcher


def _print_plan(researcher: DeepResearcher, question: str) -> None:
    print(f"\nQuestion: {question}\n")
    plan = researcher.perspective_plan
    if not plan:
        print("No perspective plan produced (feature off, model call failed, "
              "or the model's output didn't parse) — falling back to the flat "
              "sub-questions:")
        for i, q in enumerate(researcher.subquestions, 1):
            print(f"  {i}. [general] {q}")
        return

    by_perspective = {}
    for item in plan:
        by_perspective.setdefault(item["perspective"], []).append(item)

    for perspective, items in by_perspective.items():
        focus = next((i.get("focus") for i in items if i.get("focus")), "")
        header = f"[{perspective}]" + (f" — {focus}" if focus else "")
        print(header)
        for item in items:
            print(f"  - {item['question']}")
    print(f"\n{len(plan)} labeled question(s) total "
          f"({len(by_perspective)} perspective(s), including 'general').")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--question", "-q", required=True,
                        help="The research topic/question to plan for.")
    parser.add_argument("--endpoint", default="http://127.0.0.1:8082/v1",
                        help="OpenAI-compatible chat endpoint (default: local llama.cpp on 8082).")
    parser.add_argument("--model", default="qwen2.5-3b-helper",
                        help="Model name/alias as the endpoint expects it.")
    parser.add_argument("--max-perspectives", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=60)
    args = parser.parse_args()

    researcher = asyncio.run(preview_perspectives(
        question=args.question,
        endpoint=args.endpoint,
        model=args.model,
        max_perspectives=args.max_perspectives,
        timeout=args.timeout,
    ))
    _print_plan(researcher, args.question)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
