"""Small reproducible CHM150 speech-recognition evaluation through Faustus.

Downloads only selected clips, outside the repository. References preserve
disfluencies: WER measures ASR fidelity, not the quality of a polished draft.
CHM150: Carlos Daniel Hernandez Mena and Abel Herrera, CC BY-SA 4.0.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
import unicodedata
import sys
from urllib.parse import urlparse
from pathlib import Path

import requests

DATASET = "carlosdanielhernandezmena/chm150_asr"
SOURCE = "https://huggingface.co/datasets/" + DATASET


def words(text):
    return re.findall(r"\w+", unicodedata.normalize("NFC", text).casefold())


def errors(reference, hypothesis):
    ref, hyp = words(reference), words(hypothesis)
    row = list(range(len(hyp) + 1))
    for i, word in enumerate(ref, 1):
        next_row = [i]
        for j, other in enumerate(hyp, 1):
            next_row.append(min(row[j] + 1, next_row[-1] + 1, row[j - 1] + (word != other)))
        row = next_row
    return row[-1], len(ref)


def prepare(directory, corpus="chm150", sample_offset=0):
    directory.mkdir(parents=True, exist_ok=True)
    selected, seen = [], set()
    disfluent = corpus == "disfluency"
    dataset = "amaai-lab/DisfluencySpeech" if disfluent else DATASET
    source_page = "https://huggingface.co/datasets/" + dataset
    for offset in ((sample_offset,) if disfluent else (0, 700, 1400, 2100)):
        response = requests.get("https://datasets-server.huggingface.co/rows", params={
            "dataset": dataset, "config": "default" if disfluent else "chm150_asr", "split": "test" if disfluent else "train", "offset": offset, "length": 100}, timeout=30)
        response.raise_for_status()
        count = 0
        for entry in response.json()["rows"]:
            row = entry["row"]
            if disfluent:
                if row["transcript_b"] == row["transcript_c"]:
                    continue  # explicitly annotated false starts/repairs
            else:
                if row["duration"] < 4 or row["speaker_id"] in seen:
                    continue
                seen.add(row["speaker_id"])
            source = row["audio"][0]["src"]
            audio = requests.get(source, timeout=30)
            audio.raise_for_status()
            sample_id = f"disfluency-test-{entry['row_idx']}" if disfluent else row["audio_id"]
            filename = sample_id + Path(urlparse(source).path).suffix
            (directory / filename).write_bytes(audio.content)
            selected.append({"id": sample_id, "row_index": entry["row_idx"],
                             "speaker_id": row.get("speaker_id", "single-speaker"), "duration": row.get("duration"),
                             "reference": row["transcript_a"] if disfluent else row["normalized_text"],
                             "clean_reference": row.get("transcript_c"), "file": filename,
                             "sha256": hashlib.sha256(audio.content).hexdigest()})
            count += 1
            if count == (4 if disfluent else 2):
                break
    manifest = {"source": source_page, "license": "Apache-2.0" if disfluent else "CC-BY-SA-4.0",
                "attribution": "DisfluencySpeech, Kyra Wang and Dorien Herremans (2024)" if disfluent else "CHM150 CORPUS, Carlos Daniel Hernandez Mena and Abel Herrera (2016)",
                "selection": f"First four test rows from offset {sample_offset} whose transcript_b differs from transcript_c; exploratory repair subset." if disfluent else "First two distinct speakers with clips >=4 seconds in each 100-row window at offsets 0,700,1400,2100; not a representative test split.",
                "samples": selected}
    (directory / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--corpus", choices=["chm150", "disfluency"], default="chm150")
    parser.add_argument("--sample-offset", type=int, default=0)
    parser.add_argument("--polish", action="store_true", help="Evaluate the 3.8 editor after ASR where a clean reference exists")
    parser.add_argument("--url", default="http://127.0.0.1:7001")
    parser.add_argument("--language", action="append", default=[])
    parser.add_argument("--local-model", help="Compare an already cached faster-whisper model, without changing Faustus settings")
    parser.add_argument("--beam-size", type=int, default=5)
    args = parser.parse_args()
    manifest = prepare(args.directory, args.corpus, args.sample_offset) if args.prepare else json.loads((args.directory / "manifest.json").read_text(encoding="utf-8"))
    caps_response = requests.get(args.url + "/api/stt/capabilities", timeout=15)
    caps_response.raise_for_status()
    caps = caps_response.json()
    local_model = None
    if args.local_model:
        from faster_whisper import WhisperModel
        local_model = WhisperModel(args.local_model, device="cpu", compute_type="int8", local_files_only=True)
        caps = {"provider": "isolated-faster-whisper", "model": args.local_model, "device": "cpu", "beam_size": args.beam_size}
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from src.stt_cleanup import clean_segments, segments_to_text
    results = []
    for language in args.language or ["auto", "es"]:
        for sample in manifest["samples"]:
            started = time.perf_counter()
            result = {"id": sample["id"], "requested_language": language, "reference": sample["reference"]}
            try:
                audio = (args.directory / sample["file"]).read_bytes()
                if hashlib.sha256(audio).hexdigest() != sample["sha256"]:
                    raise ValueError("Audio hash mismatch")
                if local_model is None:
                    response = requests.post(args.url + "/api/stt/transcribe", data={"language": language, "expected_provider": caps["provider"]},
                                             files={"file": (sample["file"], audio, "audio/flac")}, timeout=120)
                    response.raise_for_status()
                    body = response.json()
                else:
                    segments, info = local_model.transcribe(str(args.directory / sample["file"]), language=None if language == "auto" else language,
                                                           beam_size=args.beam_size, condition_on_previous_text=False, vad_filter=True)
                    cleaned, stats = clean_segments([{"start": seg.start, "end": seg.end, "text": seg.text} for seg in segments])
                    body = {"text": segments_to_text(cleaned), "language": info.language, "cleanup_stats": stats}
                distance, count = errors(sample["reference"], body["text"])
                result.update(text=body["text"], detected_language=body.get("language"), word_errors=distance, reference_words=count,
                              wer=round(distance / max(count, 1), 4), cleanup_stats=body.get("cleanup_stats"))
                result["asr_seconds"] = round(time.perf_counter() - started, 3)
                if args.polish and sample.get("clean_reference"):
                    from eval_natural_dictation import SYSTEM
                    editor_started = time.perf_counter()
                    edited = requests.post("http://127.0.0.1:8081/v1/chat/completions", json={
                        "model": "qwen3.8-27b-q8-llamacpp", "stream": False, "temperature": 0, "max_tokens": 384,
                        "chat_template_kwargs": {"enable_thinking": False}, "response_format": {"type": "json_object"},
                        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": json.dumps({"mode": "dictate", "draft": "", "transcript": body["text"]})}]}, timeout=60)
                    edited.raise_for_status()
                    choice = edited.json()["choices"][0]
                    if choice.get("finish_reason") != "stop":
                        raise ValueError("Truncated editor output")
                    text = json.loads(choice["message"]["content"])["text"]
                    before, clean_words = errors(sample["clean_reference"], body["text"])
                    after, _ = errors(sample["clean_reference"], text)
                    result.update(polished=text, clean_reference=sample["clean_reference"], clean_reference_words=clean_words,
                                  before_clean_errors=before, after_clean_errors=after,
                                  editor_seconds=round(time.perf_counter() - editor_started, 3))
            except Exception as exc:
                result["error"] = f"{type(exc).__name__}: {exc}"
            result["seconds"] = round(time.perf_counter() - started, 3)
            results.append(result)
            report = {"source": manifest["source"], "capabilities": caps, "metric": "Case/punctuation-normalized WER; numbers are not expanded; reference includes fillers. Clean-reference comparisons are separate.", "results": results}
            filename = f"asr-beam{args.beam_size}-results.json" if local_model else "asr-results.json"
            (args.directory / filename).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(json.dumps(result, ensure_ascii=True), flush=True)
    return int(any("error" in result for result in results))


if __name__ == "__main__":
    raise SystemExit(main())
