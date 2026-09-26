import src.deliverables as d


def test_requested_files_are_found_in_spanish_and_english():
    assert d.requested_files("Escribe tu respuesta final en RESPUESTA.md con las fuentes") == ["RESPUESTA.md"]
    assert d.requested_files("Guarda el resumen en informes/semana.csv") == ["semana.csv"]
    assert d.requested_files("Write the report to out/report.md and save data.json") == ["report.md", "data.json"]
    assert d.requested_files("Lee RESPUESTA.md y dime qué opinas") == []
    assert d.requested_files("¿Qué hora es?") == []


def test_written_looks_at_write_tools_only():
    events = [{"tool": "read_file", "command": '{"path": "RESPUESTA.md"}'},
              {"tool": "write_file", "command": {"path": "notas.md", "content": "x"}}]
    assert not d.written(["RESPUESTA.md"], events)
    events.append({"tool": "write_file", "command": '{"path": "C:/x/RESPUESTA.md", "content": "# R"}'})
    assert d.written(["respuesta.md"], events)


def test_exists_in_finds_a_file_a_previous_turn_created(tmp_path):
    (tmp_path / "sub").mkdir()
    assert not d.exists_in(str(tmp_path), ["RESPUESTA.md"])
    (tmp_path / "sub" / "RESPUESTA.md").write_text("# R", encoding="utf-8")
    assert d.exists_in(str(tmp_path), ["respuesta.md"])
    assert not d.exists_in("", ["RESPUESTA.md"])
