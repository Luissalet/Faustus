import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from src import cli_model, codex_chat as chat

PEER = Path(__file__).parent / 'fixtures' / 'codex_chat_peer.py'


def infer(monkeypatch, tmp_path, scenario='ok', timeout=4, cancel=None):
    monkeypatch.setattr(chat, '_command', lambda *args: [sys.executable, str(PEER), scenario])
    processes = []
    original = subprocess.Popen
    def spawn(*args, **kwargs):
        proc = original(*args, **kwargs)
        processes.append(proc)
        return proc
    monkeypatch.setattr(chat.subprocess, 'Popen', spawn)
    try:
        return chat._infer(sys.executable, dict(os.environ), str(tmp_path), 'Synthetic prompt',
            'client-default', 'subscription', timeout, cancel)
    finally:
        assert processes and all(proc.poll() is not None for proc in processes)


@pytest.mark.parametrize('scenario', ['ok', 'early-events'])
def test_real_pipes_return_only_final_answer_and_cleanup(monkeypatch, tmp_path, scenario):
    assert infer(monkeypatch, tmp_path, scenario) == 'Hola, English ✓'


@pytest.mark.parametrize('scenario,reason', [
    ('wrong-account', 'access method'), ('provider', 'provider overrides'),
    ('permissions', 'permissions'), ('request', 'native tool'),
    ('native-tool', 'native tool'), ('rpc-error', 'turn/start'),
    ('foreign-turn', 'unexpected turn'), ('failed', 'did not complete'),
    ('exit', 'before completing'), ('invalid', 'invalid'), ('oversized', 'oversized'),
])
def test_protocol_failures_are_closed_and_never_leak_diagnostics(monkeypatch, tmp_path, scenario, reason):
    with pytest.raises(cli_model.ClientModelError, match=reason) as exc:
        infer(monkeypatch, tmp_path, scenario)
    assert exc.value.fallback_eligible is False
    assert 'synthetic-secret' not in str(exc.value)


@pytest.mark.parametrize('scenario', ['hang', 'stderr'])
def test_timeout_kills_real_peer_even_if_stderr_is_flooded(monkeypatch, tmp_path, scenario):
    before = time.monotonic()
    with pytest.raises(cli_model.ClientModelError, match='timed out'):
        infer(monkeypatch, tmp_path, scenario, timeout=.5)
    assert time.monotonic() - before < 6


def test_cancel_stops_real_peer(monkeypatch, tmp_path):
    cancel = threading.Event()
    timer = threading.Timer(.4, cancel.set)
    timer.start()
    try:
        with pytest.raises(cli_model.ClientModelError, match='cancelled'):
            infer(monkeypatch, tmp_path, 'hang', cancel=cancel)
    finally:
        timer.cancel()


@pytest.mark.parametrize('scenario,setting,reason', [
    ('empty-items', 'MAX_ITEMS', 'item limit'),
    ('repeat-events', 'MAX_EVENTS', 'event limit'),
])
def test_empty_event_floods_are_bounded_and_process_is_cleaned(monkeypatch, tmp_path, scenario, setting, reason):
    monkeypatch.setattr(chat, setting, 8)
    with pytest.raises(cli_model.ClientModelError, match=reason):
        infer(monkeypatch, tmp_path, scenario)


def test_replaced_item_does_not_double_count_output_budget(monkeypatch, tmp_path):
    monkeypatch.setattr(chat, 'MAX_ANSWER', 30)
    assert infer(monkeypatch, tmp_path, 'replacement') == 'Hola, English ✓'


def test_command_does_not_use_exec_or_ignore_user_config(tmp_path):
    cmd = chat._command('codex', 'subscription', tmp_path / 'instructions.txt')
    assert cmd[:3] == ['codex', 'app-server', '--stdio']
    assert '--ignore-user-config' not in cmd
    assert 'forced_login_method="chatgpt"' in cmd
    assert 'notify=[]' in cmd and 'project_doc_max_bytes=0' in cmd
    assert all(cmd[cmd.index(feature) - 1] == '--disable' for feature in chat.DISABLED)
    assert 'forced_login_method="api"' in chat._command('codex', 'api', tmp_path / 'i')


def test_each_inherited_mcp_name_is_literal_not_treated_as_a_path():
    names = ['ordinary', 'x.y', 'quoted"name', 'line\nname']
    overrides = chat._thread_overrides({'mcp_servers': {key: {} for key in names}})
    assert overrides == {'mcp_servers': {key: {'enabled': False} for key in names}}


def test_disabled_runners_and_precancellation_do_not_spawn(monkeypatch):
    from src import agent_runners
    monkeypatch.setattr(agent_runners, 'enabled', lambda: False)
    with pytest.raises(cli_model.ClientModelError, match='off'):
        chat.complete('prompt', 'model', 'subscription', 10)
    monkeypatch.setattr(agent_runners, 'enabled', lambda: True)
    cancel = threading.Event(); cancel.set()
    with pytest.raises(cli_model.ClientModelError, match='cancelled'):
        chat.complete('prompt', 'model', 'subscription', 10, cancel)


def test_cli_model_dispatches_codex_through_same_textual_harness(monkeypatch):
    monkeypatch.setattr(cli_model, 'authorize', lambda *args: ('codex', 'subscription', 'alice'))
    def complete(prompt, model, mode, timeout, cancel):
        assert 'Español' in prompt and model == 'client-default' and mode == 'subscription'
        return 'Hola'
    monkeypatch.setattr(chat, 'complete', complete)
    assert cli_model.complete('url', 'client-default', [{'role': 'system', 'content': 'Español'}]) == 'Hola'
