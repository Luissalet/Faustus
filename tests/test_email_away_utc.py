from datetime import datetime, timezone
from email.message import EmailMessage

import pytest

from routes import email_pollers as pollers


@pytest.mark.parametrize('mail_date,expected', [
    ('Mon, 07 Sep 2026 13:00:00 +0200', False),  # 11:00 UTC, before enabling
    ('Mon, 07 Sep 2026 08:00:00 -0500', True),   # 13:00 UTC, after enabling
    ('Mon, 07 Sep 2026 11:30:00 -0000', False),
    ('Mon, 07 Sep 2026 12:30:00 +0000', True),
    ('invalid', False),
])
def test_away_start_uses_utc_not_the_senders_zone(mail_date, expected):
    msg = EmailMessage()
    msg['Date'] = mail_date
    assert pollers._message_after_away_enabled({'email_auto_reply_enabled_at':'2026-09-07T12:00:00'},msg) is expected


def test_away_cooldown_never_converts_naive_utc_as_local_time(monkeypatch, tmp_path):
    import sqlite3
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            assert tz is timezone.utc
            return cls(2026,9,7,12,0,0,tzinfo=tz)
        def timestamp(self):
            assert self.tzinfo is not None, 'Naive UTC must not use the OS timezone'
            return super().timestamp()
    monkeypatch.setattr(pollers, 'datetime', Clock)
    monkeypatch.setattr(pollers, 'SCHEDULED_DB', str(tmp_path/'away.db'))
    pollers._ensure_away_reply_table()
    with sqlite3.connect(pollers.SCHEDULED_DB) as db:
        db.execute("INSERT INTO email_away_replies (owner,account_id,message_id,sender_addr,sent_at) VALUES (?,?,?,?,?)",
                   ('alice','mail','previous','sender@example.test','2026-09-07T11:00:00'))
    assert pollers._away_reply_already_sent({'email_auto_reply_cooldown':'1d'},'alice','mail','new','sender@example.test')
    assert not pollers._away_reply_already_sent({'email_auto_reply_cooldown':'1d'},'bob','mail','new','sender@example.test')
