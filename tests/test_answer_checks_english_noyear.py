"""English dates without a year are checked like Spanish ones: the year
comes from the text, the question, or the one that puts the date within half
a year of today."""
import datetime as dt

from src.answer_checks import weekday_mismatches

TODAY = dt.date(2026, 9, 26)


def test_weekday_before_a_date_without_year():
    found = weekday_mismatches("Your next meeting is Monday, September 29.", "", TODAY)
    assert [(m["date"], m["said"], m["real"]) for m in found] == [("2026-09-29", "monday", "tuesday")]


def test_date_without_year_then_weekday():
    found = weekday_mismatches("December 25 is a Sunday.", "", TODAY)
    assert found and found[0]["real"] == "friday"


def test_right_weekdays_and_abbreviations_pass():
    for text in ("Tuesday 29 September", "See you Friday, Oct 2.", "December 25 is a Friday.",
                 "Friday the 25th of December"):
        assert weekday_mismatches(text, "", TODAY) == [], text


def test_the_year_from_the_question_wins():
    found = weekday_mismatches("It will be Friday, December 25.", "What day is Christmas 2027?", TODAY)
    assert found and found[0]["date"] == "2027-12-25" and found[0]["real"] == "saturday"


def test_no_today_and_no_year_means_no_claim():
    assert weekday_mismatches("Monday, September 29", "", None) == []
