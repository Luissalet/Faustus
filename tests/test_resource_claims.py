"""H18: resource claims before parallel tool calls, with a measured latency advantage.

Acceptance: no races, and compatible parallel calls finish faster than the same
calls run one after another while conflicting writes are serialized.
"""
import asyncio
import json
import os
import threading
import time
from types import SimpleNamespace

import pytest

from src import resource_claims as rc
from src.resource_claims import Mode, ClaimBroker, ClaimSet


@pytest.fixture(autouse=True)
def _fresh_broker():
    rc.reset_broker_for_tests()
    yield
    rc.reset_broker_for_tests()


def P(path, mode):
    return rc.path_claim(path, mode)


# ── compatibility matrix and overlap ─────────────────────────────────────────

@pytest.mark.parametrize("a,b,conflict", [
    (Mode.READ, Mode.READ, False),
    (Mode.READ, Mode.WRITE, True),
    (Mode.WRITE, Mode.READ, True),
    (Mode.WRITE, Mode.WRITE, True),
])
def test_matrix_on_the_same_path(tmp_path, a, b, conflict):
    f = str(tmp_path / "f.txt")
    assert rc.conflicts(P(f, a), P(f, b)) is conflict


def test_distinct_paths_never_conflict(tmp_path):
    assert not rc.conflicts(P(str(tmp_path / "a"), Mode.WRITE), P(str(tmp_path / "b"), Mode.WRITE))


def test_a_path_and_its_sibling_with_a_common_prefix_are_not_nested(tmp_path):
    assert not rc.conflicts(rc.tree_claim(str(tmp_path / "dir"), Mode.WRITE),
                            P(str(tmp_path / "dir2" / "f"), Mode.WRITE))


def test_tree_overlaps_paths_and_trees_inside_it(tmp_path):
    tree = rc.tree_claim(str(tmp_path / "src"), Mode.READ)
    assert rc.conflicts(tree, P(str(tmp_path / "src" / "a" / "b.py"), Mode.WRITE))
    assert rc.conflicts(tree, rc.tree_claim(str(tmp_path / "src" / "a"), Mode.WRITE))
    assert not rc.conflicts(tree, P(str(tmp_path / "src" / "a.py"), Mode.READ))
    assert not rc.conflicts(tree, P(str(tmp_path / "other" / "a.py"), Mode.WRITE))


def test_workspace_claim_overlaps_everything_under_its_root_only(tmp_path):
    ws = rc.workspace_claim(str(tmp_path / "ws"), Mode.WRITE)
    assert rc.conflicts(ws, P(str(tmp_path / "ws" / "x" / "y.py"), Mode.READ))
    assert rc.conflicts(ws, rc.workspace_claim(str(tmp_path / "ws"), Mode.READ))
    assert not rc.conflicts(ws, P(str(tmp_path / "elsewhere" / "y.py"), Mode.READ))


def test_process_and_external_claims_overlap_only_their_own_key(tmp_path):
    assert rc.conflicts(rc.process_claim("p1"), rc.process_claim("p1"))
    assert not rc.conflicts(rc.process_claim("p1"), rc.process_claim("p2"))
    assert rc.conflicts(rc.external_claim("mcp:a", Mode.WRITE), rc.external_claim("mcp:a", Mode.READ))
    assert not rc.conflicts(rc.external_claim("mcp:a", Mode.READ), rc.external_claim("mcp:a", Mode.READ))
    assert not rc.conflicts(rc.external_claim("mcp:a", Mode.WRITE), P(str(tmp_path / "f"), Mode.WRITE))
    assert not rc.conflicts(rc.process_claim("x"), rc.external_claim("x", Mode.WRITE))


def test_wildcard_overlaps_everything(tmp_path):
    w = rc.wildcard_claim()
    assert rc.conflicts(w, P(str(tmp_path / "f"), Mode.READ))
    assert rc.conflicts(w, rc.process_claim("p"))
    assert not rc.conflicts(rc.wildcard_claim(Mode.READ), P(str(tmp_path / "f"), Mode.READ))


