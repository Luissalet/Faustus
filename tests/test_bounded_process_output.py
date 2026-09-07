import subprocess
import sys

from src.bounded_process_output import capture


def test_both_large_streams_are_drained_and_only_tails_kept():
    code = "import sys; sys.stdout.buffer.write(b'A'*4000000+b'ENDOUT'); sys.stderr.buffer.write(b'B'*4000000+b'ENDERR')"
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    result = capture(proc, timeout=10, stop=proc.kill, limit=2048)
    assert len(result.stdout) == len(result.stderr) == 2048
    assert result.stdout.endswith(b"ENDOUT") and result.stderr.endswith(b"ENDERR")
    assert result.truncated and not result.timed_out
    assert proc.returncode == 0


def test_timeout_stops_the_owned_process_and_preserves_partial_output():
    proc = subprocess.Popen([sys.executable, "-u", "-c", "import time; print('started'); time.sleep(30)"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    result = capture(proc, timeout=0.5, stop=proc.kill)
    assert result.timed_out
    assert b"started" in result.stdout
    assert proc.poll() is not None


def test_small_output_is_not_labelled_truncated():
    proc = subprocess.Popen([sys.executable, "-c", "print('hello')"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    result = capture(proc, timeout=5, stop=proc.kill)
    assert result.stdout.strip() == b"hello"
    assert result.stderr == b"" and not result.truncated
