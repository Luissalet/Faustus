"""Registers the ``qa_state`` marker used by every ``tests/qa/test_qa_*.py``.

Each QA test module declares exactly one of::

    pytestmark = pytest.mark.qa_state("green")   # behaviour exists and is proven here
    pytestmark = pytest.mark.qa_state("xfail")    # mechanism missing; test is xfail(strict=True)
    pytestmark = pytest.mark.qa_state("manual")   # needs real hardware/model/mic/mail/account

``tests/test_qa_index.py`` reads this marker back off every collected module to
make sure no one of the 48 acceptance scenarios was quietly skipped or left
without a declared state.
"""
import sys
import os

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

QA_STATES = ("green", "xfail", "manual")


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "qa_state(state): declares a tests/qa module's status - one of "
        + ", ".join(QA_STATES),
    )
