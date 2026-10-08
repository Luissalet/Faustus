import pytest

from src.action_intents import classify_tool_intent, message_needs_tools


def test_calendar_entry_request_promotes_to_agent():
    assert message_needs_tools("Can you add an entry to my calendar?")
    intent = classify_tool_intent("Can you add an entry to my calendar?")
    assert intent.needs_tools
    assert intent.category == "calendar"


def test_calendar_imperative_variants_promote_to_agent():
    assert message_needs_tools("add lunch with Sam to my calendar tomorrow at noon")
    assert message_needs_tools("schedule a call with Mina next Friday")
    assert message_needs_tools("put dentist appointment on my calendar")
    assert message_needs_tools("Alright. Recreate that same appointment")
    assert message_needs_tools("Okay delete that doctor appointment from the calendar")
    assert message_needs_tools("have another go at adding a test entry to the calendar")
    assert message_needs_tools(
        "Okay so you should be able to create that calendar event for tomorrow at 1:30 p.m. right for me to go to the hardware store"
    )
    assert message_needs_tools(
        "make it an appointment at 12pm for me to visit the doctor it's tomorrow the 2nd of June 2026"
    )


def test_calendar_read_requests_promote_to_agent():
    assert message_needs_tools("What upcoming events do I have?")
    assert message_needs_tools("Can you show my next appointments?")
    assert message_needs_tools("Do I have upcoming Taekwondo classes this week?")
    assert message_needs_tools("What's on my calendar tomorrow?")
    assert message_needs_tools("When is my next meeting?")


def test_note_todo_and_reminder_actions_promote_to_agent():
    assert message_needs_tools("add milk to my todo list")
    assert message_needs_tools("take a note that the server needs checking")
    assert message_needs_tools("set a reminder to call Pat at 4pm")


def test_email_and_ui_actions_promote_to_agent():
    assert message_needs_tools("reply to that email")
    assert message_needs_tools("mark those emails as read")
    assert message_needs_tools("open my calendar")
    assert message_needs_tools("turn off web search")


def test_research_action_promotes_to_agent():
    assert message_needs_tools("research cost effective local models")
    assert message_needs_tools("can you look into GPU hosting options")


def test_explicit_web_search_promotes_to_agent():
    assert message_needs_tools("use web search and find a recipe for chocolate chip cookies")
    assert message_needs_tools("do a web search for the best chocolate chip cookies")
    assert message_needs_tools("search the web for current RTX 3090 prices")
    assert classify_tool_intent("use web search and find a recipe").category == "web"


def test_workspace_agent_requests_promote_to_shell_workspace():
    prompts = [
        "fix the bug in this repo",
        "run the tests for this project",
        "debug the server logs",
        "run terminal-bench on this task",
        "inspect the traceback and patch the code",
    ]
    for prompt in prompts:
        intent = classify_tool_intent(prompt)
        assert intent.needs_tools
        assert intent.category == "workspace"


def test_explanatory_calendar_questions_stay_plain_chat():
    assert not message_needs_tools("How do I add an entry to my calendar?")
    assert not message_needs_tools("What about the built-in Faustus calendar, is that linked to email?")
    assert not message_needs_tools("Can you explain how calendar reminders work?")
    intent = classify_tool_intent("How do I add an entry to my calendar?")
    assert not intent.needs_tools
    assert intent.reason == "explanatory feature question"


def test_router_reports_non_calendar_categories():
    assert classify_tool_intent("reply to that email").category == "email"
    assert classify_tool_intent("open my calendar").category == "ui"
    assert classify_tool_intent("research cost effective local models").category == "research"


def test_spanish_project_objective_order_is_unicode_normalization_safe():
    intent = classify_tool_intent("Añade este documento a los objetivos del proyecto")
    assert intent.needs_tools
    assert intent.category == "project"


@pytest.mark.parametrize('message', [
    'Actualiza el brief con deck_update sobre el borrador. Corrige: CINCO objetivos específicos del TFM.',
    'Update the presentation brief. The thesis has five objectives.',
    'Crea una presentación del TFM; contiene cinco objetivos específicos.',
])
def test_thesis_objectives_after_document_request_are_not_dashboard_mutations(message):
    intent = classify_tool_intent(message)
    assert not (intent.category == 'project' and 'objective' in intent.reason.lower())


@pytest.mark.parametrize("message", [
    "Create a new project with the goal of finishing the release",
    "Set up a project including the objective to ship the release",
    "Crea un nuevo proyecto con la meta de preparar la defensa",
    "Update the project's goal to ship the release",
])
def test_native_project_mutation_scope_owns_generic_project_goal_intent(message):
    from src.action_intents import requires_builtin_project_objective_action
    assert classify_tool_intent(message).category == "project"
    assert not requires_builtin_project_objective_action(message, {"mcp__hub__project_goal_update"})


