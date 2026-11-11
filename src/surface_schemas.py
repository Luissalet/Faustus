"""Function-tool schemas of the work surfaces: Pizarra (pizarra_*) and Hoja (sheet_*). Kept out of
``tool_schemas.py`` and appended to ``FUNCTION_TOOL_SCHEMAS`` there. Descriptions stay under 250 tokens."""
from __future__ import annotations

_OPS_BOARD = (
    "ops: list of {op: ...}. add {kind: sticky|text|rect|ellipse|diamond|frame|link, text, color (red orange yellow green "
    "blue purple pink gray or #hex), id?, x?, y?, w?, h?, near?: id} (without x,y it is placed in free space); update {id|ids, text, color, "
    "kind, w, h}; move {id|ids, x,y or dx,dy}; delete {id|ids}; connect {from, to, label?, arrow?: end|both|none}; "
    "group {ids, title} (a frame around them); layout {ids|frame, layout: grid|tree|flow|timeline|mindmap, cols?, root?}; "
    "align {ids, how: left|center|right|top|middle|bottom|distribute-h|distribute-v}; order {ids, to: front|back}; "
    "outline {items: [{text, kind?, color?, label?, children?: [...]}], layout, frame_title?, near?} builds nested items with "
    "connectors and lays them out in one op."
)

SURFACE_TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "pizarra_create",
        "description": ("Create a whiteboard (Pizarra, JSON Canvas) in this chat and open it beside the conversation; the person edits it "
                        "by hand while you edit the same board. Pass items (nested outline) and layout to get a tidy board in one call: "
                        "'plan the launch as a board' -> items=[{text:'Launch', children:[...]}], layout='mindmap'. Or pass ops "
                        "(see pizarra_edit). Pizarra: pizarra, tablero, lluvia de ideas, mapa mental, diagrama."),
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string"},
            "items": {"type": "array", "description": "Outline: [{text, children?, kind?, color?, label?}]", "items": {"type": "object"}},
            "layout": {"type": "string", "enum": ["grid", "tree", "flow", "timeline", "mindmap"]},
            "kind": {"type": "string", "description": "Default kind of the items (sticky, rect, ellipse, diamond, text)"},
            "frame_title": {"type": "string"},
            "ops": {"type": "array", "items": {"type": "object"}, "description": "Instead of items: the same ops as pizarra_edit"}}}}},
    {"type": "function", "function": {
        "name": "pizarra_read",
        "description": ("Read a whiteboard as a compact outline: every item with id, kind, colour, position, size and text; frames with "
                        "what they hold; connectors; plus what the person has selected and the part of the board they are looking at. "
                        "Without id it reads the open board. region: a frame id or {x,y,w,h} to read only that part. "
                        "Leer la pizarra abierta."),
        "parameters": {"type": "object", "properties": {
            "id": {"type": "string"}, "region": {"description": "Frame id or {x,y,w,h}"}}}}},
    {"type": "function", "function": {
        "name": "pizarra_edit",
        "description": ("Change a whiteboard with a batch of ops; the person sees items appear and your named cursor move. You never "
                        "compute pixel positions: leave x,y out, use near, layout, outline or group. Ids you give to new items can be used "
                        "by later ops in the batch. Without id it edits the open board. Pizarra: pizarra, tablero."),
        "parameters": {"type": "object", "properties": {
            "id": {"type": "string"}, "ops": {"type": "array", "items": {"type": "object"}, "description": _OPS_BOARD}},
            "required": ["ops"]}}},
    {"type": "function", "function": {
        "name": "pizarra_arrange",
        "description": ("Tidy items of a whiteboard as grid, tree (top-down by connectors), flow (left-to-right by connectors), timeline "
                        "or mindmap. scope: all (default), selected (what the person selected), a frame id, or a list of ids. "
                        "Ordenar la pizarra."),
        "parameters": {"type": "object", "properties": {
            "id": {"type": "string"}, "scope": {"description": "all | selected | frame id | [ids]"},
            "layout": {"type": "string", "enum": ["grid", "tree", "flow", "timeline", "mindmap"]},
            "cols": {"type": "integer"}, "root": {"type": "string", "description": "Root item for tree / mindmap"}},
            "required": ["layout"]}}},
]

#: Descriptions for the hybrid tool index (src/tool_index.py) and the natural-language examples that find the
#: tools (src/tool_index_examples.py).
INDEX_DESCRIPTIONS = {
    "pizarra_create": "Create a whiteboard (Pizarra, a JSON Canvas file) in this chat and open it beside the conversation. Give items (a nested outline) and a layout (tree, flow, timeline, mindmap, grid) and it comes back as a tidy board with connectors, in one call. The person edits the same board by hand: sticky notes, shapes, frames, connectors, pen, images. Use for 'monta el plan del lanzamiento en una pizarra', 'hazme un mapa mental de...', 'lluvia de ideas en un tablero', 'draw the architecture as a diagram', 'plan this as a board'.",
    "pizarra_read": "Read the open whiteboard as a compact outline: items with id, kind, colour, position and text, frames with their contents, connectors, what the person has selected and where they are looking. Use before editing and after the person changed the board: 'mira la pizarra', 'qué he seleccionado', 'what is on the board', 'resume lo que hay en el marco Marketing'.",
    "pizarra_edit": "Edit the whiteboard with a batch of ops (add sticky notes, shapes, text, frames; update, move, delete; connect with labelled arrows; group into a frame; layout as grid, tree, flow, timeline or mindmap; align; z-order; outline). Positions are computed for you. The person sees the items appear and the agent's named cursor move. Use for 'añade una nota amarilla con...', 'conecta A con B', 'agrupa esto en un marco', 'pon en rojo lo urgente', 'add a decision diamond after step 3'.",
    "pizarra_arrange": "Tidy a set of whiteboard items (all, the person's selection, a frame or a list) as grid, tree, flow, timeline or mindmap. Use for 'ordena la pizarra', 'organiza esto como un árbol', 'line these notes up in a timeline', 'arrange the selection in a grid'.",
}
INDEX_EXAMPLES = {
    "pizarra_create": ["monta el plan del lanzamiento en una pizarra", "hazme un mapa mental de las ideas para el TFM", "draw the system architecture on a whiteboard"],
    "pizarra_read": ["mira lo que hay en la pizarra", "qué notas he seleccionado en el tablero", "summarise the board"],
    "pizarra_edit": ["añade una nota amarilla con 'revisar precios' y conéctala con la de presupuesto", "pon en rojo todo lo urgente de la pizarra", "add a diamond for the go/no-go decision after QA"],
    "pizarra_arrange": ["ordena las notas de la pizarra en una línea de tiempo", "organiza lo seleccionado como un árbol", "lay the board out as a flow"],
}
