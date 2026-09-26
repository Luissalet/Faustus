"""Per-process GPU attribution (`gpu_shared_memory.compute_apps`/`label_process`
plus `routes.local_models_routes._attribute_gpu_processes`): whoever
`nvidia-smi --query-compute-apps` says is running on a card, not just what
Ollama reports as loaded — so a card holding a managed engine's model (or any
other process) is never shown as "nothing loaded" just because Ollama has
nothing there.
"""
from __future__ import annotations

import subprocess

from src import gpu_shared_memory as gsm
import routes.local_models_routes as lm

MB = 1024 * 1024

_CSV = (
    "GPU-aaa,111,/usr/bin/llama-server,11264\n"
    "GPU-bbb,222,ollama_llama_server,4096\n"
    # Windows WDDM: nvidia-smi cannot report used_memory for a process — the
    # row still names a real pid and must not be dropped.
    "GPU-bbb,333,C:\\Users\\me\\tools\\somebot.exe,[N/A]\n"
)


# ── gpu_shared_memory.parse_compute_apps ────────────────────────────────────

def test_parse_compute_apps_reads_rows_and_handles_na():
    rows = gsm.parse_compute_apps(_CSV)
    assert rows == [
        {"gpu_uuid": "GPU-aaa", "pid": 111, "process_name": "/usr/bin/llama-server", "used_mb": 11264},
        {"gpu_uuid": "GPU-bbb", "pid": 222, "process_name": "ollama_llama_server", "used_mb": 4096},
        {"gpu_uuid": "GPU-bbb", "pid": 333, "process_name": "C:\\Users\\me\\tools\\somebot.exe", "used_mb": None},
    ]


def test_parse_compute_apps_skips_unparsable_and_empty():
    assert gsm.parse_compute_apps("") == []
    assert gsm.parse_compute_apps("not,enough,columns\n") == []
    assert gsm.parse_compute_apps("GPU-x, notapid, proc, 10\n") == []


# ── gpu_shared_memory.label_process ─────────────────────────────────────────

def test_label_process_prefers_engine_match():
    # Even a process literally named llama-server yields to a known engine
    # whose pid is confirmed listening on its configured port.
    assert gsm.label_process("llama-server", 111, {111: "Coding engine"}) == "Coding engine"


def test_label_process_llama_server_by_name():
    assert gsm.label_process("llama-server", 999, {}) == "llama-server"
    assert gsm.label_process("llama-server.exe", 999, {}) == "llama-server"
    assert gsm.label_process("/opt/bin/llama-server", 999, {}) == "llama-server"


def test_label_process_ollama_runner():
    assert gsm.label_process("ollama_llama_server", 5, None) == "Ollama"
    assert gsm.label_process("ollama.exe", 5, None) == "Ollama"


def test_label_process_falls_back_to_basename():
    assert gsm.label_process("C:\\tools\\somebot.exe", 5, None) == "somebot.exe"
    assert gsm.label_process("", 5, None) == ""


# ── gpu_shared_memory.compute_apps: never raises ────────────────────────────

def test_compute_apps_no_nvidia_smi_returns_empty(monkeypatch):
    monkeypatch.setattr(gsm, "_nvidia_smi_path", lambda: None)
    gsm.reset_compute_apps_cache()
    assert gsm.compute_apps() == []


def test_compute_apps_subprocess_failure_returns_empty(monkeypatch):
    monkeypatch.setattr(gsm, "_nvidia_smi_path", lambda: "/usr/bin/nvidia-smi")

    def boom(*a, **k):
        raise OSError("no such device")

    monkeypatch.setattr(gsm.subprocess, "run", boom)
    gsm.reset_compute_apps_cache()
    assert gsm.compute_apps() == []


def test_compute_apps_nonzero_exit_returns_empty(monkeypatch):
    monkeypatch.setattr(gsm, "_nvidia_smi_path", lambda: "/usr/bin/nvidia-smi")
    monkeypatch.setattr(
        gsm.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout="", stderr="err"),
    )
    gsm.reset_compute_apps_cache()
    assert gsm.compute_apps() == []


