"""The same call again with only its numbers nudged is circling, not progress.

Live, 24-09-2026: a text-only 27B asked the vision model the same question
about the same page six times, each with a crop region shifted by a few
hundredths; none of the exact-repeat or cycle rules saw it.
"""
import json

from src.loop_breaker import LoopPolicy

Q = "Transcribe EXACTAMENTE el texto de esta zona. Indica cuántos guiones tiene cada espacio."


def _call(region):
    return json.dumps({"path": "vistas/6a.jpg", "action": "ask", "region": region, "zoom": 3, "question": Q})


LIVE = [[0.5, 0.78, 1, 0.95], [0.5, 0.45, 1, 0.7], [0.5, 0.7, 1, 0.82], [0.5, 0.82, 1, 0.92],
        [0.5, 0.78, 1, 0.9]]


def test_the_live_sequence_gets_one_nudge():
    p = LoopPolicy()
    actions = [p.observe("inspect_image", _call(r), f"h{i}") for i, r in enumerate(LIVE)]
    assert actions[-1] == "nudge" and p.last_trigger == "revisit"
    assert actions[:-1] == ["none"] * 4
    # ...once per run
    assert p.observe("inspect_image", _call([0.5, 0.8, 1, 0.93]), "h9") == "none"


def test_reading_page_after_page_is_progress():
    p = LoopPolicy()
    for page in range(1, 12):
        assert p.observe("pdf_read_section", json.dumps({"path": "a.pdf", "page": page}), f"r{page}") == "none"


def test_distinct_regions_that_tile_a_page_are_progress():
    p = LoopPolicy()
    tiles = [[0, 0, 0.5, 0.5], [0.5, 0, 1, 0.5], [0, 0.5, 0.5, 1], [0.5, 0.5, 1, 1]]
    assert [p.observe("inspect_image", _call(t), f"t{i}") for i, t in enumerate(tiles)] == ["none"] * 4


def test_a_different_question_starts_a_new_run():
    p = LoopPolicy()
    for i, r in enumerate(LIVE[:3]):
        p.observe("inspect_image", _call(r), f"a{i}")
    other = json.dumps({"path": "vistas/6a.jpg", "action": "ask", "region": LIVE[3], "question": "otra"})
    assert p.observe("inspect_image", other, "b") == "none"
    assert p.observe("inspect_image", _call(LIVE[4]), "c") == "none"


def test_rewording_the_question_over_the_same_region_is_still_circling():
    """Live: the same corner re-cropped with the question rephrased each time."""
    p = LoopPolicy()
    qs = ["Transcribe exactly the handwritten text in this region, line by line.",
          "Transcribe the handwritten quotes here exactly and count the underscores in each blank.",
          "Transcribe exactly the text of this zone, preserving blanks and underscores.",
          "Transcribe exactly each line of text in this region and the blanks."]
    region = [0.45, 0.72, 1, 0.98]
    acts = []
    for i, q in enumerate(qs):
        acts.append(p.observe("inspect_image", json.dumps(
            {"path": "vistas/6a.jpg", "region": region, "zoom": 3, "question": q}), f"h{i}"))
    assert "nudge" in acts and p.last_trigger == "revisit", acts


def test_different_files_are_different_runs():
    p = LoopPolicy()
    for i, name in enumerate(["alpha_report.csv", "beta_summary.md", "gamma_notes.txt", "delta_plan.json"]):
        assert p.observe("read_file", json.dumps({"path": name, "offset": 1, "limit": 50}), f"r{i}") == "none"


def test_shell_commands_that_differ_are_not_one_run():
    p = LoopPolicy()
    cmds = ["sed -n 1,50p src/alpha_module.py && echo done reading the alpha module",
            "sed -n 1,50p src/beta_service.py && echo done reading the beta service",
            "sed -n 1,50p src/gamma_worker.py && echo done reading the gamma worker",
            "sed -n 1,50p docs/overview_notes.md && echo done reading the overview notes"]
    for i, c in enumerate(cmds):
        assert p.observe("bash", json.dumps({"command": c}), f"s{i}") == "none"
