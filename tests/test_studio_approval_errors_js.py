from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(not shutil.which("node"), reason="Node.js required")
def test_approval_error_messages_keep_the_server_explanation():
    result = subprocess.run(["node", "studio/checks/approval-errors.check.mjs"], cwd=ROOT,
                            capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ALL OK" in result.stdout


def test_approval_card_is_not_removed_before_the_server_accepts():
    source = (ROOT / "studio/src/screens/Studio.tsx").read_text(encoding="utf-8")
    run = source[source.index("  const run = useCallback("):source.index("  const rejoin = useCallback(")]
    before_send = run[:run.index("for await (const event of sendTurn(")]
    assert "ask: undefined" not in before_send
    assert "closeApproval(" in run[run.index("onRunId: (id) => {"):]
