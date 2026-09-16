# Local agent close-the-loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Impedir que un agente local cuelgue el turno en un servidor Windows, se quede sin modelo en VRAM, marque Verified un bug visual sin browser, o reinyecte un spec de 170 k en el chat siguiente.

**Architecture:** Cinco capas independientes en el harness actual: guard de servidores Windows-correcto, pin de `keep_alive` por run, idle corto para lanzamientos de servidor, contrato UI en el `TurnLedger`, continuidad de todos/adjuntos a nivel proyecto. Cada capa se entrega con tests verdes y se puede mergear sola. Mid-turn overflow (`src/context_overflow.py`, FAUSTUS §86) ya está en master: Task 7b reutiliza esos stubs; no reimplementar spill.

**Tech Stack:** Python 3.13, pytest, Ollama `/api/chat` + `/api/generate`, Git Bash en Windows, Playwright MCP ya cableado como `mcp__builtin_browser__*`.

**Spec:** `docs/spec/superpowers/specs/2026-09-16-local-agent-close-the-loop-design.md`

## Global Constraints

- Rama `master` en `D:\LocalAI\odysseus`. No tocar el `.git` de Silhouettes.
- Windows-first: los tests de detached POSIX se parametrizan; no se asume Linux.
- No autowitch de modelo. No convertir silenciosamente un Flask en `#!bg`.
- Settings nuevos con default que cambia el comportamiento **solo** en los casos que hoy fallan (Windows+nohup, turnos UI, runs locales).
- Suite: pytest de los ficheros tocados en cada task; no la suite de 9 k salvo al final.
- Commits solo si Luis lo pide en el momento de ejecutar. Los pasos «Commit» de este plan se saltan hasta entonces.
- Actualizar `FAUSTUS.md` §90 al cerrar, con un repro medido, no con intención.

## File map

| File | Why |
|---|---|
| `src/agent_tools/subprocess_tools.py` | regex, detached por OS, idle de servidor |
| `tests/test_subprocess_hardening.py` | contrato actual invertido en Windows para nohup |
| `src/run_model_pin.py` | nuevo: pin/unpin |
| `src/llm_core.py` | mezclar keep_alive del pin |
| `src/agent_loop.py` | ciclo de vida del pin; browser tools; adjuntos; inject todos |
| `src/agent_harness.py` | UI verify + policy text |
| `src/agent_tools/coding_tools.py` | todos de proyecto |
| `src/settings.py`, `src/agent_settings_schema.py` | knobs |
| `src/project_tests.py` | campo `ui_verify` informativo |
| `tests/test_run_model_pin.py` | nuevo |
| `tests/test_agent_harness.py` | UI verify |
| `tests/test_coding_tools.py` o el test de todos que ya exista | project todos |
| `FAUSTUS.md` | §90 |

---

### Task 1: Guard — `python app.py` es un servidor

**Files:**
- Modify: `src/agent_tools/subprocess_tools.py` (`_SERVER_LAUNCH_RE`, `foreground_server_launch`)
- Test: `tests/test_subprocess_hardening.py`

**Interfaces:**
- Consumes: `foreground_server_launch(command: str) -> Optional[str]`
- Produces: la misma firma; ahora también hace match de `python app.py`, `python -m flask`, `.venv/Scripts/python.exe app.py`

- [ ] **Step 1: Write the failing assertions** into `test_foreground_server_launch_detection`. Añadir a `blocked`:

```python
        "python app.py",
        "python app.py > server.log 2>&1",
        'cd "C:\\\\Users\\\\luism\\\\Desktop\\\\Proyectos independientes\\\\Silhouettes" && python app.py',
        "python -m flask run",
        r".venv\Scripts\python.exe app.py",
```

