"""Natural-language examples per tool, for the tool index (OBJ-7, lote 91).

Luis's framing: "un usuario no debería saberse de memoria todas las tools;
'dime nosequé para el proyecto X' tiene que bastar." This module holds no
logic — it is a data table, ``EXAMPLES``, mapping each registered tool name
to a handful of colloquial requests (Spanish and English, mixed) that a real
user would type to want that tool, without ever naming the tool, a route, or
an argument. ``src.tool_index`` folds these into the text it indexes and
searches, so both the embedding lane and the keyword/lexical fallback can
match plain speech to the right tool.

Coverage: every tool in ``src.agent_tools.TOOL_HANDLERS`` gets an entry (4-8
phrases; genuinely internal/rare tools get at least 2). The project board
tools (``board_*``, lote 92) are included too even though they may not be
registered yet when this module is imported — ``tool_index`` only pulls
examples for names it already has a description for, so an unregistered
``board_*`` key here is inert data until lote 92 lands, at which point it is
picked up with no further change needed here (see BOARD_TOOL_NAMES below,
which the lote 91 benchmark uses to treat those rows as ``pending``).

Keep phrases as a REAL USER would type them: no tool names, no snake_case,
no file paths as an API would spell them, no "use the X tool". Accents are
used where natural Spanish would have them; do not strip them here — the
tool index folds a copy without accents on its own (see
``tool_index._examples_block``), so both spellings are matched at retrieval
time from a single, natural-looking source list.
"""

from __future__ import annotations

from typing import Dict, List

# The board tools (lote 92) may not exist in TOOL_HANDLERS yet when lote 91
# lands — see BRIEF_CIERRE / CONTRATO_BOARD. Naming them here, separately
# from EXAMPLES, lets the lote 91 benchmark (tests/test_l91_*.py) recognize
# these rows and skip them from the pass/fail count until they are real.
BOARD_TOOL_NAMES: frozenset[str] = frozenset({
    "board_list", "board_ready", "board_get", "board_create",
    "board_update", "board_comment", "board_link", "board_claim",
})

