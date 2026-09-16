# Cerrar el bucle del agente local (servidor, VRAM, UI, plan)

**Date:** 2026-09-16  
**Status:** Implemented (FAUSTUS.md §90). Server guard, keep_alive pin, UI verify, project todos, continue-turn budget. Code is on this tree; not committed unless Luis asks.  
**Problem source:** chats Silhouettes 14–16 sep 2026, sobre todo `782b7d89` (qwen3.8:27b-q4_K_M). El último turno escribió parches, dijo «reinicio el servidor y verifico en el navegador», lanzó `nohup python app.py`, el bash no volvió, Ollama descargó el modelo. El canvas seguía en `viewBox 0 0 200 200`, los thumbnails no existían en el DOM, el drag perdía `normalization_pose`.

Relacionado: FAUSTUS.md §§85–89 (sandbox Windows, loop-breaker, adjuntos como destinos, `node --test`, typo de workspace). Esos parches cubren fallos *anteriores* de la misma serie. Esta spec cubre lo que esos parches no tocan.

## Problem

Un modelo local de 27B Q4 no «se vuelve tonto» a los 40 minutos. El harness le deja *parecer* que está cerrando un trabajo de UI de muchas horas:

1. **El guard de servidores no reconoce el lanzamiento real.** `_SERVER_LAUNCH_RE` pilla `flask run` y `python -m uvicorn`. No pilla `python app.py`. `_BACKGROUNDED_RE` trata `nohup` y `&` como detached. En Windows/Git Bash eso no despega un Flask: el turno se queda en `tool_progress` con `tail: ""` más de 14 min. El test `test_foreground_server_launch_detection` *exige* que `nohup python -m uvicorn … &` esté permitido.
2. **Ollama descarga el peso en cuanto deja de haber inferencia.** `keep_alive` va en cada `/api/chat`. El último generate del turno manda el valor guardado (a menudo 5 m). El bash posterior no refresca el timer. Resultado en la barra: `no model`, 2.3/44 GB. El agente no puede continuar aunque el servidor del usuario sí esté arriba.
3. **Verified no significa «se ve bien».** El ledger acepta `edit_file` + `node --check` / pytest. Un objetivo `Verify all fixes in browser` se puede marcar completed sin `browser_snapshot`. El harness de afirmaciones no sabe qué es un bug visual.
4. **El plan de 170 k se reinyecta en cada chat nuevo.** `user_authored_text()` ya corta adjuntos para no tratarlos como destinos (§88). Siguen ocupando el prompt. Qwen rediagnostica el mismo bug en vez de leer las 15 líneas de `_previewPose`. Los `todowrite` viven por `session_id` (`data/agent_todos/<sid>.json`), no por proyecto.
5. **El idle watchdog no salva este caso.** Default 300 s, pero el timeout adaptativo (3× mediana de comandos previos) se entrena con pytest/compiles largos y deja vivir un Flask mudo. Además, si `&` no despega, el progreso SSE cada 2 s no cuenta como stdout: el hang de 870 s implica que el adaptativo o un setting local subió el suelo por encima de 5 min, o el proceso *sí* escribía lo bastante para resetear el idle.

El modelo aporta lo suyo (narran en vez de verificar, typo de rutas, Q4 flojo en tools). El harness convierte esos fallos en turnos de una hora que *mienten* Verified.

## Goals

- Un `python app.py` / Flask / uvicorn no puede bloquear el turno en Windows aunque lleve `nohup` o `&`. La única vía de servidor persistente es `#!bg` → `bg_jobs` / `manage_bg_jobs`.
- Mientras hay un run de agente activo, el modelo de Ollama de ese run no se descarga por idle. Al terminar el run, se restaura el `keep_alive` guardado.
- Un turno que muta UI (html/css/js de editor, templates, static/) no puede cerrar como verified sin evidencia de browser (navigate + snapshot o evaluate), salvo que el usuario lo desactive.
- Un «sigue el plan» en un chat nuevo del mismo proyecto arranca con los todos incompletos y el working set de ficheros, no con el spec entero otra vez.
- Los comandos con pinta de servidor usan un idle corto y fijo, no el adaptativo.