Dejar `python server.py --check` y `python -c "import server"` en `allowed`.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_subprocess_hardening.py::test_foreground_server_launch_detection -v`

Expected: FAIL en `python app.py` (`assert None` vs truthy).

- [ ] **Step 3: Extend `_SERVER_LAUNCH_RE`**

En `subprocess_tools.py`, ampliar el grupo de launchers. Mantener `flask\s+run`. Añadir alternativa para script Flask:

```python
r"(?:^|[;&|(]\s*)(?:[\w./\\:-]*python[\w.]*\s+(?:-m\s+)?)?(?:"
r"uvicorn|gunicorn|hypercorn|daphne|waitress-serve|flask\s+run|streamlit\s+run|"
# ... existing ...
r")|"
r"(?:^|[;&|(]\s*)[\w./\\:-]*pythonw?(?:\.exe)?\s+(?:-m\s+flask\b|[\w./\\-]*app\.py)\b"
```

Refinar para no casar `python -m pytest`. Un segundo regex `_PYTHON_APP_RE` dedicado es más claro que un único regex gigante. `foreground_server_launch` hace OR de los dos, luego aplica `_BACKGROUNDED_RE` (todavía; Task 2 lo cambia).

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_subprocess_hardening.py::test_foreground_server_launch_detection tests/test_subprocess_hardening.py::test_bash_tool_refuses_foreground_server_before_running -v`

Expected: PASS. `python app.py` ahora devuelve exit 2 con `#!bg` en el error.

- [ ] **Step 5: Commit** (solo si Luis lo pide)

```bash
git add src/agent_tools/subprocess_tools.py tests/test_subprocess_hardening.py
git commit -m "fix(harness): treat python app.py as a blocking server launch"
```

---

### Task 2: En Windows, `nohup` y `&` no despegan

**Files:**
- Modify: `src/agent_tools/subprocess_tools.py` (`foreground_server_launch`, `_BACKGROUNDED_RE`)
- Modify: `src/agent_harness.py` (`local_model_policy`)
- Test: `tests/test_subprocess_hardening.py`

**Interfaces:**
- Consumes: Task 1
- Produces: `foreground_server_launch(command: str, *, windows: Optional[bool] = None) -> Optional[str]`
  - `windows=None` → `IS_WINDOWS`
  - POSIX: `nohup`/`&` siguen eximiendo (tests actuales de `allowed` con `windows=False`)
  - Windows: esas señales **no** eximen; sí eximen `timeout`, `gtimeout`, `Start-Process`

- [ ] **Step 1: Write failing tests**

```python
def test_windows_nohup_does_not_count_as_detached():
    cmd = "nohup python -m uvicorn app:app > s.log 2>&1 &"
    assert st.foreground_server_launch(cmd, windows=True)
    assert st.foreground_server_launch(cmd, windows=False) is None

def test_windows_ampersand_python_app_blocked():
    cmd = 'cd "/c/Users/x/Silhouettes" && nohup python app.py > server.log 2>&1 &\nsleep 3\ncurl -s http://127.0.0.1:5000/editor'
    assert st.foreground_server_launch(cmd, windows=True)
```

El primer caso **invierte** la fila actual de `allowed` que dice que nohup uvicorn está permitido. Esa fila debe quedar solo bajo `windows=False`.

- [ ] **Step 2: Run tests — expect FAIL**

Run: `pytest tests/test_subprocess_hardening.py::test_windows_nohup_does_not_count_as_detached -v`

Expected: FAIL porque hoy `_BACKGROUNDED_RE` casa `nohup` en cualquier OS.

- [ ] **Step 3: Implement OS-aware detach**

```python
_POSIX_DETACH_RE = re.compile(
    r"(?:&\s*$|&\s*\n|\bnohup\b|\bsetsid\b|\bdisown\b|\bscreen\s+-d|\btmux\s+new)",
    re.I | re.M,
)
_ANY_DETACH_RE = re.compile(
    r"(?:\bstart\s+/b\b|Start-Process|\btimeout\s+-?\d|\bgtimeout\s+\d)",
    re.I | re.M,
)

def _is_detached(command: str, windows: bool) -> bool:
    if _ANY_DETACH_RE.search(command):
        return True
    if windows:
        return False
    return bool(_POSIX_DETACH_RE.search(command))
```

`foreground_server_launch`: si hay launcher y not `_is_detached`, devolver el launcher. Actualizar `local_model_policy()` con una frase: «On Windows, `nohup` and a trailing `&` do not detach; use `#!bg` as the first line.»

- [ ] **Step 4: Run the whole hardening file**

Run: `pytest tests/test_subprocess_hardening.py -v`

Expected: PASS. En esta máquina Windows, `BashTool().execute("nohup python app.py")` → exit 2.

- [ ] **Step 5: Commit** (si Luis lo pide)

```bash
git add src/agent_tools/subprocess_tools.py src/agent_harness.py tests/test_subprocess_hardening.py
git commit -m "fix(harness): do not treat nohup or & as detached on Windows"
```