def test_claim_set_admits_compatible_claims_and_refuses_a_conflicting_group(tmp_path):
    cs = ClaimSet()
    f, g = str(tmp_path / "f"), str(tmp_path / "g")
    assert cs.try_add([P(f, Mode.READ)])
    assert cs.try_add([P(f, Mode.READ)])          # read/read
    assert cs.try_add([P(g, Mode.WRITE)])         # different path
    assert not cs.try_add([P(f, Mode.WRITE)])     # write over a read
    assert len(cs) == 3                           # the refused set added nothing
    assert cs.conflict_for([P(g, Mode.READ)])[1].resource.key.endswith("g")


# ── claims derived from tool calls ───────────────────────────────────────────

def _labels(claims):
    return sorted((c.resource.kind, c.mode.value) for c in claims)


def test_read_of_a_file_and_of_a_directory(tmp_path):
    (tmp_path / "d").mkdir()
    (tmp_path / "f.py").write_text("x")
    ws = str(tmp_path)
    assert _labels(rc.claims_for_call("read_file", json.dumps({"path": "f.py"}), ws)) == [("path", "read")]
    assert _labels(rc.claims_for_call("grep", json.dumps({"path": "d", "pattern": "x"}), ws)) == [("tree", "read")]


def test_file_writes_claim_each_named_path(tmp_path):
    ws = str(tmp_path)
    c = rc.claims_for_call("write_file", json.dumps({"path": "a.py", "content": "x"}), ws)
    assert _labels(c) == [("path", "write")]
    patch = "*** Begin Patch\n*** Add File: a.txt\n+x\n*** Update File: sub/b.txt\n@@\n-a\n+b\n*** End Patch"
    c = rc.claims_for_call("apply_patch", patch, ws)
    assert _labels(c) == [("path", "write"), ("path", "write")]


def test_a_write_whose_targets_cannot_be_determined_takes_the_conservative_workspace_claim(tmp_path):
    ws = str(tmp_path)
    for tool, content in (("apply_patch", "not a patch at all"), ("write_file", "{}")):
        c = rc.claims_for_call(tool, content, ws)
        assert _labels(c) == [("workspace", "write")], tool
        # ...and it conflicts with any read or write inside the workspace
        assert rc.conflicts(c[0], P(str(tmp_path / "anything" / "x.py"), Mode.READ))


def test_a_workspace_wide_read_claims_the_workspace_for_reading(tmp_path):
    c = rc.claims_for_call("grep", json.dumps({"pattern": "x"}), str(tmp_path))
    assert _labels(c) == [("workspace", "read")]


def test_calls_the_claims_cannot_describe_claim_nothing(tmp_path):
    assert rc.claims_for_call("bash", json.dumps({"command": "rm -rf x"}), str(tmp_path)) == []
    assert rc.claims_for_call("definitely_not_a_tool", "{}", str(tmp_path)) == []
    assert rc.claims_for_call("read_file", json.dumps({"path": "f"}), str(tmp_path), scope="off") == []


def test_external_and_process_claims_only_in_the_wider_scope(tmp_path):
    ws = str(tmp_path)
    assert rc.claims_for_call("mcp__srv__do_thing", "{}", ws) == []
    wide = rc.claims_for_call("mcp__srv__do_thing", json.dumps({"pid": 7}), ws, scope="files_and_external")
    kinds = {(c.resource.kind, c.resource.key) for c in wide}
    assert ("external", "mcp:srv") in kinds and ("process", "7") in kinds


def test_scope_setting_defaults_and_rejects_nonsense(monkeypatch):
    from src import settings
    real = settings.get_setting
    monkeypatch.setattr(settings, "get_setting", lambda k, d=None: "banana" if k == "agent_resource_claims" else real(k, d))
    assert rc.scope_setting() == "files"


# ── the broker ───────────────────────────────────────────────────────────────

