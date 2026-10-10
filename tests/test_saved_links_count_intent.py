from src.answer_checks import saved_links_count_requested


def test_genuine_saved_link_count_requests_in_spanish_and_english():
    assert saved_links_count_requested("¿Cuántos enlaces tengo guardados en Links Hoard?")
    assert saved_links_count_requested("Usa Links Hoard para consultar cuántos enlaces tengo guardados.")
    assert saved_links_count_requested("Dime el número total de mis enlaces guardados.")
    assert saved_links_count_requested("¿Cuál es el número total de enlaces guardados en mi biblioteca?")
    assert saved_links_count_requested("How many saved links have I kept in Links Hoard?")
    assert saved_links_count_requested("Tell me the total number of my saved links.")
    assert saved_links_count_requested("Cuenta mis enlaces guardados en Links Hoard.")


def test_unrelated_review_and_board_totals_do_not_request_library_count():
    review_prompt = (
        "Revisa el cambio de Links Hoard sobre enlaces. "
        "El tablero tiene un total de 6 tareas."
    )
    assert not saved_links_count_requested(review_prompt)
    assert not saved_links_count_requested(
        "Review the Links Hoard changes about saved links; the board has 6 tasks total."
    )
    assert not saved_links_count_requested("What is the total number of tasks on my board?")
    assert not saved_links_count_requested(
        "Dime el total de enlaces guardados en el tablero de tareas."
    )


def test_quoted_or_negated_count_question_is_not_an_active_request():
    assert not saved_links_count_requested(
        "Revisa esta documentación; no ejecutes la pregunta citada: "
        "¿Cuántos enlaces tengo guardados en Links Hoard?"
    )
    assert not saved_links_count_requested(
        "Don't answer the quoted question: 'How many saved links do I have in Links Hoard?'"
    )
    assert not saved_links_count_requested("No me digas cuántos enlaces tengo guardados en Links Hoard.")


def test_link_count_without_library_or_saved_link_scope_is_not_enough():
    assert not saved_links_count_requested("¿Cuántos enlaces menciona esta página web?")
    assert not saved_links_count_requested("Links Hoard sirve para guardar y buscar enlaces.")


def test_later_task_review_context_does_not_change_the_counted_object():
    assert saved_links_count_requested(
        "¿Cuántos enlaces tengo guardados en Links Hoard para revisar las tareas?"
    )
    assert saved_links_count_requested(
        "How many links have I saved in Links Hoard to review my tasks?"
    )
    assert not saved_links_count_requested(
        "How many tasks can I do with saved links in Links Hoard?"
    )
