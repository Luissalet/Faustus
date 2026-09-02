"""Curated, offline catalogue of popular Ollama library models.

Settings → Local models → "Discover" reads this instead of scraping
ollama.com: the library page has no stable API and a browse list that
silently turns empty when the scrape breaks is worse than a short list that
is always there. Sizes are the download size of the default (mostly Q4_K_M)
build of each tag as the library reports it, rounded — they are here so the
fit verdict can be computed BEFORE the pull, and they are labelled
approximate everywhere they are shown. Anything not in this list can still
be pulled by typing its name.

Every entry: name (the library id), family, vendor, a one-line blurb, the
capabilities Ollama reports for the family (``vision`` / ``tools`` /
``thinking`` / ``embedding``) and its tags with parameter size and
approximate gigabytes. ``default_tag`` is what ``ollama pull <name>``
resolves to.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

GIB = 1024 ** 3


def _m(name: str, family: str, vendor: str, blurb: str, caps: List[str],
       tags: List[tuple], default_tag: Optional[str] = None) -> Dict[str, Any]:
    return {
        "name": name,
        "family": family,
        "vendor": vendor,
        "blurb": blurb,
        "capabilities": sorted(set(caps)),
        "default_tag": default_tag or tags[0][0],
        "tags": [{"tag": t, "params": p, "gb": g} for (t, p, g) in tags],
    }


# (tag, parameter size, approx GB on disk)
CATALOG: List[Dict[str, Any]] = [
    # ── Qwen ──
    _m("qwen3.5", "qwen", "Alibaba",
       "Qwen 3.5 — current general-purpose family with tool calling and optional thinking.",
       ["tools", "thinking"],
       [("2b", "2B", 1.6), ("4b", "4B", 2.9), ("9b", "9B", 6.6), ("27b", "27B", 17.0), ("27b-q8_0", "27B", 29.0)],
       default_tag="9b"),
    _m("qwen3", "qwen", "Alibaba",
       "Qwen 3 dense and MoE models; hybrid thinking / non-thinking modes.",
       ["tools", "thinking"],
       [("0.6b", "0.6B", 0.5), ("1.7b", "1.7B", 1.4), ("4b", "4B", 2.6), ("8b", "8B", 5.2),
        ("14b", "14B", 9.3), ("30b", "30B-A3B", 19.0), ("32b", "32B", 20.0), ("235b", "235B-A22B", 142.0)],
       default_tag="8b"),
    _m("qwen3-coder", "qwen", "Alibaba",
       "Agentic coding model (MoE); strong at multi-file edits and tool use.",
       ["tools"],
       [("30b", "30B-A3B", 19.0), ("480b", "480B-A35B", 290.0)]),
    _m("qwen2.5", "qwen", "Alibaba",
       "Qwen 2.5 — reliable instruction models in every size; 128K context.",
       ["tools"],
       [("0.5b", "0.5B", 0.4), ("1.5b", "1.5B", 1.0), ("3b", "3B", 1.9), ("7b", "7B", 4.7),
        ("14b", "14B", 9.0), ("32b", "32B", 20.0), ("72b", "72B", 47.0)],
       default_tag="7b"),
    _m("qwen2.5-coder", "qwen", "Alibaba",
       "Code-specialised Qwen 2.5; fill-in-the-middle and repo-level completion.",
       ["tools"],
       [("0.5b", "0.5B", 0.4), ("1.5b", "1.5B", 1.0), ("3b", "3B", 1.9), ("7b", "7B", 4.7),
        ("14b", "14B", 9.0), ("32b", "32B", 20.0)],
       default_tag="7b"),
    _m("qwen2.5vl", "qwen", "Alibaba",
       "Vision-language Qwen: documents, charts, screenshots, video frames.",
       ["vision"],
       [("3b", "3B", 3.2), ("7b", "7B", 6.0), ("32b", "32B", 21.0), ("72b", "72B", 49.0)],
       default_tag="7b"),
    _m("qwen3-embedding", "qwen", "Alibaba",
       "Multilingual text embeddings from the Qwen 3 family.",
       ["embedding"],
       [("0.6b", "0.6B", 1.2), ("4b", "4B", 2.5), ("8b", "8B", 4.7)]),
    _m("qwq", "qwen", "Alibaba",
       "Reasoning model that thinks out loud before answering.",
       ["tools", "thinking"],
       [("32b", "32B", 20.0)]),
    # ── Meta ──
    _m("llama3.3", "llama", "Meta",
       "Llama 3.3 70B — quality close to the 405B at a fraction of the size.",
       ["tools"],
       [("70b", "70B", 43.0)]),
    _m("llama3.2", "llama", "Meta",
       "Small Llama 3.2 models for edge and quick chat; 128K context.",
       ["tools"],
       [("1b", "1B", 1.3), ("3b", "3B", 2.0)],
       default_tag="3b"),
    _m("llama3.2-vision", "llama", "Meta",
       "Llama 3.2 with image understanding.",
       ["vision"],
       [("11b", "11B", 7.8), ("90b", "90B", 55.0)]),
    _m("llama3.1", "llama", "Meta",
       "Llama 3.1 — the 8B is still a solid default for tools and chat.",
       ["tools"],
       [("8b", "8B", 4.9), ("70b", "70B", 43.0), ("405b", "405B", 243.0)]),
    _m("codellama", "llama", "Meta",
       "Code Llama: completion, infilling and instruct variants.",
       [],
       [("7b", "7B", 3.8), ("13b", "13B", 7.4), ("34b", "34B", 19.0), ("70b", "70B", 39.0)]),
    # ── Google ──
    _m("gemma3", "gemma", "Google",
       "Gemma 3 — multimodal (except 1B), 128K context, 140+ languages.",
       ["vision"],
       [("1b", "1B", 0.8), ("4b", "4B", 3.3), ("12b", "12B", 8.1), ("27b", "27B", 17.0)],
       default_tag="4b"),
    _m("gemma3n", "gemma", "Google",
       "Gemma 3n — designed for everyday devices; effective 2B/4B footprint.",
       [],
       [("e2b", "E2B", 5.6), ("e4b", "E4B", 7.5)],
       default_tag="e4b"),
    _m("embeddinggemma", "gemma", "Google",
       "Compact embedding model for on-device retrieval.",
       ["embedding"],
       [("300m", "300M", 0.6)]),
    # ── DeepSeek ──
    _m("deepseek-r1", "deepseek", "DeepSeek",
       "DeepSeek-R1 reasoning models, including distilled Qwen/Llama variants.",
       ["tools", "thinking"],
       [("1.5b", "1.5B", 1.1), ("7b", "7B", 4.7), ("8b", "8B", 5.2), ("14b", "14B", 9.0),
        ("32b", "32B", 20.0), ("70b", "70B", 43.0), ("671b", "671B", 404.0)],
       default_tag="8b"),
    _m("deepseek-v3", "deepseek", "DeepSeek",
       "DeepSeek-V3 671B MoE — flagship general model.",
       ["tools"],
       [("671b", "671B", 404.0)]),
    _m("deepseek-coder-v2", "deepseek", "DeepSeek",
       "MoE code model; the 16B runs on consumer cards.",
       [],
       [("16b", "16B", 8.9), ("236b", "236B", 133.0)]),
    # ── Mistral ──
    _m("mistral", "mistral", "Mistral AI",
       "Mistral 7B — the classic small instruct model.",
       ["tools"],
       [("7b", "7B", 4.1)]),
    _m("mistral-nemo", "mistral", "Mistral AI",
       "12B built with NVIDIA; 128K context, strong multilingual.",
       ["tools"],
       [("12b", "12B", 7.1)]),
    _m("mistral-small3.2", "mistral", "Mistral AI",
       "Mistral Small 3.2 — 24B with vision and improved instruction following.",
       ["tools", "vision"],
       [("24b", "24B", 15.0)]),
    _m("magistral", "mistral", "Mistral AI",
       "Mistral's reasoning model (24B).",
       ["tools", "thinking"],
       [("24b", "24B", 14.0)]),
    _m("devstral", "mistral", "Mistral AI",
       "Agentic coding model built for software-engineering tasks.",
       ["tools"],
       [("24b", "24B", 14.0)]),
    _m("codestral", "mistral", "Mistral AI",
       "Code generation in 80+ languages.",
       [],
       [("22b", "22B", 13.0)]),
    _m("mixtral", "mistral", "Mistral AI",
       "Sparse mixture-of-experts models.",
       ["tools"],
       [("8x7b", "8x7B", 26.0), ("8x22b", "8x22B", 80.0)]),
    # ── Microsoft ──
    _m("phi4", "phi", "Microsoft",
       "Phi-4 14B — strong reasoning for its size.",
       [],
       [("14b", "14B", 9.1)]),
    _m("phi4-mini", "phi", "Microsoft",
       "Phi-4 mini 3.8B with function calling.",
       ["tools"],
       [("3.8b", "3.8B", 2.5)]),
    _m("phi4-reasoning", "phi", "Microsoft",
       "Phi-4 fine-tuned for step-by-step reasoning.",
       ["thinking"],
       [("14b", "14B", 11.0)]),
    _m("phi3", "phi", "Microsoft",
       "Phi-3 lightweight models.",
       [],
       [("3.8b", "3.8B", 2.2), ("14b", "14B", 7.9)]),
    # ── OpenAI ──
    _m("gpt-oss", "gptoss", "OpenAI",
       "OpenAI's open-weight models; the 20B runs in 16 GB.",
       ["tools", "thinking"],
       [("20b", "20B", 14.0), ("120b", "120B", 65.0)]),
    # ── Vision ──
    _m("llava", "llava", "LLaVA",
       "Original open vision assistant (CLIP + Vicuna).",
       ["vision"],
       [("7b", "7B", 4.7), ("13b", "13B", 8.0), ("34b", "34B", 20.0)]),
    _m("llava-llama3", "llava", "LLaVA",
       "LLaVA fine-tuned from Llama 3 Instruct.",
       ["vision"],
       [("8b", "8B", 5.5)]),
    _m("minicpm-v", "minicpm", "OpenBMB",
       "Compact vision model with good OCR.",
       ["vision"],
       [("8b", "8B", 5.5)]),
    _m("moondream", "moondream", "vikhyat",
       "Tiny vision model for edge devices.",
       ["vision"],
       [("1.8b", "1.8B", 1.7)]),
    # ── IBM / others ──
    _m("granite3.3", "granite", "IBM",
       "Granite 3.3 — enterprise-friendly, 128K context, fill-in-the-middle.",
       ["tools"],
       [("2b", "2B", 1.5), ("8b", "8B", 4.9)],
       default_tag="8b"),
    _m("granite-code", "granite", "IBM",
       "Granite code models.",
       [],
       [("3b", "3B", 2.0), ("8b", "8B", 4.6), ("20b", "20B", 12.0), ("34b", "34B", 19.0)]),
    _m("starcoder2", "starcoder", "BigCode",
       "StarCoder2 code completion.",
       [],
       [("3b", "3B", 1.7), ("7b", "7B", 4.0), ("15b", "15B", 9.1)]),
    _m("command-r", "command-r", "Cohere",
       "Command R — RAG and tool use at long context.",
       ["tools"],
       [("35b", "35B", 20.0)]),
    _m("command-r7b", "command-r", "Cohere",
       "Small Command R for RAG and tools.",
       ["tools"],
       [("7b", "7B", 5.1)]),
    _m("glm4", "glm", "Zhipu AI",
       "GLM-4 9B — bilingual (zh/en) chat.",
       ["tools"],
       [("9b", "9B", 5.5)]),
    _m("hermes3", "llama", "Nous Research",
       "Hermes 3 — Llama 3.1 fine-tune with strong function calling.",
       ["tools"],
       [("8b", "8B", 4.7), ("70b", "70B", 43.0)]),
    _m("dolphin3", "llama", "Cognitive Computations",
       "Dolphin 3.0 — uncensored general assistant on Llama 3.1 8B.",
       ["tools"],
       [("8b", "8B", 4.9)]),
    _m("olmo2", "olmo", "AI2",
       "Fully open OLMo 2 models (weights, data and code).",
       [],
       [("7b", "7B", 4.5), ("13b", "13B", 8.4)]),
    _m("smollm2", "smollm", "Hugging Face",
       "Very small models for constrained devices.",
       ["tools"],
       [("135m", "135M", 0.3), ("360m", "360M", 0.7), ("1.7b", "1.7B", 1.8)],
       default_tag="1.7b"),
    _m("tinyllama", "llama", "TinyLlama",
       "1.1B Llama-architecture model; fits anywhere.",
       [],
       [("1.1b", "1.1B", 0.6)]),
    # ── Embeddings ──
    _m("nomic-embed-text", "nomic-bert", "Nomic AI",
       "High-quality text embeddings; 8K context. The usual RAG default.",
       ["embedding"],
       [("latest", "137M", 0.3)]),
    _m("mxbai-embed-large", "bert", "Mixedbread",
       "Large embedding model, strong on MTEB.",
       ["embedding"],
       [("latest", "335M", 0.7)]),
    _m("bge-m3", "bert", "BAAI",
       "Multilingual, multi-granularity embeddings.",
       ["embedding"],
       [("latest", "567M", 1.2)]),
    _m("all-minilm", "bert", "Sentence Transformers",
       "Tiny sentence embeddings.",
       ["embedding"],
       [("latest", "23M", 0.05)]),
    _m("snowflake-arctic-embed", "bert", "Snowflake",
       "Arctic embed family, retrieval-optimised.",
       ["embedding"],
       [("latest", "335M", 0.7)]),
]


def catalog() -> List[Dict[str, Any]]:
    """The whole catalogue, in display order."""
    return CATALOG


def search(q: str = "") -> List[Dict[str, Any]]:
    """Entries whose name / family / vendor / blurb / capability contain every
    whitespace-separated term of ``q`` (case-insensitive). Empty ``q`` → all."""
    terms = [t for t in str(q or "").lower().split() if t]
    if not terms:
        return list(CATALOG)
    out = []
    for entry in CATALOG:
        hay = " ".join([
            entry["name"], entry["family"], entry["vendor"], entry["blurb"],
            " ".join(entry["capabilities"]),
            " ".join(t["tag"] for t in entry["tags"]),
        ]).lower()
        if all(t in hay for t in terms):
            out.append(entry)
    return out


def tag_size_bytes(gb: float) -> int:
    return int(float(gb) * GIB)


def full_name(entry: Dict[str, Any], tag: Dict[str, Any]) -> str:
    """`nomic-embed-text` for a `latest` tag, `qwen3.5:9b` otherwise."""
    t = str(tag.get("tag") or "")
    if not t or t == "latest":
        return entry["name"]
    return f"{entry['name']}:{t}"