---

### Task 3: Idle corto para launchers de servidor

**Files:**
- Modify: `src/agent_tools/subprocess_tools.py` (`_on_host`, PowerShell path)
- Modify: `src/settings.py`, `src/agent_settings_schema.py`
- Test: `tests/test_subprocess_hardening.py`

**Interfaces:**
- Consumes: `looks_like_server_launch(command)` = match de launcher **ignorando** detach (función nueva, 5 líneas)
- Produces: setting `agent_server_idle_timeout_seconds` default `45`; `_on_host` pasa `idle_timeout=min(effective, 45)` cuando `looks_like_server_launch`

- [ ] **Step 1: Failing test** (no hace falta un sleep de 45 s). Unitario:

```python
def test_server_shaped_command_uses_short_idle(monkeypatch):
    monkeypatch.setattr(st, "_effective_idle_timeout", lambda key: 900.0)
    assert st._idle_for_command("python app.py") == 45.0
    assert st._idle_for_command("pytest -q") == 900.0
```

Añadir `_idle_for_command` como helper puro para no montar un subprocess.

- [ ] **Step 2: Run — expect FAIL** (`_idle_for_command` no existe).

- [ ] **Step 3: Implement**

```python
def looks_like_server_launch(command: str) -> bool:
    return bool(_SERVER_LAUNCH_RE.search(command or "") or _PYTHON_APP_RE.search(command or ""))

def _idle_for_command(command: str, key: str = "bash") -> float:
    base = _effective_idle_timeout(key)
    if not looks_like_server_launch(command):
        return base
    try:
        from src.settings import get_setting
        cap = float(get_setting("agent_server_idle_timeout_seconds", 45) or 45)
    except Exception:
        cap = 45.0
    if cap <= 0:
        return base
    return min(base, cap) if base else cap
```

En `_on_host`, sustituir `idle_s = _effective_idle_timeout("bash")` por `idle_s = _idle_for_command(content, "bash")`. Igual en python/powershell tools.

Settings: `"agent_server_idle_timeout_seconds": 45` y entrada en `agent_settings_schema.py` junto a `agent_subprocess_idle_timeout_seconds`.

- [ ] **Step 4: Run**

Run: `pytest tests/test_subprocess_hardening.py -v`

Expected: PASS.

---

### Task 4: Pin de `keep_alive` durante el run

**Files:**
- Create: `src/run_model_pin.py`
- Create: `tests/test_run_model_pin.py`
- Modify: `src/llm_core.py` (donde ya se asigna `payload["keep_alive"]`)
- Modify: `src/agent_loop.py` (start + `finally` del run local)
- Modify: `src/settings.py`, `src/agent_settings_schema.py`

**Interfaces:**
- Produces:

```python
def pin_for_run(run_id: str, endpoint: str, model: str) -> None: ...
def unpin_for_run(run_id: str) -> Optional[Dict[str, str]]: ...
def keep_alive_override(run_id: Optional[str] = None) -> Optional[str]: ...
def restore_keep_alive(endpoint: str, model: str, keep_alive: str) -> bool: ...
```

El override se aplica en `_apply_gen_overrides_ollama` **después** de `model_load_options`, de modo que el pin gana al valor guardado. Un override explícito en `overrides["keep_alive"]` del caller sigue ganando: el pin solo rellena si falta.

- [ ] **Step 1: Unit tests without network**

```python
from src import run_model_pin as pin

def test_pin_supplies_keep_alive_for_that_run(monkeypatch):
    monkeypatch.setattr("src.run_model_pin.get_setting", lambda k, d=None: "2h" if k == "agent_run_keep_alive" else d)
    pin.reset_for_tests()
    pin.pin_for_run("r1", "http://127.0.0.1:11434", "qwen3.8:27b")
    assert pin.keep_alive_override("r1") == "2h"
    assert pin.keep_alive_override("other") is None
    rec = pin.unpin_for_run("r1")
    assert rec["model"] == "qwen3.8:27b"
    assert pin.keep_alive_override("r1") is None

def test_llm_core_merges_run_pin(monkeypatch):
    # reuse the fake client pattern in tests/test_model_load_options.py
    ...
```

El segundo test clona el fake httpx de `tests/test_model_load_options.py` y comprueba `client.payload["keep_alive"] == "2h"` cuando hay pin, aunque `model_load_options` tenga `"5m"`.