async def test_readers_share_and_a_writer_waits_for_them(tmp_path):
    b = ClaimBroker()
    f = str(tmp_path / "f")
    r1 = await b.acquire([P(f, Mode.READ)], owner="r1")
    r2 = await b.acquire([P(f, Mode.READ)], owner="r2")
    order = []

    async def writer():
        async with b.hold([P(f, Mode.WRITE)], owner="w"):
            order.append("w")

    task = asyncio.create_task(writer())
    await asyncio.sleep(0.05)
    assert order == [] and len(b.snapshot()["waiting"]) == 1
    b.release(r1)
    await asyncio.sleep(0.02)
    assert order == []          # still one reader
    b.release(r2)
    await asyncio.wait_for(task, 1)
    assert order == ["w"]


async def test_a_waiting_writer_is_not_overtaken_by_later_readers(tmp_path):
    b = ClaimBroker()
    f = str(tmp_path / "f")
    first = await b.acquire([P(f, Mode.READ)], owner="r1")
    order = []

    async def use(name, mode):
        async with b.hold([P(f, mode)], owner=name):
            order.append(name)
            await asyncio.sleep(0.01)

    w = asyncio.create_task(use("w", Mode.WRITE))
    await asyncio.sleep(0.02)
    r2 = asyncio.create_task(use("r2", Mode.READ))   # compatible with r1, but the writer is ahead
    await asyncio.sleep(0.05)
    assert order == []
    b.release(first)
    await asyncio.wait_for(asyncio.gather(w, r2), 1)
    assert order == ["w", "r2"]


async def test_a_claim_set_is_granted_all_or_nothing_so_two_runs_cannot_deadlock(tmp_path):
    b = ClaimBroker()
    a, c = str(tmp_path / "a"), str(tmp_path / "c")
    done = []

    async def run(name, first, second):
        async with b.hold([P(first, Mode.WRITE), P(second, Mode.WRITE)], owner=name):
            done.append(name)
            await asyncio.sleep(0.02)

    await asyncio.wait_for(asyncio.gather(run("x", a, c), run("y", c, a)), 2)
    assert sorted(done) == ["x", "y"]


async def test_timeout_names_the_blocker_and_releases_the_queue_slot(tmp_path):
    b = ClaimBroker()
    f = str(tmp_path / "f")
    held = await b.acquire([P(f, Mode.WRITE)], owner="holder", label="edit_file")
    with pytest.raises(rc.ClaimTimeout) as err:
        await b.acquire([P(f, Mode.READ)], owner="late", label="read_file", timeout=0.05)
    assert any("edit_file holds write path" in x for x in err.value.blockers)
    assert b.snapshot()["waiting"] == [] and b.snapshot()["stats"]["timeouts"] == 1
    b.release(held)
    lease = await asyncio.wait_for(b.acquire([P(f, Mode.WRITE)]), 1)
    b.release(lease)


async def test_a_cancelled_waiter_leaves_the_queue_and_does_not_block_others(tmp_path):
    b = ClaimBroker()
    f = str(tmp_path / "f")
    held = await b.acquire([P(f, Mode.WRITE)])
    waiter = asyncio.create_task(b.acquire([P(f, Mode.WRITE)], owner="cancelled"))
    await asyncio.sleep(0.02)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert b.snapshot()["waiting"] == []
    b.release(held)
    lease = await asyncio.wait_for(b.acquire([P(f, Mode.WRITE)]), 1)
    b.release(lease)
    assert b.snapshot()["held"] == []


async def test_a_holder_in_another_event_loop_wakes_a_waiter_here(tmp_path):
    b = ClaimBroker()
    f = str(tmp_path / "f")
    acquired, release = threading.Event(), threading.Event()

    def other_loop():
        async def main():
            lease = await b.acquire([P(f, Mode.WRITE)], owner="thread")
            acquired.set()
            while not release.is_set():
                await asyncio.sleep(0.005)
            b.release(lease)
        asyncio.run(main())

    t = threading.Thread(target=other_loop)
    t.start()
    assert acquired.wait(2)
    waiting = asyncio.create_task(b.acquire([P(f, Mode.READ)], owner="here"))
    await asyncio.sleep(0.03)
    assert not waiting.done()
    release.set()
    lease = await asyncio.wait_for(waiting, 2)
    b.release(lease)
    t.join(2)
    assert lease.waited_s > 0