## Non-goals

- Hacer que qwen3.8:27b-q4 se comporte como Claude. El techo del modelo sigue ahí.
- Subir `num_ctx` / VRAM como arreglo principal.
- Reescribir el tool-RAG ni el Context Engine.
- Autocambiar de modelo (27B → coder) sin que Luis lo pida. Como mucho, un aviso.
- Convertir automáticamente un Flask en `#!bg` sin decírselo al modelo (silencioso = el modelo cree que `curl` ya corrió). Rechazar con el texto de `#!bg` es el contrato actual y se mantiene.
- UI nueva en Studio para este trabajo (SSE/`keep_alive` en el pill de uso sí, si sale barato).

## Chosen approach

Cinco capas, cada una entregable sola, en este orden. El rechazo es preferible a la magia.

```text
bash/powershell content
        │
        ▼
  ¿parece servidor?  (python app.py, flask, uvicorn, npm start, …)
        │
        ├─ no ──► idle adaptativo (como hoy)
        │
        └─ sí
              │
              ├─ primer línea #!bg ──► bg_jobs (como hoy)
              ├─ timeout N / Start-Process ──► permitido
              ├─ Windows + nohup/&/disown ──► BLOQUEAR (hoy se permite)
              └─ POSIX + nohup/& ──► permitido (sí despega)

cada /api/chat del run
        │
        ▼
  keep_alive = max(guardado, agent_run_keep_alive)   # default 2h
        │
        ▼
  run finally ──► POST Ollama keep_alive = valor guardado

cierre de turno
        │
        ▼
  ¿mutó UI o el user pidió ver el editor?
        │
        ├─ no ──► verified como hoy (tests + ledger)
        └─ sí ──► exige evento browser_* ok en el ledger
                  si no: harness check + una ronda, luego nota visible
```

## Architecture

### 1. Guard de servidores (Windows-correcto)

**Dónde:** `src/agent_tools/subprocess_tools.py` (`foreground_server_launch`, `_SERVER_LAUNCH_RE`, `_BACKGROUNDED_RE`), tests en `tests/test_subprocess_hardening.py`. El prompt `local_model_policy()` en `src/agent_harness.py` menciona uvicorn/flask run; hay que nombrar `python app.py` y «en Windows nohup no cuenta».

**Detección nueva (además de lo que ya está):**

- `python[w]?[.exe]?\s+[\w./\\-]*app\.py`
- `python[w]?[.exe]?\s+-m\s+flask\b`
- `py\s+-3\s+app\.py` y equivalentes razonables
- el intérprete del venv: `.venv/Scripts/python.exe app.py`

Falsos positivos a no bloquear (ya cubiertos o a fijar en test):

- `python -m pytest`, `python -c`, `python app.py --check`, `python -m py_compile`
- `node --check`, `npm test`, `npm run build`

**Detached real:**

| Señal | POSIX | Windows (Git Bash / PowerShell) |
|---|---|---|
| `#!bg` (ya ramifica en `tool_execution` antes) | sí | sí |
| `timeout N` / `gtimeout` | sí | sí |
| `Start-Process` | n/a | sí |
| `nohup`, `&` al final, `disown`, `setsid` | sí | **no** |

`foreground_server_launch` debe aceptar un flag de plataforma (hoy usa el OS real; los tests podrán inyectar `windows=True`).

El mensaje de error ya pide `#!bg`. No cambiar el copy salvo añadir: «`nohup`/`&` no despegan un servidor en Windows».

### 2. Pin de `keep_alive` durante el run

**Dónde:** módulo nuevo pequeño `src/run_model_pin.py` (un dict in-process `run_id → {endpoint, model}`). `src/llm_core.py` ya mezcla `keep_alive` desde `model_load_options.resolve_for_request`. El pin del run gana al valor guardado, pierde frente a un override explícito del usuario (`/keepalive` si existiera; si no, no inventar comando).

**Ciclo de vida:**

