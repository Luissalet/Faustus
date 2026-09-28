"""Optional CPU Parakeet V3, loaded once; VAD bounds each recognition segment.

onnx-asr: MIT. NVIDIA weights and istupakov ONNX conversion: CC BY 4.0.
https://huggingface.co/istupakov/parakeet-tdt-0.6b-v3-onnx
"""
import io
import threading
from importlib.util import find_spec

MODEL = "nemo-parakeet-tdt-0.6b-v3"
_lock = threading.RLock()
_model = None


def installed():
    return all(find_spec(name) is not None for name in ("onnx_asr", "onnxruntime", "av"))


def loaded():
    return _model is not None


def decode(audio_bytes):
    import av
    import numpy as np
    chunks = []
    with av.open(io.BytesIO(audio_bytes)) as container:
        resampler = av.AudioResampler(format="flt", layout="mono", rate=16000)
        for frame in container.decode(audio=0):
            chunks.extend(f.to_ndarray().reshape(-1) for f in resampler.resample(frame))
        chunks.extend(f.to_ndarray().reshape(-1) for f in resampler.resample(None))
    return np.concatenate(chunks).astype(np.float32) if chunks else np.empty(0, dtype=np.float32)


def transcribe_segments(audio_bytes, metadata=None):
    global _model
    waveform = decode(audio_bytes)
    if metadata is not None:
        # Parakeet auto-detects internally but does not expose a language ID.
        metadata.update(model=MODEL, language="", language_mode="auto", device="cpu")
    if waveform.size == 0 or not waveform.any():
        return []
    with _lock:
        if _model is None:
            import onnx_asr
            model = onnx_asr.load_model(MODEL, quantization="int8", providers=["CPUExecutionProvider"])
            vad = onnx_asr.load_vad("silero", providers=["CPUExecutionProvider"])
            _model = model.with_vad(vad, min_speech_duration_ms=100,
                                   max_speech_duration_s=20, min_silence_duration_ms=500,
                                   speech_pad_ms=200)
        # VAD boundaries are segment times, not word alignment.
        return [{"start": segment.start, "end": segment.end, "text": segment.text.strip()}
                for segment in _model.recognize(waveform, sample_rate=16000)
                if segment.text.strip()]