async def test_run_with_claims_passes_through_when_nothing_is_claimed_or_off(tmp_path, monkeypatch):
    calls = []

    async def factory():
        calls.append(1)
        return ("desc", {"output": "ok"})

    blk = SimpleNamespace(tool_type="bash", content=json.dumps({"command": "ls"}))
    assert await rc.run_with_claims(blk, str(tmp_path), factory) == ("desc", {"output": "ok"})
    monkeypatch.setattr(rc, "scope_setting", lambda: "off")
    blk = SimpleNamespace(tool_type="write_file", content=json.dumps({"path": "a", "content": "x"}))
    assert await rc.run_with_claims(blk, str(tmp_path), factory) == ("desc", {"output": "ok"})
    assert len(calls) == 2 and rc.broker().snapshot()["stats"]["granted"] == 0


async def test_run_with_claims_refuses_a_call_it_could_not_get_claims_for(tmp_path, monkeypatch):
    monkeypatch.setattr(rc, "wait_seconds_setting", lambda: 0.05)
    f = tmp_path / "a.txt"
    held = await rc.broker().acquire([P(str(f), Mode.WRITE)], owner="other run", label="write_file")
    started = []

    async def factory():
        started.append(1)
        return ("desc", {"output": "ok"})

    blk = SimpleNamespace(tool_type="read_file", content=json.dumps({"path": str(f)}))
    desc, result = await rc.run_with_claims(blk, str(tmp_path), factory)
    assert started == []
    assert desc == "read_file: BLOCKED"
    assert result["error_code"] == "RESOURCE_CLAIM_TIMEOUT" and result["exit_code"] == 1
    assert result["status"] == "denied" and "write_file holds" in result["blockers"][0]
    rc.broker().release(held)


# ── measured advantage and serialization ─────────────────────────────────────

READ_S = 0.08


async def _timed(broker, claims, delay, track):
    async with broker.hold(claims, owner="bench"):
        track["now"] += 1
        track["max"] = max(track["max"], track["now"])
        await asyncio.sleep(delay)
        track["now"] -= 1


async def _sequential(claims_list, delay):
    t0 = time.monotonic()
    for _ in claims_list:
        await asyncio.sleep(delay)
    return time.monotonic() - t0


async def _parallel(claims_list, delay):
    broker = ClaimBroker()
    track = {"now": 0, "max": 0}
    t0 = time.monotonic()
    await asyncio.gather(*(_timed(broker, c, delay, track) for c in claims_list))
    return time.monotonic() - t0, track["max"], broker


async def test_benchmark_compatible_reads_finish_faster_than_sequential(tmp_path):
    f = str(tmp_path / "shared.txt")
    claims = [[P(f, Mode.READ)] for _ in range(4)] + [[P(str(tmp_path / f"o{i}"), Mode.READ)] for i in range(4)]
    seq = await _sequential(claims, READ_S)
    par, peak, broker = await _parallel(claims, READ_S)
    print(f"\n[bench] 8 compatible reads: sequential {seq:.3f}s, claims-parallel {par:.3f}s "
          f"({seq / par:.1f}x, peak concurrency {peak})")
    assert peak == 8
    assert par < seq / 3
    assert broker.snapshot()["stats"]["waited"] == 0


async def test_benchmark_conflicting_writes_are_serialized_and_independent_ones_overlap(tmp_path):
    same = str(tmp_path / "same.txt")
    claims = [[P(same, Mode.WRITE)] for _ in range(4)]
    par, peak, broker = await _parallel(claims, READ_S / 2)
    print(f"\n[bench] 4 writes to one file: {par:.3f}s, peak concurrency {peak}")
    assert peak == 1                       # mutual exclusion held throughout
    assert par >= 4 * (READ_S / 2) * 0.9   # they really ran one after another
    assert broker.snapshot()["stats"]["waited"] == 3

    apart = [[P(str(tmp_path / f"w{i}.txt"), Mode.WRITE)] for i in range(4)]
    par2, peak2, _ = await _parallel(apart, READ_S / 2)
    print(f"[bench] 4 writes to four files: {par2:.3f}s, peak concurrency {peak2}")
    assert peak2 == 4 and par2 < par / 2