1. Al empezar el lane local de un run (`agent_loop` / `agent_runs`): `pin_for_run(run_id, endpoint, model)`.
2. Cada request Ollama de ese run: `keep_alive = agent_run_keep_alive` (setting, default `"2h"`). Ollama interpreta `"2h"` y `"-1"`. Preferir `"2h"` a `-1` para no dejar el peso clavado si el proceso muere sin `finally`.
3. `finally` del run: `unpin_for_run(run_id)` hace un POST barato (`/api/generate` con `prompt` vacío o un ping, `keep_alive` = valor guardado o `"5m"`). Fallo de red = log, no tumba el turno.

**Qué no hacer:** un heartbeat cada 30 s hacia Ollama mientras corre bash. El `keep_alive` de la última inferencia ya cubre el hueco si es ≥ el timeout duro de bash (1 h) o el idle. `"2h"` cubre el timeout de 1 h con margen.

Setting: `agent_run_keep_alive` (string Ollama, default `"2h"`), `agent_run_keep_alive_restore` (default `true`).

### 3. Idle corto para comandos de servidor

Aunque el guard falle (regex nueva, comando raro), un Flask mudo no puede heredar 3× la mediana de pytest.

En `_on_host` / PowerShell, si `looks_like_server_launch(content)` (misma regex, **ignorando** `_BACKGROUNDED_RE`): `idle_timeout = min(effective, agent_server_idle_timeout_seconds)` default **45**. No adaptativo.

Esto mata el hang de 14 min incluso si alguien desactiva el guard.

### 4. Contrato de verificación UI

**Ledger** (`src/agent_harness.py` `TurnLedger`):

- Clasificar paths mutados: `_UI_PATH_RE` = `\.(html?|css|s?css)$` o `/static/` o `/templates/` o `\.(jsx|tsx|vue)$` (además de `.js`/`.mjs` bajo `static/` / `editor`).
- Tools de evidencia browser: nombre que contiene `browser_snapshot`, `browser_take_screenshot`, `browser_evaluate`, `browser_navigate` (prefijo MCP irrelevante).
- `needs_ui_verify()` = setting on AND (mutó UI OR el texto del usuario casa `_UI_INTENT_RE`: `browser|navegador|viewport|canvas|thumbnail|drag|visual|editor 2d|capa`).
- Al cerrar: si `needs_ui_verify()` y no hay evento browser ok → mismo mecanismo que claims sin write: ronda harness (máx. 1) con el texto «los cambios de UI no están verificados en el navegador; llama browser_navigate + browser_snapshot». Si agota: `verified=false` y nota visible, **aunque** pytest/node pasen.

**todowrite:** un item cuyo `content` casa `verif|browser|screenshot|navegador|captura` no puede pasar a `completed` con `verified: true` sin evidencia browser en el tramo. Se marca `verified: false` como ya hace con los completed sin tools.

**Tool schema:** si `needs_ui_verify()` se ve venir al *inicio* del turno (intent del user) o tras la primera mutación UI, `_expand_browser_mcp_tools` debe incluir el set de sesión aunque el índice semántico no haya recuperado `builtin_browser`. Hoy `_browser_intent_is_real` exige un hit de índice para no hinchar el prompt 12k→38k. La expansión forzada va **después** de mutar UI, no en el round 0 de un «arregla el parser». Setting `agent_ui_verify` default `true`.

**project_tests:** no cambiar el AND pytest+node. Añadir un campo `ui_verify` en el payload de verified (ok/skipped/missing) para la tarjeta. Missing no pinta tests en rojo; pinta la nota de harness.

### 5. Continuidad de plan (sin reinyectar 170 k)

Tres piezas, una sola semántica: el proyecto tiene un working state.

**a. Todos de proyecto.** Hoy `save_todos(session_id)`. Añadir `save_project_todos(project_id, todos)` en `data/agent_todos/project-<id>.json`. Cada `todowrite` escribe sesión *y* proyecto si el chat tiene `project_id`. Un chat nuevo del mismo proyecto, en el primer turno, inyecta un bloque:

```
Incomplete work for this project (from the previous chat):
- [in_progress] Diagnose why layers appear giant when dragged
- [pending] Verify all fixes in browser
Last files touched: static/editor/viewport2d.js, …
```

Si no hay incompletos, no inyectar.

