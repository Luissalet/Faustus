from datetime import datetime, timezone
from types import SimpleNamespace
import json

from src import chat_export


def test_background_export_uses_utc_not_local_clock(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            assert tz is timezone.utc, 'Export must explicitly select UTC'
            return cls(2026, 9, 7, 14, 2, 3, tzinfo=tz)
    monkeypatch.setattr(chat_export, 'datetime', Clock)
    transcript = chat_export.build_transcript(SimpleNamespace(name='test', model='', history=[], id='utc'))
    assert transcript.exported_at.utcoffset().total_seconds() == 0
    assert chat_export.default_filename(transcript, 'md') == 'conversation_test_20260907_140203.md'
    assert json.loads(chat_export.render_json(transcript))['exported'] == '2026-09-07T14:02:03+00:00'