- [ ] **Step 2: Run — expect FAIL** (módulo no existe).

Run: `pytest tests/test_run_model_pin.py -v`

- [ ] **Step 3: Implement `run_model_pin.py`**

Dict `_PINS: Dict[str, Dict[str, str]]` protegido por `threading.Lock`. `restore_keep_alive` hace `POST {endpoint}/api/generate` con `{"model", "keep_alive", "prompt": " ", "stream": false}` timeout 3 s; no lanza. `unpin_for_run` llama restore si `agent_run_keep_alive_restore` y hay endpoint.

En el agent loop, el `run_id` ya existe cuando empieza el lane local. `pin_for_run` justo antes del primer generate; `try/finally: unpin_for_run`. Pasar `run_id` a `llm_core` por el canal de overrides que ya usa el loop (si hoy no hay `run_id` en overrides, añadirlo al dict de gen overrides — no al JSON de Ollama).

- [ ] **Step 4: Run pin tests + `tests/test_model_load_options.py`**

Expected: PASS. El valor guardado `5m` sigue aplicándose cuando no hay run pin.

---

### Task 5: Contrato UI en el TurnLedger

**Files:**
- Modify: `src/agent_harness.py`
- Modify: `src/settings.py`, `src/agent_settings_schema.py`
- Test: `tests/test_agent_harness.py`

**Interfaces:**
- Produces:

```python
UI_PATH_RE  # compiled
UI_INTENT_RE
BROWSER_EVIDENCE_TOOLS  # frozenset of suffixes

class TurnLedger:
    def mutated_ui_paths(self) -> List[str]: ...
    def has_browser_evidence(self) -> bool: ...
    def needs_ui_verify(self, user_text: str) -> bool: ...
```

La ronda harness se engancha donde ya viven las de «claimed writes without tools» (`check_claims` / el bloque que inyecta el retry). Una sola ronda extra, mismo tope de 2 que el resto.

- [ ] **Step 1: Failing tests**

```python
def test_needs_ui_verify_on_editor_js(tmp_path):
    led = TurnLedger(workspace=str(tmp_path))
    led.record("edit_file", "static/editor/viewport2d.js", {"ok": True}, round_num=1)
    assert led.needs_ui_verify("arregla el canvas")
    assert not led.has_browser_evidence()

def test_browser_snapshot_satisfies_ui_verify(tmp_path):
    led = TurnLedger(workspace=str(tmp_path))
    led.record("edit_file", "static/editor/layer_tree.js", {"ok": True}, round_num=1)
    led.record("mcp__builtin_browser__browser_snapshot", "", {"ok": True}, round_num=2)
    assert led.has_browser_evidence()

def test_todowrite_verify_item_not_mutation_backed_without_browser(tmp_path):
    led = TurnLedger(workspace=str(tmp_path))
    led.record("edit_file", "static/editor/editor.css", {"ok": True}, round_num=1)
    out = led.record_progress([
        {"content": "Verify all fixes in browser", "status": "completed", "priority": "high"}
    ], 2)
    assert out[0]["verified"] is False
```

Ajustar nombres de `record()` a la firma real de `TurnLedger` (ver tests existentes en `test_agent_harness.py`).

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3: Implement.** `needs_ui_verify` respeta `agent_ui_verify` (default true). Paths: `.html`, `.css`, `.scss`, `.js`/`.mjs`/`.jsx`/`.tsx` bajo `static/` o `templates/`, o user text casa `browser|navegador|viewport|canvas|thumbnail|drag|visual`. Browser evidence: `ev["ok"]` y `"browser_snapshot" in tool or "browser_take_screenshot" in tool or "browser_evaluate" in tool or "browser_navigate" in tool`.

Cierre: si needs y not evidence, inyectar el mismo estilo de harness check que claims. Copy:

```
[Harness check] UI files changed this turn but no browser snapshot/evaluate ran.
Open the editor in the browser MCP (browser_navigate + browser_snapshot).
Do not start Flask/python app.py in the foreground; use #!bg if the server is down.
```

- [ ] **Step 4: Run `tests/test_agent_harness.py -v` plus el subset nuevo.** Expected PASS. No romper claims tests.

---

### Task 6: Ofrecer tools de browser cuando el turno es UI

