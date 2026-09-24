"""After an approval resume, the note names the plan's finished steps and the
step to continue (live, exam run 15: the model re-read the brief and
re-described the page because the resume note ended with the saved request,
which begins by telling it to read the brief)."""
from src import agent_loop as al
from src.agent_tools import coding_tools


def test_resume_line_names_done_steps_and_the_current_one(monkeypatch):
    todos = [
        {"content": "Leer enunciado", "status": "completed"},
        {"content": "Examinar originales", "status": "completed"},
        {"content": "Conectar las pistas", "status": "in_progress"},
        {"content": "Redactar respuesta", "status": "pending"},
    ]
    monkeypatch.setattr(coding_tools, "load_todos", lambda sid: todos)
    line = al._resume_plan_line("sess-1")
    assert "Leer enunciado; Examinar originales" in line
    assert "do not redo them" in line
    assert "Continue with the step: Conectar las pistas" in line


def test_resume_line_takes_the_first_open_step_when_none_is_in_progress(monkeypatch):
    monkeypatch.setattr(coding_tools, "load_todos", lambda sid: [
        {"content": "A", "status": "completed"}, {"content": "B", "status": "pending"}])
    assert "Continue with the step: B" in al._resume_plan_line("s")


def test_no_plan_or_nothing_open_gives_nothing(monkeypatch):
    monkeypatch.setattr(coding_tools, "load_todos", lambda sid: [])
    assert al._resume_plan_line("s") == ""
    monkeypatch.setattr(coding_tools, "load_todos", lambda sid: [{"content": "A", "status": "completed"}])
    assert al._resume_plan_line("s") == ""
    assert al._resume_plan_line(None) == ""
