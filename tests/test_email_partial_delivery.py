import smtplib

import pytest

from routes.email_helpers import _send_smtp_message
from tests.test_email_smtp_security import _FakeSMTP, _cfg


@pytest.mark.parametrize("security", ["ssl", "starttls", "none"])
def test_partial_recipient_rejection_is_not_reported_as_full_success(monkeypatch, security):
    class PartialSMTP(_FakeSMTP):
        def sendmail(self, sender, recipients, message):
            return {"rejected@example.com": (550, b"No such mailbox")}

    monkeypatch.setattr(smtplib, "SMTP", PartialSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", PartialSMTP)
    with pytest.raises(smtplib.SMTPRecipientsRefused) as error:
        _send_smtp_message(_cfg(security), "sender@example.com",
                           ["accepted@example.com", "rejected@example.com"], "hello")
    assert set(error.value.recipients) == {"rejected@example.com"}
