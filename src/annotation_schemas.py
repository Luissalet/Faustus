"""Function-tool schemas of annotations and guided review (src/annotations.py). Appended to
``FUNCTION_TOOL_SCHEMAS`` in ``tool_schemas.py``; the MCP server (mcp_servers/annotations_server.py) reads them too."""
from __future__ import annotations

ANNOTATION_TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "annotate_open",
        "description": "Open the Annotate tab beside the chat on something for the person to mark up: your plan (kind plan, ref a message id or 'last'), a Markdown/HTML file of the workspace (kind file, ref the path), a Studio document (kind doc, ref its id) or a local web page served on this machine, live with hot reload (kind web, ref http://localhost:PORT/...). The person selects text or an element and leaves comments, suggested changes, deletions and questions, then sends them back as their next message. Use after proposing a plan or writing a page: 'anota el plan', 'déjame comentar el README', 'quiero marcar cosas en la web de localhost:5173'. Abrir para anotar.",
        "parameters": {"type": "object", "properties": {
            "kind": {"type": "string", "enum": ["plan", "file", "doc", "web"], "description": "What to annotate"},
            "ref": {"type": "string", "description": "plan: message id or 'last'; file: path (relative to the workspace or absolute); doc: document id; web: http://localhost:PORT/path"},
            "title": {"type": "string", "description": "Optional title shown in the tab"}}, "required": ["kind"]}}},
    {"type": "function", "function": {
        "name": "annotations_list",
        "description": "The annotations the person left in this chat (on the plan, files, documents, local pages and review diffs), each quoting what it is about: comment, suggested change (with the replacement), delete or question. pending_only=true lists only the ones not sent yet. Use when the person says 'mira mis anotaciones', 'he dejado comentarios en el plan', 'what did I mark'. Leer anotaciones.",
        "parameters": {"type": "object", "properties": {
            "pending_only": {"type": "boolean", "description": "Only the annotations not sent to you yet"}}}}},
    {"type": "function", "function": {
        "name": "review_open",
        "description": "Open a code review beside the chat: the workspace's uncommitted changes (base HEAD, default), or its diff against a branch or ref (base main, origin/main, HEAD~3, a SHA). The person reads the diff file by file and comments on single lines; those notes come back to you. Returns the changed files with +/- counts. Use for 'revisa los cambios', 'abre la revisión del diff contra main', 'review my uncommitted work'. Abrir revisión de código.",
        "parameters": {"type": "object", "properties": {
            "base": {"type": "string", "description": "Ref to diff the working tree against (default HEAD = uncommitted changes)"},
            "workspace": {"type": "string", "description": "Absolute repository folder (default: this chat's workspace)"}}}}},
    {"type": "function", "function": {
        "name": "review_guide",
        "description": "Order the open review's hunks into reading chapters with a short summary each: core (the logic that motivates the change), effects (what behaves differently because of it) and glue (wiring, registration, config, docs). The Annotate tab shows each chapter next to its real diff. Call after review_open. Use for 'guíame por el diff', 'ordena los cambios para revisarlos', 'walk me through this change'. Guiar la revisión.",
        "parameters": {"type": "object", "properties": {
            "model": {"type": "string", "description": "Optional model name; default the configured utility model"}}}}},
]

#: Descriptions for the hybrid tool index (src/tool_index.py) and the examples that find the tools
#: (src/tool_index_examples.py).
INDEX_DESCRIPTIONS = {
    "annotate_open": "Open the Annotate tab beside the chat on the agent's plan, a Markdown or HTML file, a Studio document or a local web page (proxied live from localhost with hot reload) so the person can select parts and leave comments, suggested changes, deletions and questions that come back to the agent as one message. Use for 'déjame anotar el plan', 'quiero comentar este documento', 'abre localhost:5173 para marcar cosas', 'annotate the README', 'let me mark up the page'.",
    "annotations_list": "Read the annotations the person left in this chat on plans, files, documents, local pages and review diffs, quoting what each refers to (comment, suggested change, delete, question), pending ones marked. Use for 'mira mis anotaciones', 'qué he comentado en el plan', 'what did I mark up', 'read my review comments'.",
    "review_open": "Open a code review beside the chat with the workspace's uncommitted changes or its diff against a branch or ref, file by file, so the person comments on single lines and the notes come back to the agent. Use for 'revisa mis cambios sin commitear', 'abre la revisión contra main', 'review the diff of this branch', 'quiero comentar el diff'.",
    "review_guide": "Order the open code review into reading chapters (core, effects, glue) with a short summary each, next to the real diff, using the chat's model. Use for 'guíame por los cambios', 'ordena el diff para revisarlo', 'walk me through this change', 'explícame el diff por partes'.",
}
INDEX_EXAMPLES = {
    "annotate_open": ["déjame anotar el plan que has propuesto", "abre la web de localhost:5173 para que la comente", "quiero dejar comentarios en el README"],
    "annotations_list": ["mira las anotaciones que he dejado en el plan", "qué comentarios tengo pendientes", "read my annotations on the page"],
    "review_open": ["revisa mis cambios sin commitear", "abre una revisión del diff contra main", "quiero comentar el diff línea a línea"],
    "review_guide": ["guíame por los cambios en orden", "ordena el diff en capítulos para revisarlo", "walk me through this change"],
}
