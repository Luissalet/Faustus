"""inspect_image: after N questions about one image with nothing written,
the answer asks the model to write its current best answer first."""
import asyncio

from src.agent_tools import image_inspect_tool as iit


def _run(monkeypatch, every, calls, session="s-write", write_after=None):
    monkeypatch.setattr(iit, "_write_every", lambda: every)

    async def fake(args, ctx):
        return {"output": "answer " + str(args.get("question")), "exit_code": 0}

    monkeypatch.setitem(iit._ACTIONS, "ask", fake)
    tool = iit.InspectImageTool()
    outs = []
    for i in range(calls):
        if write_after is not None and i == write_after:
            iit.note_written(session)
        outs.append(asyncio.run(tool.execute(
            '{"action":"ask","path":"C:/x/map.png","question":"q%d"}' % i, {"session_id": session})))
    return outs


def test_note_on_every_nth_look(monkeypatch):
    iit._LOOKS.clear()
    outs = _run(monkeypatch, 3, 7)
    flagged = [i for i, o in enumerate(outs) if "write your current best answer" in o["output"]]
    assert flagged == [2, 5]
    assert outs[2]["looks_since_write"] == 3
    assert "map.png" in outs[2]["output"]


def test_write_restarts_count(monkeypatch):
    iit._LOOKS.clear()
    outs = _run(monkeypatch, 3, 4, write_after=2)  # 1,2 | write | 1,2
    assert not any("write your current best answer" in o["output"] for o in outs)


def test_off_and_other_images(monkeypatch):
    iit._LOOKS.clear()
    assert not any("best answer" in o["output"] for o in _run(monkeypatch, 0, 7))
    iit._LOOKS.clear()
    _run(monkeypatch, 3, 2)
    assert iit.looks_since_write("s-write", "C:/x/map.png") == 2
    assert iit.looks_since_write("s-write", "C:/x/other.png") == 0
    assert iit.looks_since_write("other-session", "C:/x/map.png") == 0


def test_errors_carry_no_note(monkeypatch):
    iit._LOOKS.clear()
    monkeypatch.setattr(iit, "_write_every", lambda: 1)

    async def bad(args, ctx):
        return {"error": "nope", "exit_code": 1}

    monkeypatch.setitem(iit._ACTIONS, "ask", bad)
    out = asyncio.run(iit.InspectImageTool().execute('{"action":"ask","path":"a.png"}', {"session_id": "e"}))
    assert "best answer" not in str(out)