**Files:**
- Modify: `src/agent_loop.py` (`_browser_intent_is_real` / `_expand_browser_mcp_tools` / donde se calcula `relevant_tools` cada ronda)
- Test: el fichero que ya cubre `_browser_intent_is_real` (buscar `test_browser_intent` / `test_expand_browser`)

**Interfaces:**
- Consumes: Task 5 `needs_ui_verify` / mutaciones UI del ledger del turno en curso
- Produces: a partir de la ronda *posterior* a la primera mutación UI, el set de tools incluye `_BROWSER_MCP_SESSION_TOOLS` aunque el índice no haya recuperado `builtin_browser`. Round 0 de un turno no-UI no cambia (protege el caso 12k→38k).

- [ ] **Step 1: Failing test** con un set que solo tiene `edit_file` y un ledger con `static/editor/x.js` mutado → `expand` debe añadir `mcp__builtin_browser__browser_navigate` y `browser_snapshot`.

- [ ] **Step 2: Run — FAIL.**

- [ ] **Step 3: Implement.** Pasar el ledger (o un bool `force_browser`) a `_expand_browser_mcp_tools`. No expandir las 28 tools de ratón por coordenadas; solo el set de sesión (`_BROWSER_MCP_SESSION_TOOLS`) más `browser_take_screenshot` y `browser_evaluate` si el MCP los declara.

- [ ] **Step 4: Run tests de browser intent + un test de no-regresión: «add a health() endpoint» no mete Playwright.**

---

### Task 7: Todos de proyecto + recorte de adjuntos

**Files:**
- Modify: `src/agent_tools/coding_tools.py`
- Modify: `src/agent_loop.py` (inyección al construir el system/user del primer round; recorte del cuerpo)
- Modify: `src/agent_harness.py` (`user_authored_text` ya corta para paths; extraer `attachment_budgeted_text(text, max_chars)`)
- Modify: `src/settings.py` (`agent_project_todos`, `agent_inline_attachment_max_chars`)
- Test: `tests/test_workspace_confine.py` (ya tiene `test_todowrite_persists_session_list`); nuevo `tests/test_project_todos.py`; tests de `user_authored_text` en `test_agent_harness.py`

**Interfaces:**
- Produces:

```python
def save_project_todos(project_id: str, todos: List[Dict[str, Any]]) -> None: ...
def load_project_todos(project_id: str) -> List[Dict[str, Any]]: ...
def incomplete_todos(todos) -> List[Dict[str, Any]]: ...  # pending | in_progress
def attachment_budgeted_text(text: str, max_chars: int = 4000) -> str: ...
```

`TodoWriteTool.execute` si `ctx["project_id"]`: escribe ambos sitios. Inyección: bloque de ≤ 800 tokens al final del system o como system extra, solo si `incomplete_todos` no vacío. Formato en la spec.

`attachment_budgeted_text`: reutiliza el corte `=== File:` / `=== ZIP archive:` de `user_authored_text`; del cuerpo cortado extrae headings `^#{1,3} ` hasta `max_chars`.

- [ ] **Step 1: Tests**

```python
def test_todowrite_also_writes_project_file(tmp_path, monkeypatch, admin):
    ...

def test_new_chat_prompt_includes_incomplete_project_todos():
    ...

def test_attachment_budget_keeps_headings_drops_body():
    blob = "Keep implementing.\n=== File: plan.md ===\n# Task 08\n" + ("x" * 8000) + "\n# Task 09\nmore"
    out = attachment_budgeted_text(blob, 400)
    assert "Task 08" in out and "Task 09" in out
    assert "x" * 1000 not in out
    assert "read the section you need" in out.lower() or "attachment" in out.lower()
```

- [ ] **Step 2: FAIL, then implement, then PASS** `pytest tests/test_project_todos.py tests/test_agent_harness.py -k attachment -v`

---

### Task 7b: Continue-turn inyecta working state, no el spec

**Files:**
- Modify: `src/agent_loop.py` (`_looks_like_continue_turn`, `_CONTINUE_TURN_RE`, round-0 injection near the existing todowrite-refresh block ~8038)
- Modify: `src/agent_tools/coding_tools.py` (persist `last_files` / last tools on the project file)
- Modify: `src/agent_harness.py` (`attachment_budgeted_text` already from Task 7 — apply it to the live last user message, not only ledger paths)
- Test: `tests/test_project_todos.py`, `tests/test_agent_loop_workspace_tool_floor.py` or a new `tests/test_continue_turn_context.py`