async def test_benchmark_a_writer_never_overlaps_readers_of_the_same_file(tmp_path):
    f = str(tmp_path / "f.txt")
    broker = ClaimBroker()
    events = []

    async def use(name, mode):
        async with broker.hold([P(f, mode)], owner=name):
            events.append((name, "start", time.monotonic()))
            await asyncio.sleep(0.03)
            events.append((name, "end", time.monotonic()))

    await asyncio.gather(use("r1", Mode.READ), use("r2", Mode.READ), use("w", Mode.WRITE),
                         use("r3", Mode.READ))
    span = {n: {k: t for (m, k, t) in events if m == n} for n in ("r1", "r2", "w", "r3")}
    w = span["w"]
    for r in ("r1", "r2", "r3"):
        assert span[r]["end"] <= w["start"] or span[r]["start"] >= w["end"], (r, span)
    # the two readers ahead of the writer shared the file; the one behind waited for it
    assert span["r2"]["start"] < span["r1"]["end"]
    assert span["r3"]["start"] >= w["end"]


# ── the loop takes claims before it runs calls ───────────────────────────────

def _hold_in_thread(path, mode, seconds, release=None):
    """Hold a claim from another thread for `seconds`, or until `release` is set."""
    ready = threading.Event()

    def run():
        async def main():
            lease = await rc.broker().acquire([P(path, mode)], owner="other run", label="write_file")
            ready.set()
            if release is not None:
                await asyncio.to_thread(release.wait, seconds)
            else:
                await asyncio.sleep(seconds)
            rc.broker().release(lease)
        asyncio.run(main())

    t = threading.Thread(target=run)
    t.start()
    assert ready.wait(2)
    return t


def _run_loop(tmp_path, monkeypatch, call, security_bypass=False):
    import src.agent_loop as al
    from tests.test_agent_harness_loop import _collect, _events, _patch_common, _scripted_stream
    _patch_common(monkeypatch)
    ran = []

    async def exec_block(block, *a, **k):
        ran.append((block.tool_type, time.monotonic()))
        return (block.tool_type, {"output": "contents", "exit_code": 0})

    monkeypatch.setattr(al, "execute_tool_block", exec_block, raising=False)
    _scripted_stream(monkeypatch, [(call, "tool_calls"), ("Done. No files were changed.", "stop")])
    gen = al.stream_agent_loop(
        "http://127.0.0.1:11434/v1", "local-model",
        [{"role": "user", "content": "Read the file"}],
        max_rounds=4, relevant_tools={"read_file", "write_file"}, workspace=str(tmp_path),
        security_gate_bypass=security_bypass)
    return _events(_collect(gen)), ran


def test_loop_read_waits_for_a_write_another_run_holds(tmp_path, monkeypatch):
    target = tmp_path / "f.py"
    target.write_text("x")
    t0 = time.monotonic()
    thread = _hold_in_thread(str(target), Mode.WRITE, 1.2)
    events, ran = _run_loop(tmp_path, monkeypatch, '```read_file\n{"path": "f.py"}\n```')
    thread.join(2)
    assert [r[0] for r in ran] == ["read_file"]
    assert ran[0][1] - t0 >= 1.1, "the read started while another run still held the write claim"
    assert any(e.get("type") == "tool_output" and "contents" in e.get("output", "") for e in events)


