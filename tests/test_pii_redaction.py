"""FAUSTUS #146 — src.pii_redaction unit tests (positive + negative cases)."""
from src.pii_redaction import redact_pii


def test_email_redacted():
    text, counts = redact_pii("Contact me at luis.gomez@example.com for details.")
    assert "[EMAIL]" in text
    assert "luis.gomez@example.com" not in text
    assert counts == {"EMAIL": 1}


def test_multiple_emails_counted():
    text, counts = redact_pii("a@b.com and c@d.org both work.")
    assert counts["EMAIL"] == 2
    assert text.count("[EMAIL]") == 2


def test_ipv4_redacted():
    text, counts = redact_pii("The server is at 192.168.1.100 on the LAN.")
    assert "[IP]" in text
    assert "192.168.1.100" not in text
    assert counts == {"IP": 1}


def test_ipv4_out_of_range_not_redacted():
    text, counts = redact_pii("Version 999.999.999.999 is not a real IP.")
    assert "[IP]" not in text
    assert "IP" not in counts


def test_spanish_dni_valid_redacted():
    # 12345678Z is a well-known valid demo DNI (control letter checked)
    text, counts = redact_pii("Mi DNI es 12345678Z, gracias.")
    assert "[ID]" in text
    assert "12345678Z" not in text
    assert counts == {"ID": 1}


def test_spanish_nie_valid_redacted():
    # X1234567L: X -> 0, body 01234567 % 23 = 3 -> 'A'... compute dynamically instead.
    from src.pii_redaction import _DNI_LETTERS
    body = "01234567"
    letter = _DNI_LETTERS[int(body) % 23]
    nie = f"X1234567{letter}"
    text, counts = redact_pii(f"NIE: {nie}")
    assert "[ID]" in text
    assert nie not in text
    assert counts == {"ID": 1}


def test_dni_with_wrong_control_letter_not_redacted():
    # 12345678 % 23 -> correct letter is 'Z'; use a deliberately wrong one.
    from src.pii_redaction import _DNI_LETTERS
    correct = _DNI_LETTERS[12345678 % 23]
    wrong = "A" if correct != "A" else "B"
    text, counts = redact_pii(f"Numero {12345678}{wrong} no es un DNI valido")
    assert "[ID]" not in text
    assert "ID" not in counts


def test_plain_8_digit_number_without_letter_not_redacted():
    text, counts = redact_pii("El pedido numero 12345678 se completo hoy.")
    assert "[ID]" not in text
    assert "ID" not in counts


def test_iban_valid_redacted():
    # Well-known valid example Spanish IBAN (passes mod-97 checksum).
    iban = "ES9121000418450200051332"
    text, counts = redact_pii(f"Transfer to {iban} please.")
    assert "[IBAN]" in text
    assert iban not in text
    assert counts == {"IBAN": 1}


def test_random_alnum_lookalike_not_flagged_as_iban():
    fake = "AB1234567890123456"
    text, counts = redact_pii(f"Reference code {fake} for the order.")
    assert "[IBAN]" not in text
    assert "IBAN" not in counts


def test_credit_card_luhn_valid_redacted():
    # 4111 1111 1111 1111 is the standard Luhn-valid test Visa number.
    text, counts = redact_pii("Card on file: 4111111111111111.")
    assert "[CARD]" in text
    assert "4111111111111111" not in text
    assert counts == {"CARD": 1}


def test_credit_card_with_separators_redacted():
    text, counts = redact_pii("Card: 4111-1111-1111-1111 expires soon.")
    assert "[CARD]" in text
    assert counts == {"CARD": 1}


def test_luhn_invalid_number_not_redacted():
    # One digit off from the valid test card above -> fails Luhn.
    text, counts = redact_pii("Ref number 4111111111111112 is just an id.")
    assert "[CARD]" not in text
    assert "CARD" not in counts


def test_spanish_mobile_phone_redacted():
    text, counts = redact_pii("Llamame al 612345678 cuando puedas.")
    assert "[PHONE]" in text
    assert "612345678" not in text
    assert counts == {"PHONE": 1}


def test_spanish_phone_with_country_code_redacted():
    text, counts = redact_pii("Mi numero es +34 612 345 678.")
    assert "[PHONE]" in text
    assert counts == {"PHONE": 1}


def test_international_phone_redacted():
    text, counts = redact_pii("Call +1 415 555 0132 for support.")
    assert "[PHONE]" in text
    assert counts == {"PHONE": 1}


def test_year_not_redacted_as_phone_or_card():
    text, counts = redact_pii("The company was founded in 2024 and grew in 2025.")
    assert text == "The company was founded in 2024 and grew in 2025."
    assert counts == {}


def test_price_not_redacted():
    text, counts = redact_pii("The total price is 19.99 dollars, or $1999 in cents.")
    assert "[CARD]" not in text
    assert "[PHONE]" not in text
    assert "[IBAN]" not in text
    assert "[ID]" not in text


def test_page_number_style_text_untouched():
    text, counts = redact_pii("See page 12 of chapter 3 for details.")
    assert text == "See page 12 of chapter 3 for details."
    assert counts == {}


def test_empty_and_non_string_input():
    text, counts = redact_pii("")
    assert text == ""
    assert counts == {}

    text2, counts2 = redact_pii(None)
    assert text2 is None
    assert counts2 == {}


def test_mixed_text_multiple_types_all_counted():
    text, counts = redact_pii(
        "Email juan@empresa.es, phone 612345678, DNI 12345678Z, "
        "card 4111111111111111, IP 10.0.0.5, all in one note."
    )
    assert counts.get("EMAIL") == 1
    assert counts.get("PHONE") == 1
    assert counts.get("ID") == 1
    assert counts.get("CARD") == 1
    assert counts.get("IP") == 1
    assert "juan@empresa.es" not in text
    assert "612345678" not in text
    assert "12345678Z" not in text
    assert "4111111111111111" not in text
    assert "10.0.0.5" not in text


def test_original_text_returned_when_nothing_matches():
    original = "Nothing sensitive here, just a regular sentence about weather."
    text, counts = redact_pii(original)
    assert text == original
    assert counts == {}
