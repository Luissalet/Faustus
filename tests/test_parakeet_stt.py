import io
import sys
import wave
from types import SimpleNamespace

import numpy as np
import pytest

from services.stt import parakeet_stt as p
from services.stt.stt_service import STTService
from services.speech_runtime import capabilities


def wav(samples, rate=16000):
    out = io.BytesIO()
    with wave.open(out, 'wb') as f:
        f.setnchannels(1); f.setsampwidth(2); f.setframerate(rate)
        f.writeframes(np.asarray(samples, dtype='<i2').tobytes())
    return out.getvalue()


def test_decode_resamples_and_silence_never_loads_model(monkeypatch):
    monkeypatch.setattr(p, '_model', None)
    data = wav(np.zeros(48000), 48000)
    assert len(p.decode(data)) == 16000
    assert p.transcribe_segments(data) == []
    assert not p.loaded()


def test_model_cached_cpu_vad_and_segment_times(monkeypatch):
    calls = []
    class Model:
        def with_vad(self, vad, **kwargs):
            assert kwargs['max_speech_duration_s'] == 20
            assert kwargs['min_speech_duration_ms'] == 100
            return self
        def recognize(self, waveform, **kwargs):
            assert kwargs == {'sample_rate': 16000}
            return iter([SimpleNamespace(start=.1, end=.9, text=' Sí. ')])
    def load_model(*args, **kwargs):
        calls.append(kwargs)
        return Model()
    monkeypatch.setitem(sys.modules, 'onnx_asr', SimpleNamespace(load_model=load_model, load_vad=lambda *a, **k: object()))
    monkeypatch.setattr(p, '_model', None)
    data = wav(np.ones(16000) * 500)
    metadata = {}
    for _ in range(2):
        assert p.transcribe_segments(data, metadata) == [{'start': .1, 'end': .9, 'text': 'Sí.'}]
    assert calls == [{'quantization': 'int8', 'providers': ['CPUExecutionProvider']}]
    assert metadata['language'] == '' and metadata['language_mode'] == 'auto'


def test_discovery_no_model_loading_and_provider_dispatch(monkeypatch):
    monkeypatch.setattr(p, 'installed', lambda: True)
    settings = dict(stt_enabled=True, stt_provider='parakeet', stt_model='small', stt_language='es')
    caps = capabilities(settings, 'stt')
    assert caps['model'] == p.MODEL and caps['execution'] == 'local'
    assert caps['language'] == '' and not caps['ready']
    svc = STTService()
    monkeypatch.setattr(svc, '_load_settings', lambda: settings)
    monkeypatch.setattr(p, 'transcribe_segments', lambda *a: [{'start': 0, 'end': 1, 'text': 'Gracias.'}])
    assert svc.available
    assert svc.transcribe(b'audio', expected_provider='parakeet') == 'Gracias.'
    assert svc.transcribe_segments(b'audio')[0]['text'] == 'Gracias.'
    monkeypatch.setattr(p, 'installed', lambda: False)
    assert not svc.available
    assert not capabilities(settings, 'stt')['dependency_installed']


def test_corrupt_audio_fails_instead_of_inventing_transcript():
    with pytest.raises(Exception):
        p.transcribe_segments(b'not audio')
