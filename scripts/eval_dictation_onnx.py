"""Isolated CPU Parakeet comparison using eval_dictation_audio manifests.

Run in a separate venv with onnx-asr[cpu,hub] and soundfile. Downloads the
selected model on first use; does not change Faustus providers or settings.
Parakeet weights: NVIDIA / istupakov ONNX conversion, CC BY 4.0.
"""
import argparse
import hashlib
import importlib.metadata
import json
import statistics
import time
from pathlib import Path

import onnx_asr
import soundfile as sf

from eval_dictation_audio import errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads((args.directory / 'manifest.json').read_text(encoding='utf-8'))
    started = time.perf_counter()
    model = onnx_asr.load_model('nemo-parakeet-tdt-0.6b-v3', quantization='int8', providers=['CPUExecutionProvider'])
    report = {'model': 'nemo-parakeet-tdt-0.6b-v3', 'quantization': 'int8',
              'provider': 'CPUExecutionProvider', 'source': manifest['source'],
              'model_source': 'https://huggingface.co/istupakov/parakeet-tdt-0.6b-v3-onnx',
              'model_license': 'CC-BY-4.0', 'model_attribution': 'NVIDIA; ONNX conversion by istupakov',
              'load_seconds_including_download': round(time.perf_counter() - started, 3),
              'versions': {p: importlib.metadata.version(p) for p in ['onnx-asr', 'onnxruntime']},
              'metric': 'Case/punctuation-normalized WER; no number expansion; verbatim references retain fillers.',
              'results': []}
    for sample in manifest['samples']:
        path = args.directory / sample['file']
        if hashlib.sha256(path.read_bytes()).hexdigest() != sample['sha256']:
            raise ValueError('Audio hash mismatch: ' + sample['id'])
        waveform, sample_rate = sf.read(path, dtype='float32')
        if waveform.ndim != 1:
            waveform = waveform.mean(axis=1)
        started = time.perf_counter()
        text = model.recognize(waveform, sample_rate=sample_rate)
        elapsed = time.perf_counter() - started
        distance, count = errors(sample['reference'], text)
        result = {'id': sample['id'], 'reference': sample['reference'], 'text': text,
                  'word_errors': distance, 'reference_words': count,
                  'seconds': round(elapsed, 3), 'audio_seconds': round(len(waveform) / sample_rate, 3)}
        report['results'].append(result)
        print(json.dumps(result, ensure_ascii=True), flush=True)
        report['summary'] = {
            'word_errors': sum(r['word_errors'] for r in report['results']),
            'reference_words': sum(r['reference_words'] for r in report['results']),
            'median_seconds': statistics.median(r['seconds'] for r in report['results'])}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