def test_compute_apps_parses_real_output(monkeypatch):
    monkeypatch.setattr(gsm, "_nvidia_smi_path", lambda: "/usr/bin/nvidia-smi")
    monkeypatch.setattr(
        gsm.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=_CSV, stderr=""),
    )
    gsm.reset_compute_apps_cache()
    rows = gsm.compute_apps()
    assert len(rows) == 3
    assert rows[0]["pid"] == 111 and rows[0]["used_mb"] == 11264
    assert rows[2]["used_mb"] is None


# ── routes.local_models_routes._attribute_gpu_processes ─────────────────────

def test_attribute_gpu_processes_maps_by_uuid_and_labels(monkeypatch):
    cards = [
        {"index": 0, "uuid": "GPU-aaa"},
        {"index": 1, "uuid": "GPU-bbb"},
    ]
    monkeypatch.setattr(gsm, "compute_apps", lambda: [
        {"gpu_uuid": "GPU-aaa", "pid": 111, "process_name": "llama-server", "used_mb": 11264},
        {"gpu_uuid": "GPU-bbb", "pid": 222, "process_name": "ollama_llama_server", "used_mb": 4096},
        {"gpu_uuid": "GPU-bbb", "pid": 333, "process_name": "somebot", "used_mb": None},
    ])
    # pid 111 is a managed engine ("Coding engine") listening on its port.
    monkeypatch.setattr(lm, "_engine_pids", lambda: {111: "Coding engine"})

    lm._attribute_gpu_processes(cards)

    # A managed engine is marked so the VRAM bar counts it as a model even
    # though its label is the engine's name, not a model's.
    assert cards[0]["processes"] == [{"pid": 111, "label": "Coding engine", "used_mb": 11264, "kind": "model",
                                      "engine": True}]
    # Any other app drawing on the card is kept but marked "other" (the UI
    # only counts those).
    assert cards[1]["processes"] == [
        {"pid": 222, "label": "Ollama", "used_mb": 4096, "kind": "model"},
        {"pid": 333, "label": "somebot", "used_mb": None, "kind": "other"},
    ]


def test_attribute_gpu_processes_unmatched_uuid_is_dropped(monkeypatch):
    cards = [{"index": 0, "uuid": "GPU-aaa"}]
    monkeypatch.setattr(gsm, "compute_apps", lambda: [
        {"gpu_uuid": "GPU-does-not-exist", "pid": 1, "process_name": "x", "used_mb": 1},
    ])
    monkeypatch.setattr(lm, "_engine_pids", lambda: {})
    lm._attribute_gpu_processes(cards)
    assert cards[0]["processes"] == []


def test_attribute_gpu_processes_no_nvidia_smi_never_raises(monkeypatch):
    cards = [{"index": 0, "uuid": "GPU-aaa"}]
    monkeypatch.setattr(gsm, "compute_apps", lambda: [])
    monkeypatch.setattr(lm, "_engine_pids", lambda: {})
    lm._attribute_gpu_processes(cards)
    assert cards[0]["processes"] == []


def test_attribute_gpu_processes_empty_cards_never_raises(monkeypatch):
    monkeypatch.setattr(gsm, "compute_apps", lambda: [{"gpu_uuid": "x", "pid": 1, "process_name": "y", "used_mb": 1}])
    lm._attribute_gpu_processes([])  # must not raise on an empty card list


def test_engine_pids_maps_listening_engine(monkeypatch):
    from src import engines as engines_mod
    from src import process_center

    monkeypatch.setattr(engines_mod, "list_engines", lambda: [
        {"id": "eng-1", "name": "Coding engine", "port": 8081},
        {"id": "eng-2", "name": "No port", "port": None},
    ])

    def fake_listening(port, **_kw):
        return {"pid": 111, "created_at": None, "cmdline": ""} if port == 8081 else None

    monkeypatch.setattr(process_center, "pid_listening_on", fake_listening)
    assert lm._engine_pids() == {111: "Coding engine"}


def test_engine_pids_never_raises_when_engines_module_fails(monkeypatch):
    from src import engines as engines_mod

    def boom():
        raise RuntimeError("profiles store unavailable")

    monkeypatch.setattr(engines_mod, "list_engines", boom)
    assert lm._engine_pids() == {}