**Interfaces:**
- Consumes: Task 7 `load_project_todos` / `incomplete_todos` / `attachment_budgeted_text`
- Produces:

```python
# src/agent_loop.py — extend the existing short-phrase matcher
_IMPLEMENTATION_CONTINUE_RE = re.compile(
    r"keep\s+implementing|continue\s+the\s+implementation|"
    r"sigue(?:e)?(?:\s+el)?\s+plan|sigue\s+implementando",
    re.I,
)

def _looks_like_continue_turn(text: str) -> bool:
    # keep today's exact Continue-button match, AND the longer "keep implementing" family
    ...

# src/agent_tools/coding_tools.py
def save_project_working_set(project_id: str, *, last_files: list[str], last_tools: list[dict], last_error: str = "") -> None: ...
def load_project_working_set(project_id: str) -> dict: ...
# file shape: data/agent_todos/project-<id>.json
# {"todos": [...], "last_files": [...], "last_tools": [{"tool","ok","paths"}], "last_error": "", "updated_at": iso}

def continue_turn_block(todos, working_set) -> str:
    """Incomplete todos + last files + last 8 tools + last error. Empty string if nothing."""
```

Hoy `_looks_like_continue_turn` solo casa «Continue» / «continuar» cortos (`_CONTINUE_TURN_RE`) y, si los todos de *sesión* están complete, inyecta `TODOWRITE_REFRESH_NUDGE`. No casa «Keep implementing the plan» (el mensaje real de Silhouettes 16-09) y no inyecta el working set del *proyecto*.

- [ ] **Step 1: Failing tests**

```python
from src.agent_loop import _looks_like_continue_turn
from src.agent_tools import coding_tools as ct
from src.agent_harness import attachment_budgeted_text

def test_keep_implementing_is_a_continue_turn():
    assert _looks_like_continue_turn("Keep implementing the plan")
    assert _looks_like_continue_turn("Continue the implementation")
    assert _looks_like_continue_turn("sigue el plan")
    assert not _looks_like_continue_turn("what does keep implementing mean in this file?")

def test_continue_block_lists_incomplete_and_files(tmp_path, monkeypatch):
    monkeypatch.setattr(ct, "_TODO_DIR", str(tmp_path))
    ct.save_project_todos("p1", [
        {"content": "Diagnose giant layers", "status": "in_progress", "priority": "high"},
        {"content": "Verify all fixes in browser", "status": "pending", "priority": "high"},
        {"content": "Done already", "status": "completed", "priority": "low"},
    ])
    ct.save_project_working_set("p1", last_files=["static/editor/viewport2d.js"],
                                last_tools=[{"tool": "edit_file", "ok": True, "paths": ["static/editor/viewport2d.js"]}],
                                last_error="")
    from src.agent_loop import continue_turn_block
    block = continue_turn_block(ct.load_project_todos("p1"), ct.load_project_working_set("p1"))
    assert "Diagnose giant layers" in block
    assert "Verify all fixes in browser" in block
    assert "Done already" not in block
    assert "viewport2d.js" in block

def test_attachment_budget_applied_to_user_message():
    blob = "Keep implementing.\n=== File: plan.md ===\n# Task 08\n" + ("x" * 8000)
    out = attachment_budgeted_text(blob, 400)
    assert "Task 08" in out
    assert "x" * 1000 not in out
```

- [ ] **Step 2: Run — expect FAIL** on `_looks_like_continue_turn("Keep implementing the plan")` (hoy False).

Run: `pytest tests/test_continue_turn_context.py -v`

- [ ] **Step 3: Implement**

1. Extender `_looks_like_continue_turn` con `_IMPLEMENTATION_CONTINUE_RE.search` (no `match` anclado al mensaje entero). No tratar una pregunta *sobre* esas palabras como continue: si el texto tiene `?` y no empieza por el patrón, False.
2. `save_project_working_set` mergea en el mismo JSON de Task 7. Al cerrar el turno en `agent_loop` (junto a donde ya se persiste progress), si hay `project_id`: `last_files = _ledger.mutated_paths()[:12]`, `last_tools =` últimos 8 `self.events` con `tool/ok/paths/error`, `last_error` = último `e["error"]`.
3. Round 0, justo después del bloque todowrite-refresh (~8038): si `_looks_like_continue_turn(_last_user)` **o** es el primer turno de un chat nuevo del proyecto (`not any(m.get("role")=="assistant" for m in messages)`), y `incomplete_todos(load_project_todos(project_id))` no vacío, append un `role: system` con `continue_turn_block(...)`. Copy:

