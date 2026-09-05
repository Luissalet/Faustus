"""B-012: invisible mutations and lost updates in the settings store.

Two separate bugs lived in `src/settings.py`:

  1. `load_settings()` returned the cache's own dict, so mutating the result
     changed what every other reader saw — with nothing written to disk to
     explain it;
  2. read-modify-write had no revision and no lock, so two writers who both
     read the same document each wrote the whole thing back and the second
     silently erased the first. Atomic writes never protected against this;
     they protect against a *truncated* file, which is a different accident.

The threads and the subprocesses below are the point: neither bug shows up in
a single-threaded test.
"""

import json
import os
import subprocess
import sys
import textwrap
import threading

import pytest

from src import settings as settings_module
from core.file_lock import FileLock, LockTimeout


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A settings.json of our own, with the caches cleared around the test."""
    path = tmp_path / "settings.json"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(settings_module, "SETTINGS_FILE", str(path))
    settings_module._invalidate_caches()
    # The TTL cache would otherwise hide a write made microseconds ago.
    monkeypatch.setattr(settings_module, "_CACHE_TTL", 0.0)
    yield path
    settings_module._invalidate_caches()


# ── 1. no shared mutation ──────────────────────────────────────────────────

def test_mutating_the_result_changes_nothing_for_anybody(store):
    first = settings_module.load_settings()
    first["default_model"] = "smuggled-in"
    assert settings_module.load_settings().get("default_model") != "smuggled-in"
    assert settings_module.get_setting("default_model") != "smuggled-in"
    assert "smuggled-in" not in store.read_text(encoding="utf-8")


def test_two_readers_get_two_objects(store):
    a, b = settings_module.load_settings(), settings_module.load_settings()
    assert a is not b
    a["default_model"] = "mine"
    assert b.get("default_model") != "mine"


def test_a_nested_value_is_copied_too(store):
    """A shallow copy would still hand out the same inner list."""
    key = next((k for k, v in settings_module.DEFAULT_SETTINGS.items()
                if isinstance(v, list)), None)
    if key is None:
        pytest.skip("no list-valued setting to check")
    settings_module.update_settings({key: ["one"]})
    got = settings_module.load_settings()[key]
    got.append("two")
    assert settings_module.load_settings()[key] == ["one"]
    also = settings_module.get_setting(key)
    also.append("three")
    assert settings_module.get_setting(key) == ["one"]


def test_threads_reading_and_mutating_do_not_infect_each_other(store):
    settings_module.update_settings({"default_model": "base"})
    seen = []

    def reader():
        for _ in range(200):
            copy = settings_module.load_settings()
            copy["default_model"] = "local-only"
            seen.append(settings_module.get_setting("default_model"))

    threads = [threading.Thread(target=reader) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert set(seen) == {"base"}


# ── 2. revisions and conflicts ─────────────────────────────────────────────

def test_the_revision_moves_and_never_leaks_into_the_settings(store):
    assert settings_module.settings_revision() == 0
    result = settings_module.update_settings({"default_model": "one"})
    assert result["revision"] == 1
    assert settings_module.settings_revision() == 1
    assert settings_module.REVISION_KEY not in result["settings"]
    assert settings_module.REVISION_KEY not in settings_module.load_settings()
    # ...but it IS on disk, which is where it has to be to survive a restart.
    on_disk = json.loads(store.read_text(encoding="utf-8"))
    assert on_disk[settings_module.REVISION_KEY] == 1


def test_a_stale_writer_is_told_instead_of_winning(store):
    revision = settings_module.settings_revision()
    settings_module.update_settings({"default_model": "written-by-alice"})
    with pytest.raises(settings_module.SettingsConflict) as raised:
        settings_module.update_settings({"default_model": "written-by-bob"},
                                        expected_revision=revision)
    assert raised.value.expected == revision
    assert raised.value.actual == revision + 1
    # Alice's write is intact; Bob's was refused, not merged.
    assert settings_module.get_setting("default_model") == "written-by-alice"


def test_the_current_revision_is_accepted(store):
    settings_module.update_settings({"default_model": "one"})
    result = settings_module.update_settings(
        {"default_model": "two"}, expected_revision=settings_module.settings_revision())
    assert result["settings"]["default_model"] == "two"
    assert result["revision"] == 2


def test_a_patch_leaves_the_other_keys_alone(store):
    settings_module.update_settings({"default_model": "keep-me"})
    settings_module.update_settings({"default_endpoint_id": "ep-1"})
    assert settings_module.get_setting("default_model") == "keep-me"
    assert settings_module.get_setting("default_endpoint_id") == "ep-1"


# ── 3. per-key validation ──────────────────────────────────────────────────

def test_an_unknown_key_is_refused(store):
    with pytest.raises(settings_module.SettingsError):
        settings_module.update_settings({"totally_made_up": 1})
    assert "totally_made_up" not in store.read_text(encoding="utf-8")


def test_the_revision_key_cannot_be_written_by_a_caller(store):
    with pytest.raises(settings_module.SettingsError):
        settings_module.update_settings({settings_module.REVISION_KEY: 99})


def test_a_wrong_type_is_refused(store):
    boolean = next((k for k, v in settings_module.DEFAULT_SETTINGS.items()
                    if isinstance(v, bool)), None)
    assert boolean, "expected at least one boolean setting"
    with pytest.raises(settings_module.SettingsError):
        settings_module.update_settings({boolean: "yes please"})
    settings_module.update_settings({boolean: True})  # the real type is fine


def test_a_number_does_not_pass_as_a_boolean(store):
    """`True` is an int in Python; the validator must not be fooled."""
    integer = next((k for k, v in settings_module.DEFAULT_SETTINGS.items()
                    if isinstance(v, int) and not isinstance(v, bool)), None)
    if integer is None:
        pytest.skip("no int-valued setting to check")
    with pytest.raises(settings_module.SettingsError):
        settings_module.update_settings({integer: True})


def test_none_clears_a_value(store):
    settings_module.update_settings({"default_model": "something"})
    settings_module.update_settings({"default_model": None})
    assert settings_module.get_setting("default_model") is None


# ── 4. lost updates, for real: threads, then processes ─────────────────────

def test_concurrent_patches_all_survive(store):
    """Twenty threads, twenty different keys, retrying on conflict.

    Every write must end up on disk: that is exactly what the old
    read-modify-write could not promise.
    """
    keys = [k for k, v in settings_module.DEFAULT_SETTINGS.items()
            if isinstance(v, str)][:20]
    assert len(keys) >= 5, "need a handful of string settings for this"
    errors = []

    def writer(key):
        for _ in range(25):
            try:
                revision = settings_module.settings_revision()
                settings_module.update_settings({key: f"set-by-{key}"},
                                                expected_revision=revision)
                return
            except settings_module.SettingsConflict:
                continue
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
                return
        errors.append(RuntimeError(f"{key} never got through"))

    threads = [threading.Thread(target=writer, args=(k,)) for k in keys]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    final = settings_module.load_settings()
    for key in keys:
        assert final[key] == f"set-by-{key}", f"{key} was lost"


_CHILD = textwrap.dedent("""
    import sys, time
    sys.path.insert(0, %(repo)r)
    from src import settings
    settings.SETTINGS_FILE = %(path)r
    settings._CACHE_TTL = 0.0
    key, value, hold = sys.argv[1], sys.argv[2], float(sys.argv[3])
    for _ in range(200):
        try:
            revision = settings.settings_revision()
            time.sleep(hold)          # the window a lost update needs
            settings.update_settings({key: value}, expected_revision=revision)
            print("ok")
            break
        except settings.SettingsConflict:
            continue
    else:
        print("gave up")
