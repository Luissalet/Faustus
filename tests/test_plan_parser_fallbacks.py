"""A plan written without headings or numbers still yields tasks: top-level
bullets or lines with their own step word. Prose with fewer than three such
steps stays at 0 tasks, as before."""

from src.plan_tracker import parse_plan


def test_bullet_plan():
    body = ("Plan para migrar la base de datos\n\n"
            "- Exportar las tablas actuales a CSV\n  con cabeceras\n"
            "- Crear el esquema nuevo en Postgres\n"
            "- Importar los CSV y comprobar los recuentos\n")
    spec = parse_plan("plan.md", body)
    assert [t.title for t in spec.tasks] == [
        "Exportar las tablas actuales a CSV", "Crear el esquema nuevo en Postgres",
        "Importar los CSV y comprobar los recuentos"]
    assert "cabeceras" in spec.tasks[0].text


def test_step_word_plan():
    body = ("Paso 1: preparar el entorno.\nInstalar dependencias.\n"
            "Paso 2: escribir los tests.\nPaso 3 - desplegar en staging.\n")
    spec = parse_plan("plan.txt", body)
    assert len(spec.tasks) == 3
    assert spec.tasks[0].title.startswith("Paso 1") and "dependencias" in spec.tasks[0].text


def test_prose_and_short_lists_stay_empty():
    assert parse_plan("carta.txt", "Querida Ana:\n\nTe escribo para contarte que el viaje fue bien.\n").tasks == []
    assert parse_plan("x.txt", "- una cosa\n- otra\n").tasks == []


def test_numbered_plans_still_win_over_bullets():
    body = "1. Primero\n- detalle\n2. Segundo\n3. Tercero\n"
    assert [t.title for t in parse_plan("p.md", body).tasks] == ["Primero", "Segundo", "Tercero"]