```
Incomplete work for this project (from the previous chat):
- [in_progress] …
- [pending] …
Last files touched: …
Last tools: edit_file ok static/editor/viewport2d.js; bash fail …
```

Si el mensaje es continue-turn, **después** aplicar `attachment_budgeted_text` al último `role=user` de `messages` (el spec de 170 k deja de ocupar el prompt; el disco no se toca). Reutilizar overflow: no hace falta rehidratar blobs; el working set nombra paths. Misma sesión con stubs ya en history: no compactar esos stubs otra vez.

4. El `TODOWRITE_REFRESH_NUDGE` de sesión complete se queda: corre solo cuando los todos de *sesión* están complete, como hoy.

- [ ] **Step 4: Run**

Run: `pytest tests/test_continue_turn_context.py tests/test_project_todos.py tests/test_workspace_confine.py::test_todowrite_persists_session_list -v`

Expected: PASS. Un user message con `=== File:` de 170 k queda ≤ `agent_inline_attachment_max_chars` + TOC.

---

### Task 8: `ui_verify` en la tarjeta + aviso de modelo largo

**Files:**
- Modify: `src/project_tests.py` (añadir clave, no cambiar `ok`)
- Modify: `src/agent_loop.py` (SSE `system_notice` una vez si modelo casa `qwen3.8` y `round_num >= 40`)
- Test: `tests/test_project_tests_scoping.py` o el test de resumen; un test del notice con round stub

**Interfaces:**
- `run_for_turn` sigue devolviendo el mismo `ok`. El dict de harness/metrics gana `ui_verify: "ok"|"missing"|"skipped"`.
- El notice no es un error del turno.

- [ ] **Step 1–4:** test del campo `missing` cuando ledger.needs_ui_verify y not evidence; `skipped` cuando `agent_ui_verify` es false; `ok` cuando hay snapshot. Notice: se emite como máximo una vez por run (`run_state` flag).

---

### Task 9: Medir en vivo y escribir FAUSTUS.md §90

**Files:**
- Modify: `FAUSTUS.md`

No es TDD. Es el cierre del spec «Success».

- [ ] **Step 1:** Con Faustus en marcha y qwen3.8 cargado, desde un chat de prueba (no Silhouettes de producción) mandar un bash `nohup python app.py > server.log 2>&1 &`. Esperado: error `#!bg` en < 2 s, `ollama ps` sigue listando el modelo.
- [ ] **Step 2:** Un turno que edite un `.css` de un workspace de prueba y cierre. Esperado: harness check pidiendo browser, no verified ciego.
- [ ] **Step 3:** Chat nuevo en un proyecto con todos incompletos. Esperado: el primer generate incluye esos todos.
- [ ] **Step 4:** Escribir §90 en `FAUSTUS.md` con los tres números (latencia del rechazo, `ollama ps`, si el pin restauró keep_alive). Sin §90 no está cerrado.

---

## Spec coverage

| Spec section | Task |
|---|---|
| Guard `python app.py` | 1 |
| Windows nohup/& | 2 |
| Idle corto servidor | 3 |
| keep_alive pin/restore | 4 |
| UI verify ledger + todowrite | 5 |
| Browser tools en schema | 6 |
| Project todos + attachment budget | 7 |
| Continue-turn working state (spec 5c) | 7b |
| Tarjeta ui_verify + notice modelo | 8 |
| Success medible + FAUSTUS.md | 9 |
| Non-goal autowitch | 8 (solo notice) |
| Non-goal auto-#!bg | ninguna (rechazo en 1–2) |

## Placeholder scan

Sin TBD. Los pasos Commit se omiten hasta que Luis lo pida. `restore_keep_alive` usa `/api/generate` con timeout 3 s, no un TODO de «ping Ollama».

## Type consistency

- `foreground_server_launch(..., windows: Optional[bool] = None)`
- `pin_for_run(run_id, endpoint, model)` / `keep_alive_override(run_id)`
- `TurnLedger.needs_ui_verify(user_text: str) -> bool`
- `save_project_todos(project_id, todos)`
- Settings names exactly as in the spec table