EXAMPLES: Dict[str, List[str]] = {
    # ── Media inspection/conversion ──────────────────────────────────────
    "plan_media_transform": [
        "¿puedo convertir esta imagen a PNG sin perder calidad?",
        "what would happen if I resized this photo to 800px wide",
        "antes de convertirlo, dime si esta imagen tiene transparencia",
        "check if I can pull the audio out of this video before I do it",
    ],
    "transform_media": [
        "convierte esta foto a JPG",
        "extract the audio from this video as an MP3",
        "reduce la resolución de esta imagen a la mitad",
        "convert this WebP image to PNG",
        "sácame el audio de este vídeo en WAV",
        "resize this picture down for the website",
    ],
    "bash": [
        "instala este paquete con pip",
        "run npm install in this project",
        "mira qué procesos están usando el puerto 8000",
        "reinicia el servidor",
        "check disk usage on this machine",
        "clona este repositorio con git",
    ],
    "python": [
        "calcula el promedio de esta lista de números",
        "write a quick script to parse this CSV",
        "haz un cálculo rápido de compuesto de interés",
        "run some Python to process this data",
    ],
    "web_search": [
        "¿qué tiempo hace hoy en Madrid?",
        "what's the current price of Bitcoin",
        "busca cuánto cuesta un iPhone 16 en España",
        "look up the latest news about the election",
        "cuál es el marcador del partido de anoche",
        "search for the population of Japan",
    ],
    "web_fetch": [
        "abre este enlace y dime de qué trata",
        "check example.com and tell me what it says",
        "lee el contenido de esta página web",
        "fetch this article and summarize it",
        "mira esta URL que te paso",
    ],
    "read_file": [
        "lee el archivo config.py",
        "show me what's inside server.js",
        "abre el fichero de logs",
        "what does the README say",
        "enséñame las primeras 50 líneas de este archivo",
    ],
    "inspect_media": [
        "¿de qué tamaño es esta imagen?",
        "how long is this video",
        "dime la resolución y duración de este archivo",
        "does this audio file have stereo channels",
        "cuántos fotogramas por segundo tiene este vídeo",
    ],
    "grep": [
        "busca dónde se usa esta función en el código",
        "find every place the word TODO appears in this repo",
        "encuentra el texto 'API_KEY' en los archivos",
        "search the codebase for this error message",
        "dónde está definida esta variable de entorno",
    ],
    "glob": [
        "encuentra todos los archivos .py de este proyecto",
        "find every test file in the repo",
        "lista los ficheros que acaban en .json",
        "which files match *.tsx",
    ],
    "ls": [
        "qué hay en esta carpeta",
        "list the files in the src directory",
        "muéstrame el contenido de la carpeta de proyectos",
        "what's inside this folder",
    ],
    "find_symbol": [
        "dónde está definida la clase UserManager",
        "where is the function calculate_total defined",
        "encuentra la definición de esta constante",
        "find where this method is implemented",
    ],
    "callers": [
        "quién llama a esta función",
        "who calls calculate_total anywhere in the code",
        "dónde se usa este método en el resto del proyecto",
    ],
    "tests_for": [
        "qué tests cubren este archivo",
        "find the tests for this module",
        "hay algún test para esta función",
    ],
    "get_workspace": [
        "en qué carpeta estamos trabajando",
        "what's the active project folder",
        "cuál es la ruta del workspace actual",
        "where am I working right now",
    ],
    "write_file": [
        "crea un archivo config.json con estos datos",
        "write a new file called app.py with this code",
        "guarda esto en un fichero llamado notas.txt",
        "make a new script from scratch with this content",
        "save this as a new .env file",
    ],
    "edit_file": [
        "arregla el bug en utils.py",
        "fix the typo in main.py line 40",
        "cambia esta función en server.js",
        "edita el archivo de configuración para que use el puerto 8080",
        "update this file to use the new API",
    ],
    "apply_patch": [
        "aplica estos cambios en varios archivos a la vez",
        "apply this multi-file patch to the repo",
        "haz este refactor que toca varios ficheros",
        "implementa este cambio grande que afecta a varios archivos",
    ],
    "todowrite": [
        "vamos apuntando los pasos de esta tarea de código a medida que avanzo",
        "keep a checklist of what's left to do in this coding session",
        "marca este paso como hecho y sigue con el siguiente",
    ],
    "delegate_agents": [
        "divide este trabajo grande en varias partes en paralelo",
        "spin up a few workers to tackle this independently",
        "necesito que varios agentes trabajen a la vez en esto",
        "split this big refactor across parallel workers",
    ],

    # ── Documents (editor panel) ─────────────────────────────────────────
    "create_document": [
        "escribe un artículo sobre el cambio climático",
        "draft a cover letter for this job",
        "componme un poema corto sobre el mar",
        "write me a long blog post about remote work",
        "haz un documento con el resumen de la reunión",
        "start a new essay about the French revolution",
    ],
    "edit_document": [
        "cambia el segundo párrafo del documento",
        "fix the typo in that paragraph",
        "añade una sección sobre precios al documento",
        "tweak the intro of the doc I have open",
        "rename that section to 'Conclusion'",
    ],
    "update_document": [
        "reescribe el documento entero desde cero",
        "rewrite the whole draft completely, it's not working",
        "sustituye todo el contenido del documento por esta nueva versión",
        "throw out the current draft and replace it entirely",
    ],
    "suggest_document": [
        "dame feedback sobre este documento",
        "review my essay and suggest edits",
        "proofread this and tell me what to change",
        "qué mejorarías de este texto",
        "critique this draft for me",
    ],
    "manage_documents": [
        "muéstrame mis documentos guardados",
        "list all my saved docs",
        "abre el documento del informe anterior",
        "borra ese documento antiguo",
        "open the file I was writing yesterday",
        "delete that old draft",
    ],

    # ── Media/AI helpers ──────────────────────────────────────────────────
    "generate_image": [
        "genera una imagen de un gato astronauta",
        "make me a picture of a sunset over mountains",
        "créame una ilustración estilo acuarela",
        "generate a photo-realistic image of a city at night",
    ],
    "chat_with_model": [
        "pregúntale a otro modelo qué opina de esto",
        "ask a different AI what it thinks about this plan",
        "consulta esto con otro modelo a ver qué dice",
        "get a second answer from another model",
    ],
    "ask_teacher": [
        "esto es muy difícil, pídele ayuda a un modelo más potente",
        "escalate this to a smarter model, I'm stuck",
        "necesito una segunda opinión de un modelo más capaz",
    ],
    "pipeline": [
        "encadena varios modelos para resolver esto paso a paso",
        "run this through a multi-step model pipeline",
        "combina varios modelos en secuencia para esta tarea",
    ],
    "list_models": [
        "qué modelos tengo disponibles",
        "what models can I use right now",
        "enséñame la lista de modelos y sus endpoints",
    ],
    "edit_image": [
        "quítale el fondo a esta imagen",
        "upscale this photo, it's too low-res",
        "rellena esta parte de la imagen que falta",
        "mejora la resolución de esta foto",
    ],

    # ── Sessions/chats ────────────────────────────────────────────────────
    "create_session": [
        "abre un chat nuevo",
        "start a new chat called Research",
        "créame una conversación nueva con el modelo grande",
    ],
    "list_sessions": [
        "enséñame todos mis chats",
        "list my chats",
        "qué conversaciones tengo abiertas",
    ],
    "send_to_session": [
        "mándale este mensaje al otro chat",
        "send this to my other session",
        "reenvía esto a la conversación de trabajo",
    ],
    "search_chats": [
        "busca en mis conversaciones anteriores algo sobre el contrato",
        "search my past chats for that recipe I asked about",
        "en qué chat hablamos de esto antes",
    ],
    "search_project_chats": [
        "busca en las conversaciones de este proyecto qué decidimos sobre el diseño",
        "search earlier chats in this project for that decision",
        "qué se dijo antes en este proyecto sobre esto",
    ],
    "manage_session": [
        "renombra este chat a 'Presupuesto 2025'",
        "archive this conversation",
        "borra esa sesión antigua",
        "duplica este chat para probar algo distinto",
        "delete my last three chats",
    ],

    # ── Memory / skills / settings / integrations ────────────────────────
    "manage_memory": [
        "recuerda que vivo en Madrid",
        "remember that I prefer dark mode",
        "olvida lo que te dije sobre mi trabajo anterior",
        "what do you remember about me",
        "busca en tu memoria si sabes mi cumpleaños",
    ],
    "manage_skills": [
        "guarda esto como una skill reutilizable",
        "publish this as a reusable skill",
        "busca skills relacionadas con redacción",
    ],
    "manage_tasks": [
        "hazlo cada mañana automáticamente",
        "set this up to run every day at 8am",
        "programa una tarea semanal para resumir el correo",
        "pausa esa tarea automática",
        "cancel that recurring job",
    ],
    "manage_endpoints": [
        "añade un nuevo endpoint de modelo",
        "list my configured model endpoints",
        "borra ese endpoint que ya no uso",
    ],
    "manage_mcp": [
        "conecta este servidor MCP",
        "list the tools this MCP server offers",
        "reconecta el servidor que se cayó",
    ],
    "api_call": [
        "haz una petición a mi Home Assistant para encender la luz",
        "call my Gitea integration and list repos",
        "consulta mi Miniflux a ver qué hay nuevo",
    ],
    "manage_webhooks": [
        "añade un webhook nuevo",
        "list my webhooks",
        "desactiva ese webhook",
    ],
    "manage_tokens": [
        "créame un token de API nuevo",
        "list my API tokens",
        "borra ese token que ya no uso",
    ],
    "manage_settings": [
        "cambia mi voz de texto a voz",
        "set the search engine to something else",
        "usa este modelo como el modelo por defecto",
        "turn on incognito mode",
        "sube la velocidad de la voz",
        "change my reminder channel to email",
    ],
    "app_api": [
        "haz lo mismo que ese botón de la interfaz hace",
        "call the underlying endpoint the UI uses for this",
        "necesito algo que la interfaz puede hacer pero no hay tool concreta",
    ],

    # ── Project context / meta tools ─────────────────────────────────────
    "project_context": [
        "qué archivos tiene vinculados este proyecto",
        "list the folders attached to this project",
        "busca en los ficheros de contexto del proyecto",
    ],
    "manage_project_context": [
        "añade este documento al contexto del proyecto",
        "link this folder to the project so it's always available",
        "quita ese archivo viejo del contexto del proyecto",
        "fija esta versión como la definitiva de los requisitos",
    ],
    "project_objectives": [
        "qué objetivos tiene este proyecto",
        "mark this objective as done",
        "añade un nuevo objetivo al proyecto",
        "update the project goals dashboard",
    ],
    "manage_teach_mode": [
        "enséñame a hacer esto una vez y que lo repitas tú luego",
        "start recording this procedure so you can repeat it",
        "compila lo que acabo de hacer en un procedimiento",
    ],
    "capability_health": [
        "revisa si esta skill sigue funcionando bien",
        "check the health of my configured tools",
        "informa de un fallo real en este workflow",
    ],
    "branch_futures": [
        "prueba dos enfoques distintos en paralelo y compáralos",
        "create a few alternative branches from this point and see which works best",
        "compara estas dos opciones antes de decidir",
    ],
    "expert_review": [
        "revisa este capítulo con tu experto en estilo",
        "review my chapter against the style corpus",
        "comprueba si hay errores de continuidad en la historia",
        "check if this scene contradicts something earlier in the book",
    ],
    "verify_claim": [
        "comprueba que esta cita es real antes de repetirla",
        "fact-check this statistic against the source",
        "verifica que este dato no me lo estoy inventando",
    ],
    "memory_rules": [
        "guarda esta lección para la próxima vez",
        "search for what you've learned about this before",
        "esta regla que aplicaste fue útil, anótalo",
    ],

    # ── UI / plan control ─────────────────────────────────────────────────
    "ask_user": [
        "pregúntame cuál prefiero antes de seguir",
        "check with me before picking an approach",
        "dame opciones para elegir en vez de asumir",
    ],
    "update_plan": [
        "marca este paso del plan como hecho",
        "update the plan, that step is done now",
        "cambia el plan, ya no hace falta ese paso",
    ],

    # ── Desktop control ───────────────────────────────────────────────────
    "desktop_screenshot": [
        "haz una captura de mi pantalla",
        "take a screenshot of my screen",
        "qué hay en pantalla ahora mismo",
        "look at my desktop and tell me what you see",
    ],
    "desktop_list_windows": [
        "qué ventanas tengo abiertas",
        "what windows are open right now",
        "cuál es la aplicación activa",
    ],
    "desktop_focus_window": [
        "pon delante la ventana del navegador",
        "switch to the Notepad window",
        "activa la ventana de Excel",
    ],
    "desktop_click": [
        "haz clic en el botón de guardar",
        "click the OK button",
        "pulsa el botón de aceptar en pantalla",
        "double-click that file icon",
    ],
    "desktop_type": [
        "escribe mi nombre en ese campo",
        "type hello in the search box",
        "teclea la contraseña en el formulario",
    ],
    "desktop_key": [
        "guarda con ctrl+s",
        "press Enter",
        "cierra la ventana con alt+f4",
    ],
    "desktop_scroll": [
        "baja un poco en la página",
        "scroll down on the screen",
        "desplázate hacia arriba",
    ],
    "manage_desktop_control": [
        "limita el control del escritorio a solo esta aplicación",
        "keep an audit log of everything you do on my desktop",
        "restringe qué ventanas puedes tocar",
    ],
    # ── Semantic desktop control (ADP-08/09) ────────────────────────────────
    "desktop_snapshot": [
        "qué controles tiene esta ventana",
        "read the controls in this app window",
        "dime qué botones y campos hay en pantalla",
        "list the buttons and fields in the active window",
    ],
    "desktop_find": [
        "busca el botón de guardar en esta ventana",
        "find the OK button by name, not by pixel",
        "encuentra el campo de usuario en el formulario",
    ],
    "desktop_act": [
        "pulsa el botón de guardar por su nombre",
        "click the Save button, the real one, not a guessed pixel",
        "rellena el campo de contraseña con este valor",
        "select this option from the dropdown by name",
    ],
    "ui_control": [
        "abre el panel de documentos",
        "turn off the shell tool",
        "cambia al modo agente",
        "pon el tema oscuro",
        "abre un borrador de respuesta para este correo",
        "switch the model to the fast one",
    ],

    # ── Email / contacts ──────────────────────────────────────────────────
    "list_email_accounts": [
        "qué cuentas de correo tengo configuradas",
        "list my connected mailboxes",
        "compara mi bandeja de Gmail y la del trabajo",
    ],
    "list_emails": [
        "qué correos tengo sin leer",
        "show me my latest emails",
        "dime cuál fue el último correo que me llegó",
        "check my inbox",
    ],
    "read_email": [
        "lee ese correo de Marta",
        "open the email from the bank",
        "qué dice el último correo que recibí",
    ],
    "scan_email_unsubscribes": [
        "revisa mi correo a ver de qué newsletters me puedo dar de baja",
        "scan my inbox for spam I could unsubscribe from",
    ],
    "unsubscribe_email": [
        "date de baja de ese boletín",
        "unsubscribe me from that newsletter",
        "quita esa lista de correo, ya la aprobé",
    ],
    "send_email": [
        "mándale un correo a Marta con el resumen",
        "send an email to john@example.com about the meeting",
        "escríbele a mi jefe diciéndole que llegaré tarde",
        "compose a new email to the whole team",
    ],
    "reply_to_email": [
        "responde ya a ese correo diciendo que sí",
        "send the reply to that email now",
        "contesta ese mensaje confirmando la cita",
    ],
    "archive_email": [
        "archiva ese correo",
        "move this email out of my inbox",
        "quita ese mensaje de la bandeja pero no lo borres",
    ],
    "delete_email": [
        "borra ese correo",
        "delete that email permanently",
        "elimina ese mensaje de spam",
    ],
    "mark_email_read": [
        "marca ese correo como leído",
        "mark that email as unread",
        "señala este mensaje como ya visto",
    ],
    "bulk_email": [
        "archiva todos esos correos de una vez",
        "mark all my unread emails as read",
        "borra todos los correos de spam a la vez",
    ],
    "resolve_contact": [
        "cuál es el correo de Marta",
        "find John's email address for me",
        "envíale un mensaje a Pedro sin que yo tenga su email a mano",
    ],
    "manage_contact": [
        "guarda este número de teléfono para Marta",
        "add this address to my contacts",
        "actualiza el email de este contacto",
        "borra este contacto",
        "save this person's details for later",
    ],

    # ── Notes / calendar ──────────────────────────────────────────────────
    "manage_notes": [
        "apunta que hay que revisar el login",
        "add milk to my shopping list",
        "recuérdame llamar al dentista mañana a las 9",
        "toma nota de esto",
        "remind me on Friday about the report",
        "crea una nota con esto",
    ],
    "manage_calendar": [
        "qué tengo mañana",
        "add lunch with Sam to my calendar",
        "pon una reunión el viernes a las 10",
        "do I have anything scheduled this week",
        "cancela esa cita del martes",
    ],

    # ── Cookbook / model serving ──────────────────────────────────────────
    "download_model": [
        "descárgate el modelo Qwen 8B",
        "download this model from HuggingFace",
        "bájate este modelo al servidor",
    ],
    "serve_model": [
        "levanta el modelo en el puerto 8003",
        "start serving this model with vLLM",
        "pon a funcionar este modelo ya descargado",
    ],
    "list_served_models": [
        "qué modelos están corriendo ahora",
        "what's running right now",
        "muéstrame mi cookbook",
    ],
    "stop_served_model": [
        "para el modelo que está corriendo",
        "kill that running model",
        "apaga el servidor de vLLM",
    ],
    "tail_serve_output": [
        "por qué falló ese modelo al arrancar",
        "show me the error log for that failed serve",
        "mira el traceback del despliegue que se cayó",
    ],
    "list_downloads": [
        "qué se está descargando ahora mismo",
        "show my in-progress downloads",
        "cuánto le queda a la descarga del modelo",
    ],
    "cancel_download": [
        "cancela esa descarga",
        "stop downloading that model",
        "para la descarga que está en curso",
    ],
    "search_hf_models": [
        "busca un modelo de 8B en HuggingFace",
        "find a model for image generation on HuggingFace",
        "qué modelos hay para traducción",
    ],
    "list_cached_models": [
        "qué modelos tengo ya descargados",
        "do I already have this model on disk",
        "muéstrame los modelos en caché",
    ],
    "list_serve_presets": [
        "qué presets de arranque tengo guardados",
        "list my saved serve presets",
    ],
    "serve_preset": [
        "arranca el preset de stable diffusion",
        "launch the vllm-qwen preset",
    ],
    "adopt_served_model": [
        "ya arranqué un modelo a mano, hazlo visible en el cookbook",
        "register this manually-started server so I can stop it from the UI",
    ],
    "list_cookbook_servers": [
        "qué servidores tengo configurados para modelos",
        "which GPU box should I use for this",
        "list my cookbook servers",
    ],

    # ── Misc utility tools ────────────────────────────────────────────────
    "manage_bg_jobs": [
        "ha terminado ya el trabajo en segundo plano",
        "check on that background job",
        "muéstrame la salida de ese proceso en segundo plano",
        "kill that background task",
    ],
    "rename_symbol": [
        "renombra esta función en todo el proyecto",
        "rename this variable everywhere it's used",
        "cambia el nombre de esta clase en todo el código",
    ],
    "install_dependencies": [
        "instala este paquete de npm",
        "add the requests package to this project",
        "necesito instalar una librería nueva",
    ],
    "manage_scripts": [
        "guarda este comando para volver a usarlo luego",
        "save this as a reusable script",
        "vuelve a ejecutar ese script que guardamos antes",
    ],
    "capture_evidence": [
        "usa esa captura como referencia exacta del botón",
        "compare this screenshot with the previous one",
        "sigue siendo válida esta captura para lo que ves ahora",
    ],
    "browser_extract": [
        "sigue paginando esos resultados hasta el límite",
        "check if this page needs a login before scraping it",
        "verifica que el archivo se descargó bien",
    ],
    "manage_spreadsheet": [
        "importa este CSV",
        "read this Excel sheet and show me what's in it",
        "escribe estos datos en una hoja de cálculo",
        "exporta esto a CSV",
    ],

    # ── Git ───────────────────────────────────────────────────────────────
    "git_status": [
        "en qué estado está el repo",
        "what's changed since my last commit",
        "hay algo sin subir todavía",
    ],
    "git_log": [
        "enséñame los últimos commits",
        "show recent commit history",
        "qué se ha commiteado esta semana",
    ],
    "git_diff": [
        "qué he cambiado exactamente",
        "show me the diff for this commit",
        "compara lo que hay staged con lo que ya está commiteado",
    ],
    "git_branch": [
        "crea una rama nueva llamada feature-x",
        "make a new branch for this fix",
        "necesito una rama para probar esto sin tocar main",
    ],
    "git_checkout": [
        "cámbiate a la rama principal",
        "switch to the develop branch",
        "vuelve a main",
    ],
    "git_commit": [
        "sube los cambios",
        "commit this with a message about the bugfix",
        "commitea estos archivos",
    ],
    "git_merge": [
        "fusiona esta rama con main",
        "merge the feature branch into develop",
        "mergea esto ya",
    ],
    "git_delete_branch": [
        "borra esa rama que ya no se usa",
        "delete the old feature branch",
    ],
    "git_push": [
        "sube los cambios al remoto",
        "push this to origin",
        "sincroniza esto con GitHub",
    ],
    "git_pull": [
        "trae los últimos cambios del repo",
        "pull the latest changes",
        "actualiza esto con lo que hay en remoto",
    ],
    "git_fetch": [
        "mira si hay commits nuevos en origin",
        "fetch from the remote",
        "comprueba si hay algo nuevo sin traerlo todavía",
    ],

    # ── Research ──────────────────────────────────────────────────────────
    "trigger_research": [
        "investiga a fondo las opciones de hosting GPU",
        "do deep research on renewable energy trends",
        "haz una investigación completa sobre este tema",
        "look into this topic properly, not just a quick search",
    ],
    "manage_research": [
        "abre esa investigación que hice el mes pasado",
        "list my saved research reports",
        "borra ese informe de investigación antiguo",
    ],

    # ── Project board (lote 92, may not exist yet — see BOARD_TOOL_NAMES) ─
    "board_list": [
        "qué hay pendiente en este proyecto",
        "what's open on this project's board",
        "muéstrame los bugs abiertos",
        "list the ideas we haven't started yet",
    ],
    "board_ready": [
        "qué puedo empezar a hacer ya en este proyecto",
        "what's ready to work on right now",
        "dame lo próximo que no está bloqueado",
    ],
    "board_get": [
        "enséñame los detalles del FAU-12",
        "open that issue and show me the full description",
        "qué dice exactamente esa tarea del tablero",
    ],
    "board_create": [
        "apunta que hay que revisar el login",
        "log a bug about the broken checkout button",
        "esto es una idea, apúntala en el tablero del proyecto",
        "file a task for updating the docs",
    ],
    "board_update": [
        "marca esa tarea como terminada",
        "move that issue to in progress",
        "cambia la prioridad de ese bug a urgente",
    ],
    "board_comment": [
        "añade una nota a ese issue explicando por qué se bloqueó",
        "comment on that ticket with what I just found",
    ],
    "board_link": [
        "esta tarea está bloqueada por la otra",
        "link this issue as a duplicate of that one",
        "marca que este bug está relacionado con ese otro",
    ],
    "board_claim": [
        "me pongo yo con ese bug",
        "assign that task to me",
        "claim that issue, I'll handle it",
    ],

    # ── Versioned requirements (ADP-18/19/20) ──────────────────────────────
    "req_list": [
        "qué requisitos tenemos para este proyecto",
        "what are the requirements we've agreed on",
        "muéstrame los requisitos pendientes de aceptar",
        "list the accepted requirements",
    ],
    "req_get": [
        "enséñame el REQ-3 completo",
        "what exactly does REQ-2 say",
        "qué criterios de aceptación tiene ese requisito",
    ],
    "req_matrix": [
        "está implementado el REQ-4",
        "is REQ-1 actually tested and verified",
        "qué requisitos están sin implementar todavía",
        "show me the coverage for this project's requirements",
    ],
    "req_propose": [
        "apunta esto como requisito: el login debe expirar a los 30 minutos",
        "this should be a requirement, propose it",
        "añade un requisito nuevo con estos criterios de aceptación",
    ],
    "req_link": [
        "vincula el REQ-3 con este fichero",
        "link this test as evidence for REQ-5",
        "marca que este commit implementa ese requisito",
    ],

    # ── Isolated, comparable alternatives (CMP-13, W2-G) ───────────────────
    "alt_start": [
        "prueba esto de dos formas distintas y compáralas",
        "try a quick fix and a proper refactor in parallel, without losing either",
        "quiero comparar dos enfoques para este cambio",
    ],
    "alt_compare": [
        "en qué se diferencian esos dos intentos",
        "which alternative changed fewer files",
        "compara las alternativas de ese experimento",
    ],
    "alt_apply": [
        "quédate con la segunda alternativa",
        "apply the first attempt into my working copy",
        "aplica esa alternativa, la otra descártala",
    ],
}
