import json
import sys
import time
from dataclasses import replace

from src import agent_runners as reg, external_worker as worker


def test_unterminated_output_is_bounded_and_child_is_stopped(tmp_path, monkeypatch):
    monkeypatch.setattr(reg, 'enabled', lambda: True)
    source = tmp_path / 'oversize.py'
    source.write_text('import sys,time\nsys.stdout.write("x"*1100000)\nsys.stdout.flush()\ntime.sleep(60)\n')
    runner = replace(reg.get('codex', help_source=''), argv=(sys.executable, str(source), '--json'))
    started = time.monotonic()
    result = worker.run_task(runner, 'task', workspace=str(tmp_path), timeout_s=30)
    assert time.monotonic() - started < 15
    assert not result['ok'] and result['status'] == 'error'
    assert result['killed'] and not result['timed_out']
    assert 'transport' in result['error']
    assert len(result['output_tail']) <= worker.RESULT_TAIL_CHARS


def test_tool_inventory_cannot_grow_forever():
    stream = worker._Stream()
    event = json.dumps({'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'id': 'a', 'name': 'Read'}]}})
    for _ in range(4100):
        stream.feed(event)
    assert len(stream.tool_calls) == 4096
    assert stream.overflow
def test_tool_inventory_bounds_individual_identifiers():
    from src.external_worker import _Stream
    stream = _Stream()
    stream.feed(json.dumps({'type': 'assistant', 'message': {'content': [
        {'type':'tool_use','name':'tool','id':'x' * 10000}]}}))
    assert stream.overflow and not stream.tool_calls