def test_loop_read_of_another_file_does_not_wait(tmp_path, monkeypatch):
    (tmp_path / "f.py").write_text("x")
    (tmp_path / "g.py").write_text("y")
    # The writer of g.py lets go only when told to, so a slow machine cannot make
    # a read that waited look like one that did not.
    release = threading.Event()
    thread = _hold_in_thread(str(tmp_path / "g.py"), Mode.WRITE, 30, release=release)
    try:
        events, ran = _run_loop(tmp_path, monkeypatch, '```read_file\n{"path": "f.py"}\n```')
        still_held = not release.is_set() and thread.is_alive()
    finally:
        release.set()
        thread.join(5)
    assert ran and still_held            # the read ran while the other file's writer still held it


def test_loop_call_that_cannot_get_its_claims_is_refused_not_started(tmp_path, monkeypatch):
    target = tmp_path / "f.py"
    target.write_text("x")
    monkeypatch.setattr(rc, "wait_seconds_setting", lambda: 0.1)
    # Held until the loop is done: on Windows a turn can take several seconds to reach the call
    # (a refused localhost connection costs about 2 s per probe), and a fixed hold would expire first.
    release = threading.Event()
    thread = _hold_in_thread(str(target), Mode.WRITE, 60, release=release)
    try:
        events, ran = _run_loop(tmp_path, monkeypatch, '```read_file\n{"path": "f.py"}\n```')
    finally:
        release.set()
        thread.join(5)
    assert ran == []
    out = [e for e in events if e.get("type") == "tool_output"]
    assert out and "RESOURCE_CLAIM_TIMEOUT" in json.dumps(out) or "another call holds" in json.dumps(out)


def test_loop_group_does_not_put_a_workspace_write_next_to_a_read(tmp_path, monkeypatch):
    """A patch whose targets cannot be determined claims the workspace for writing, so it is
    not run beside a read of a file in it."""
    from src import agent_loop as al
    cs = ClaimSet()
    read = rc.claims_for_call("read_file", json.dumps({"path": "a.py"}), str(tmp_path))
    unknown_patch = rc.claims_for_call("apply_patch", "garbage", str(tmp_path))
    assert cs.try_add(read)
    assert not cs.try_add(unknown_patch)
    assert al._ClaimSet is ClaimSet


# ── preview / apply: external writer detection ───────────────────────────────

def test_fingerprint_and_drift_fields(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("abc")
    base = rc.fingerprint(str(f))
    assert base.known and base.exists and base.size == 3 and len(base.sha256) == 64
    assert rc.drift(base, rc.fingerprint(str(f))) == []
    # same bytes, newer mtime: an outside rewrite that restored the content
    st = os.stat(f)
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    assert rc.drift(base, rc.fingerprint(str(f))) == ["mtime_ns"]
    f.write_text("abcd")
    assert {"size", "sha256"} <= set(rc.drift(base, rc.fingerprint(str(f))))
    f.unlink()
    assert rc.drift(base, rc.fingerprint(str(f))) == ["exists"]
    assert rc.drift(rc.fingerprint(str(f)), rc.fingerprint(str(f))) == []   # still absent


def test_a_fingerprint_that_could_not_be_taken_never_matches(tmp_path):
    unknown = rc.FileFingerprint(known=False)
    assert rc.drift(unknown, rc.fingerprint(str(tmp_path))) != []
    binding = rc.PreviewBinding()
    binding._expected["x"] = unknown
    assert binding.unknown_paths() == ["x"]
    assert binding.verify() is None      # nothing it can prove; the caller decides


def test_preview_binding_reports_the_first_changed_file(tmp_path):
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_text("1")
    b.write_text("2")
    binding = rc.PreviewBinding().capture([str(a), str(b), str(tmp_path / "missing.txt")])
    assert binding.verify() is None
    b.write_text("22")
    path, expected, current, changed = binding.verify()
    assert path == str(b) and "size" in changed and expected.size == 1 and current.size == 2
    err = rc.external_writer_conflict("edit_file", path, expected, current, changed)
    assert err["error_code"] == "EXTERNAL_WRITER_DETECTED" and err["status"] == "conflict"
    assert err["exit_code"] == 1 and "another writer" in err["error"] and err["changed"] == changed
