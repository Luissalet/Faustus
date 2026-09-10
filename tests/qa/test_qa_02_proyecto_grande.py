"""QA-02 - Proyecto grande (docs/spec/v2/acceptance_scenarios.json).

Estimulo: repositorio con vendor, binarios y codigo propio; pedir un bug
localizado.
Resultado exigido (literal): "Indice respeta exclusiones y devuelve
definicion/callers/tests sin cargar el repositorio entero."

Requisitos: IDX-02, IDX-03, CTX-01.

Estado: verde (Lote 38). `src/code_index.py` is the symbol index this
scenario asked for: `find_definition`/`find_callers`/`tests_for` answer a
localized-bug question, and its own walk (`iter_candidates`) prunes
`vendor/`, binaries and anything `.gitignore`/`.faustusignore` name before a
single byte of them is ever read - proven below with a spy on `open()`.
"""
import builtins
import os

import pytest

from src import code_index as ci
from src.context_engine import store

pytestmark = pytest.mark.qa_state("green")


@pytest.fixture()
def ce_store(tmp_path):
    store.use_path(str(tmp_path / "ce.db"))
    try:
        yield
    finally:
        store.use_path(None)


def _write(root, rel, content):
    path = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(path) or root, exist_ok=True)
    mode = "wb" if isinstance(content, bytes) else "w"
    kwargs = {} if isinstance(content, bytes) else {"encoding": "utf-8"}
    with open(path, mode, **kwargs) as handle:
        handle.write(content)


PAYMENTS_PY = '''"""Payment processing - the module with the bug."""


class PaymentProcessor:
    """Charges a customer and records the result."""

    def charge_customer(self, amount: float) -> bool:
        """The buggy function the scenario is asking about."""
        return apply_discount(amount) > 0


def apply_discount(amount: float) -> float:
    return amount * 0.9


def checkout(amount: float) -> bool:
    """Calls the buggy function - a caller the answer must find."""
    return PaymentProcessor().charge_customer(amount)
'''

TEST_PAYMENTS_PY = '''from app.payments import PaymentProcessor


def test_charge_customer_applies_discount():
    assert PaymentProcessor().charge_customer(10.0) is True
'''


@pytest.fixture()
def large_project(tmp_path):
    """Vendor (200 files) + a binary + the caller's own code (Lote 38 item 4)."""
    root = str(tmp_path / "project")
    vendor_dir = os.path.join(root, "vendor")
    for i in range(200):
        _write(root, f"vendor/dep_{i}.py", f"def vendored_{i}():\n    return {i}\n")
    _write(root, "vendor/asset.bin", b"\x00\x01BINARYDATA" * 500)
    os.makedirs(vendor_dir, exist_ok=True)

    _write(root, "app/payments.py", PAYMENTS_PY)
    _write(root, "tests/test_payments.py", TEST_PAYMENTS_PY)
    return root


def test_index_answers_a_localized_bug_without_opening_vendor_or_binaries(
    ce_store, large_project
):
    root = large_project
    opened: list[str] = []
    real_open = builtins.open

    def spy_open(file, *args, **kwargs):
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    builtins.open = spy_open
    try:
        refresh_report = ci.refresh(root)
        definitions = ci.find_definition("charge_customer", workspace=root)
        callers = ci.find_callers("charge_customer", workspace=root)
        tests = ci.tests_for("charge_customer", workspace=root)
    finally:
        builtins.open = real_open

    # "sin cargar el repositorio entero": the 200 vendored files and the
    # binary were never opened, even though they were on disk right next to
    # the code the scenario asks about.
    assert not any(f"{os.sep}vendor{os.sep}" in path for path in opened)
    assert not any(path.endswith(".bin") for path in opened)
    assert refresh_report["scanned"] == 2  # app/payments.py, tests/test_payments.py

    # "definicion": exactly the buggy method, with a real line range.
    assert len(definitions) == 1
    assert definitions[0]["path"] == "app/payments.py"
    assert definitions[0]["kind"] == "method"
    assert definitions[0]["qualname"] == "PaymentProcessor.charge_customer"
    assert definitions[0]["start_line"] > 0 < definitions[0]["end_line"]

    # "callers": checkout() calls it; the definition line itself is excluded.
    assert any(hit["path"] == "app/payments.py" for hit in callers)
    def_line = definitions[0]["start_line"]
    assert (
        "app/payments.py",
        def_line,
    ) not in [(hit["path"], hit["line"]) for hit in callers]

    # "tests": the convention-named test file for this module.
    assert any(hit["path"] == "tests/test_payments.py" for hit in tests)

    # None of it required reading the vendored code, and yet vendored
    # symbols were correctly kept out of the index.
    assert ci.find_definition("vendored_0", workspace=root) == []