**b. Recorte de adjuntos en el prompt, no solo en el ledger.** `user_authored_text()` ya corta para paths. El builder de mensajes debe aplicar el mismo corte (o un techo `agent_inline_attachment_max_chars`, default 4000) al cuerpo que ve el modelo, dejando: título del adjunto, TOC de headings `^#{1,3} `, y «full text is in the workspace / attachment id, read the section you need». El spec entero sigue en disco.

**c. Continue-turn.** Si el mensaje casa `keep implementing|sigue(e)? (el )?plan|continue the implementation` y hay todos de proyecto o un run previo de la sesión: inyectar ledger resumido (últimas 8 tools, último error, files touched) **en vez de** compactar eso y dejar el spec. Reutilizar `context_overflow` stubs si el turno anterior spilló.

### 6. Checklist de cierre (inyección, no una tool nueva)

Cuando el ledger tiene mutaciones UI y el modelo emite texto final sin browser tools, la ronda harness de (4) *es* el checklist. No hace falta otra tool. El texto de la ronda lista: URL esperada si se conoce (`http://127.0.0.1:5000/editor` no se inventa: si el modelo ya hizo curl a un host, reutilizarlo; si no, «abre la ruta del editor del proyecto»).

### 7. Aviso de modelo (opcional, barato)

Si el modelo del chat casa `qwen3.8` y el turno lleva >40 rondas o compactó mid-turn, un `system_notice` SSE una vez: «este modelo se degrada en bucles largos de tools; qwen3-coder suele cerrar mejor». Sin autowitch.

## Settings (nuevos)

| Key | Default | Rol |
|---|---|---|
| `agent_run_keep_alive` | `"2h"` | keep_alive Ollama durante un run local |
| `agent_run_keep_alive_restore` | `true` | ping de restore al terminar |
| `agent_server_idle_timeout_seconds` | `45` | idle fijo si el comando parece servidor |
| `agent_ui_verify` | `true` | exigir browser evidence en turnos UI |
| `agent_inline_attachment_max_chars` | `4000` | techo del spec inlined que ve el modelo |
| `agent_project_todos` | `true` | persistir todos a nivel proyecto |

## Files

| File | Responsibility |
|---|---|
| `src/agent_tools/subprocess_tools.py` | regex servidor, detached por OS, idle corto |
| `tests/test_subprocess_hardening.py` | contrato win32 vs posix |
| `src/run_model_pin.py` | pin/unpin keep_alive por run_id |
| `src/llm_core.py` | mezclar pin en payload |
| `src/agent_loop.py` | pin en start/finally; forzar browser tools; recorte adjuntos; inject project todos |
| `src/agent_harness.py` | `needs_ui_verify`, nota, ronda; policy text |
| `src/agent_tools/coding_tools.py` | todos de proyecto |
| `src/settings.py` + `src/agent_settings_schema.py` | knobs |
| `src/project_tests.py` | campo `ui_verify` en el resumen (no cambia ok de tests) |
| `FAUSTUS.md` | §90 cuando esté medido en vivo |

## Success (medible en esta máquina)

Reproducir el comando del 16-09:

```
cd "…\Silhouettes" && nohup python app.py > server.log 2>&1 &
sleep 3
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:5000/editor
```

- En Windows: `exit_code == 2`, error con `#!bg`, el turno sigue, `ollama ps` sigue mostrando el modelo a los 8 min si el run no ha acabado.
- Un turno que solo edita `static/editor/viewport2d.js` y cierra sin browser: tarjeta **no** verified (o verified con nota UI missing) y una ronda harness.
- Chat nuevo en el proyecto Silhouettes: el primer prompt del modelo contiene los todos incompletos, no el spec de 170 k.

## Out of scope leftovers (backlog, no este plan)

- Autopromoción `python app.py` → `#!bg` sin round-trip.
- Persistencia de `viewBox`/estado del editor como oráculo.
- Segunda tarjeta GPU dedicada al coder en paralelo (ya documentado: el mismo blob no corre en dos runners).
- Deshacer el botón «Deshacer» pendiente del editor Silhouettes (app del usuario, no Faustus).