""")


def test_two_processes_cannot_lose_each_others_write(store, tmp_path):
    """The half a thread test cannot reach: a second interpreter.

    Both children read, wait, then write. Without the file lock and the
    revision one of the two values would simply not be there at the end.
    """
    repo = os.path.dirname(os.path.dirname(os.path.abspath(settings_module.__file__)))
    script = tmp_path / "child.py"
    script.write_text(_CHILD % {"repo": repo, "path": str(store)}, encoding="utf-8")

    children = [
        subprocess.Popen([sys.executable, str(script), "default_model", "from-a", "0.15"],
                         stdout=subprocess.PIPE, text=True),
        subprocess.Popen([sys.executable, str(script), "default_endpoint_id", "from-b", "0.15"],
                         stdout=subprocess.PIPE, text=True),
    ]
    outputs = [child.communicate(timeout=120)[0].strip() for child in children]
    assert outputs == ["ok", "ok"], outputs

    settings_module._invalidate_caches()
    final = settings_module.load_settings()
    assert final["default_model"] == "from-a"
    assert final["default_endpoint_id"] == "from-b"


# ── 5. the lock itself ─────────────────────────────────────────────────────

def test_the_lock_excludes_a_second_holder(tmp_path):
    path = str(tmp_path / "x.lock")
    with FileLock(path):
        with pytest.raises(LockTimeout):
            FileLock(path, timeout=0.1).acquire()
    FileLock(path, timeout=0.1).acquire().release()  # released, so free again


def test_a_stale_lock_is_broken_rather_than_wedging_the_store(tmp_path):
    """A crashed holder must not need a human with a shell to recover."""
    path = str(tmp_path / "y.lock")
    held = FileLock(path).acquire()
    old = os.path.getmtime(path) - 120
    os.utime(path, (old, old))
    FileLock(path, timeout=1.0, stale_after=30.0).acquire().release()
    held.release()


def test_a_lock_left_behind_does_not_break_a_later_write(store, tmp_path):
    stale = str(store) + ".lock"
    with open(stale, "w", encoding="utf-8") as handle:
        handle.write("{}")
    old = os.path.getmtime(stale) - 600
    os.utime(stale, (old, old))
    settings_module.update_settings({"default_model": "after-the-crash"})
    assert settings_module.get_setting("default_model") == "after-the-crash"


# ── 6. the legacy write path still behaves ────────────────────────────────

def test_save_settings_still_writes_a_whole_document_and_bumps_the_revision(store):
    settings_module.update_settings({"default_model": "one"})
    before = settings_module.settings_revision()
    settings_module.save_settings({"default_model": "whole-document"})
    assert settings_module.settings_revision() == before + 1
    assert settings_module.get_setting("default_model") == "whole-document"
    assert settings_module.REVISION_KEY not in settings_module.load_settings()


def test_save_settings_does_not_persist_a_callers_revision_key(store):
    settings_module.save_settings({"default_model": "x",
                                   settings_module.REVISION_KEY: 4242})
    on_disk = json.loads(store.read_text(encoding="utf-8"))
    assert on_disk[settings_module.REVISION_KEY] != 4242