@pytest.mark.parametrize("message", [
    "Add this item to the active project's objectives board",
    "Update Faustus Objectives board after creating the project",
    "Create a project with a goal and apply it to Faustus Objectives board",
    "Call project_objectives to update the active project goal",
    "Añade este documento al tablero de objetivos del proyecto activo",
])
def test_explicit_active_project_objective_action_keeps_builtin_obligation(message):
    from src.action_intents import requires_builtin_project_objective_action
    assert requires_builtin_project_objective_action(message, {"mcp__hub__project_goal_update"})


def test_ambiguous_project_objective_keeps_legacy_builtin_default_without_native_scope():
    from src.action_intents import requires_builtin_project_objective_action
    assert requires_builtin_project_objective_action("Update the project's goal to ship the release")


@pytest.mark.parametrize(("tool_name", "message"), [
    ("mcp__hub__projects_new", "Create a project with the goal of shipping the release"),
    ("mcp__hub__goal_put", "Update the project's goal to ship the release"),
    ("mcp__hub__case_update", "Update the project's case goal to ship the release"),
])
def test_native_mutation_aliases_work_in_mixed_tool_scope(tool_name, message):
    from src.action_intents import requires_builtin_project_objective_action
    assert not requires_builtin_project_objective_action(
        message, {tool_name, "read_file", "web_search", "project_objectives"}
    )


def test_read_only_project_tool_does_not_claim_mutation_scope():
    from src.action_intents import requires_builtin_project_objective_action
    assert requires_builtin_project_objective_action(
        "Update the project's goal to ship the release",
        {"mcp__hub__project_get", "read_file", "web_search"},
    )


def test_project_objectives_capability_question_is_not_a_mutation_order():
    from src.action_intents import requires_builtin_project_objective_action
    tool_scope = {"mcp__hub__project_goal_update"}
    for message in (
        "Can I use project_objectives to track this?",
        "Could we use project_objectives to add goals?",
        "Why should we use project_objectives to update a goal?",
        "¿Puedo usar project_objectives para añadir una meta?",
        "List the available project_objectives actions",
        "Can you use project_objectives to show the settings?",
        "¿Puedes usar project_objectives para ver la ponencia?",
        "Ok, use project_objectives to address a creative project, find the closest item, check the linked note, and finish completely",
    ):
        assert not requires_builtin_project_objective_action(message, tool_scope), message


@pytest.mark.parametrize("message", [
    "Use project_objectives to update the active project goal",
    "Ok, use project_objectives to add the goal: finish chapter3",
    "Then, use project_objectives to add the next goal",
    "Can you use project_objectives to add this goal?",
    "Could you use project_objectives to add this goal?",
    "Usa project_objectives para actualizar la meta",
    "Vale, usa project_objectives para añadir la meta",
    "Y ahora, usa project_objectives para añadir la meta",
    "¿Puedes usar project_objectives para añadir esta meta?",
])
def test_actionable_project_objectives_tool_invocation_keeps_builtin_obligation(message):
    from src.action_intents import requires_builtin_project_objective_action
    assert requires_builtin_project_objective_action(
        message, {"mcp__hub__project_goal_update"}
    )


def test_objectives_board_question_does_not_create_a_mutation_obligation():
    from src.action_intents import requires_builtin_project_objective_action
    assert not requires_builtin_project_objective_action(
        "Does the Faustus Objectives board track project goals?",
        {"mcp__hub__project_goal_update"},
    )


@pytest.mark.parametrize("message", [
    "Controla mi pantalla y cierra la ventana de chatgpt",
    "Puedes controlar mi pantalla?",
    "Cierra la ventana de Spotify",
    "Por favor, minimiza la ventana de Spotify",
    "Could you close the window of Notepad?",
    "Take a screenshot of my desktop",
    "Mira mi pantalla",
])
def test_desktop_action_promotes_chat_to_agent(message):
    from src.action_intents import classify_tool_intent
    intent = classify_tool_intent(message)
    assert intent.needs_tools and intent.category == "desktop"


@pytest.mark.parametrize("message", [
    "Como puedo controlar mi pantalla?",
    "How do I close the window?",
    "Explica que significa captura de pantalla",
    "Escribe un cuento: cierra la ventana de la casa",
    'Traduce "controla mi pantalla" al ingles',
    "No cierres la ventana de Spotify",
])
def test_desktop_explanations_do_not_request_control(message):
    from src.action_intents import desktop_action_requested
    assert not desktop_action_requested(message)
