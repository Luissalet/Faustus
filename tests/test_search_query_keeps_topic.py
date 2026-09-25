"""The auto web search keeps the question's subject (src/chat_processor.keeps_the_topic)."""
from src.chat_processor import keeps_the_topic


def test_one_word_from_a_long_question_is_rejected():
    msg = "Si hoy es viernes 25 de septiembre de 2026, ¿qué día de la semana será el 1 de enero de 2027?"
    assert not keeps_the_topic("viernes", msg)


def test_a_query_with_the_subject_is_kept():
    assert keeps_the_topic("tiempo Madrid mañana", "¿Qué tiempo hará mañana en Madrid?")
    assert keeps_the_topic("final Mundial 2026 resultado", "¿Quién ganó la final del Mundial 2026?")


def test_a_short_message_may_give_a_one_word_query():
    assert keeps_the_topic("bitcoin", "precio bitcoin")


def test_a_translated_query_is_kept_and_an_empty_one_is_not():
    assert keeps_the_topic("weather Madrid tomorrow", "¿Qué tiempo hará mañana en Madrid?")
    assert not keeps_the_topic("", "¿Qué tiempo hará mañana en Madrid?")
