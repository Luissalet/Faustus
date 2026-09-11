# Escritorio semántico (ADP-08 + ADP-09)

`src/desktop_semantics/` añade un contrato de escritorio por IDENTIDAD de
control (rol, nombre, `automation_id`, ruta estructural) encima del backend
100% por coordenadas que ya existía (`src/agent_tools/desktop_tools.py::
DesktopBackend` — `click(x, y)`, `type_text`, ...). No sustituye nada: el
modelo sigue teniendo `desktop_screenshot`/`desktop_click`/... para lo que
UIA no ve (canvas dibujado a mano, algunos motores de juego); esto es una
alternativa para el caso común de "pulsa el botón Guardar" sin tener que
adivinar un píxel.

## Vocabulario (`src/desktop_semantics/contracts.py`)

- **`Snapshot`**: una lectura acotada (profundidad/tamaño) del árbol de
  controles de la ventana activa de una sesión — `snapshot_id`, `session_id`,
  `generation`, `app`, `window`, `elements[...]`, `truncated`, `taken_at`.
- **`Ref`**: `f"{session_id}:{generation}:{snapshot_id}:{n}"` — NUNCA un
  índice desnudo tipo `"e7"` (misma lección que WEB-04 en
  `src/browser_view.py`). `generation` sube cada vez que la identidad
  (app, window) de la sesión cambia (`session.take_snapshot`), así que toda
  ref de una generación anterior queda estructuralmente obsoleta sin tener
  que volver a recorrer el árbol.
- **`resolve(ref, snapshot_fresh, caller_session_id)`**: reencuentra el
  MISMO control dentro de un snapshot FRESCO, por identidad
  `(role, name, automation_id, path)` — nunca reproduciendo el índice `n`
  contra un árbol que ha cambiado de forma. Tres fallos tipados, nunca un
  "el primero que se parece":
  - `WrongSessionError` — la ref (o el snapshot fresco) es de otra sesión.
  - `StaleRefError` — generación distinta a la actual, snapshot/elemento
    origen ya no existe, o ningún elemento del snapshot fresco comparte
    identidad.
  - `AmbiguousTargetError` — dos o más elementos del snapshot fresco son
    igual de plausibles (`.candidates`); nunca se elige el primero.
- **`act(session_id, ref, op, backend, params?, precondition?, timeout?)`**:
  toma un snapshot fresco del backend, reresuelve la ref, comprueba
  `precondition` (`{atributo: valor_esperado}` sobre el elemento
  REresuelto), ejecuta `op` y devuelve `(Element, ActionResult)`.
- **`ActionResult(delivery, observed_after, verified)`**: tres hechos
  separados, nunca un booleano:
  - `delivery ∈ {delivered, not_delivered, unknown}` — `unknown` es un
    TIMEOUT del backend, no un fallo ni un éxito; `act()` lo devuelve tal
    cual y NUNCA reintenta la llamada por su cuenta (regla del repo: un
    efecto con `delivery: unknown` se reconcilia, no se repite a ciegas).
  - `observed_after` — lo que el backend leyó justo después de actuar (o
    `None` si no se observó nada).
  - `verified` — solo `True` cuando `observed_after` se releyó realmente Y
    `delivery == "delivered"`.

## Backends

- **`fake_backend.py`** — app en memoria para tests: `FakeDesktopBackend`
  (`.semantic()` → `FakeDesktopSemanticBackend`) con `add_control`,
  `swap_window` (cambia la identidad app/window → sube la generación en el
  siguiente snapshot), `delete_control`, `set_delay(op, segundos)` (simula
  una llamada colgada más allá del timeout del llamante → `TimeoutError` →
  `delivery="unknown"`), `set_response(op, {...})` e `invocations` (lista de
  cada `invoke()`, para comprobar que una acción `unknown` no se reintenta).
- **`windows_uia.py`** (ADP-09) — backend real sobre `pywinauto` (BSD-3),
  import perezoso SIEMPRE dentro de funciones: el paquete entero importa
  limpio en Linux/macOS o en Windows sin `pywinauto` instalado.
  `available()` es la única comprobación barata y siempre segura; sin ella,
  `snapshot()`/`invoke()` lanzan `RuntimeError` con el motivo. Nunca eleva
  privilegios; un control que UIA no ve simplemente no aparece en el
  snapshot (sin promesa de compatibilidad universal).
  **Verificación física en Windows queda pendiente, declarada** — todo lo
  de este módulo se ha ejercitado aquí solo contra `fake_backend.py`
  (entorno Linux); nada ha corrido contra un escritorio Windows real.
- **`src/agent_tools/desktop_tools.py::DesktopBackend.semantic()`** —
  capacidad opcional nueva, devuelve `None` por defecto (todo backend
  existente sigue funcionando sin cambios); `WindowsBackend.semantic()` la
  implementa de forma perezosa con `WindowsUiaSemanticBackend`. Ningún otro
  método de `desktop_tools.py` se ha tocado.

## Evidencia (`evidence.py`)

Reutiliza el MISMO sitio donde las tools desktop de coordenadas ya dejan su
rastro antes/después: `src.desktop_control_session.record_action` (que ya
escribe el ring buffer en memoria y `data/runtime/desktop-audit.log`) — sin
segundo almacén. `record()` guarda `delivery`/`verified` como campos
SEPARADOS dentro de `note` (JSON): un `delivered` puede seguir teniendo
`verified: False` (la llamada se envió; nadie releyó el control para
confirmarlo), y esa distinción tiene que sobrevivir a la traza.
`export_trace(session_id)` es una exportación de solo lectura, acotada a esa
sesión, que nunca incluye píxeles de captura (`_sanitize` descarta
`screenshot`/`image`/`images`/`b64`/`data` de `observed_after` — eso es
trabajo de `capture_evidence`/`desktop_screenshot`, no de este módulo).

## Tools del agente (`src/agent_tools/desktop_semantic_tools.py`)

```
desktop_snapshot   lectura   árbol de controles de la ventana activa, con un ref por control
desktop_find        lectura   busca en un desktop_snapshot YA TOMADO (sin captura nueva)
desktop_act         acción    invoke | select | set_value | scroll | focus sobre un ref
```

`desktop_snapshot`/`desktop_find` están clasificadas `READ_PRIVATE` (misma
clase que `desktop_screenshot`/`desktop_list_windows`: leen la pantalla
privada del propietario, contenido tan no fiable como una página remota).
`desktop_act` es `EXTERNAL_SIDE_EFFECT` (misma clase que `desktop_click`) y
pasa por la puerta de aprobación humana normal en cada llamada; además se
autorrechaza explícitamente cuando `desktop_control_mode=off`
(`src/tool_capabilities.py::desktop_control_mode`).

**Nota deliberada de alcance:** `desktop_act` NO se añadió a
`tool_capabilities.ALWAYS_APPROVE_TOOLS` (el conjunto "pregunta en
LITERALMENTE cada llamada, incluso dentro de una tarea ya aprobada" que usan
los cinco tools de coordenadas) — `tests/test_desktop_tools.py`
(fichero de otro lote, no tocado aquí) fija por IGUALDAD EXACTA que ese
frozenset son esos cinco nombres, y que `desktop_tools.DESKTOP_TOOLS`/
`DESKTOP_CONTROL_TOOLS` (derivados de él) son exactamente los siete
`desktop_*` heredados. Añadir `desktop_act` ahí rompía esas dos aserciones.
Efecto práctico: `desktop_act` pide aprobación (la puerta normal por
efecto externo) y se autorrechaza en modo `off`, pero no hereda el
"pregunta SIEMPRE aunque la tarea ya esté aprobada" ni el pruning
automático de `src/tool_preflight.py::_desktop_rule`. Siguiente paso para
quien posea ese test: ampliar sus dos aserciones a superconjunto y añadir
`desktop_act` a `ALWAYS_APPROVE_TOOLS`.

`ref` solo es válido para la sesión y la generación en la que se tomó — un
cambio de ventana/app lo invalida (`StaleRefError`); `desktop_find` no toma
una captura nueva, busca sobre el `desktop_snapshot` más reciente de la
sesión (o uno concreto por `snapshot_id`).

## Errores

`{"error": ..., "exit_code": 1, "error_class": "desktop_semantics.<motivo>"}`
— `stale_ref`, `wrong_session`, `ambiguous_target` (con `"candidates"`,
los índices `n` de los elementos plausibles), `precondition_failed`,
`unsupported_operation`, `backend_unavailable`.

## Pendiente para `docs/adaptations/provenance.json` (lo escribe W1-B)

Entrada a añadir: técnica adaptada de **lahfir/agent-desktop** (Apache-2.0)
— se adaptó el CONTRATO (snapshot acotado / ref con generación / resolución
por identidad / distinción `delivered`/`not_delivered`/`unknown`), NO su
backend macOS; el código de `windows_uia.py`/`fake_backend.py`/
`contracts.py`/`session.py` es implementación propia sobre `pywinauto`
(BSD-3, dependencia opcional). `destination`: `src/desktop_semantics/`.

## Tests

`tests/test_adp08_desktop_semantics.py` — los casos de aceptación de las
dos fichas contra `fake_backend.py` (ninguno contra Windows real, ver
arriba), evidencia, e integridad de registro de las tres tools nuevas en
`TOOL_TAGS`/`TOOL_HANDLERS`/`FUNCTION_TOOL_SCHEMAS`/
`tool_capabilities.KNOWN_CAPABILITY_TOOLS`/`tool_index.
BUILTIN_TOOL_DESCRIPTIONS`/`tool_registry.snapshot()`/
`tool_security.NON_ADMIN_BLOCKED_TOOLS`.

## §Canal — elección de canal (CMP-10, `channel.py`)

`src/desktop_semantics/channel.py::choose_channel(target, *, capabilities,
policy, preferred=None) -> ChannelDecision` decide CUÁL de cuatro canales
usa una acción, por objetivo y capacidades disponibles EN ESTA llamada —
nunca un orden global fijo:

- `app_api` — un endpoint/tool con nombre actúa en nombre del usuario (sin
  ventana, sin coordenadas).
- `dom_cdp` — árbol de accesibilidad/DOM del navegador integrado sobre CDP
  (el mundo snapshot/ref de `src/browser_view.py`).
- `native_a11y` — la capa semántica de este propio paquete
  (`contracts.py`/`session.py`, UIA hoy).
- `pixels` — clics/teclas por coordenada, sin identidad — el fallback
  ORIGINAL, siempre disponible; cada otro canal es algo que Faustus prefiere
  usar EN VEZ de este cuando puede.

El orden de candidatos depende de `target["kind"]` (`api`/`browser`/
`desktop`; cualquier otro valor cae a un orden por defecto que también deja
`pixels` al final). `capabilities` son las capacidades REALES de esta
llamada (p. ej. `native_a11y=False` si el backend UIA no está disponible
ahora mismo), no lo que existe en abstracto; `policy["allow_pixels"]=False`
quita `pixels` de la lista de candidatos por completo. Si ningún candidato
es usable — incluido `pixels`, cuando la política lo permite — se lanza
`BackendUnavailableError`: `choose_channel` nunca inventa un canal que
nadie ofreció como capacidad.

`ChannelDecision{channel, reason, risk_change, requires_visible_fallback}`:
`risk_change` es `True` exactamente cuando el canal elegido NO es
semántico (`SEMANTIC_CHANNELS = {app_api, dom_cdp, native_a11y}`) pero el
canal natural para ese tipo de objetivo SÍ lo era — el caso
"semántico→píxeles" de la ficha. `requires_visible_fallback` es `True` en
ese mismo caso (o cuando `policy["require_fallback_visibility"]` lo pide
explícitamente) y OBLIGA a dejarlo visible: `record_fallback(session_id,
decision, target=...)` añade UNA entrada al MISMO rastro de auditoría que
`desktop_act`/`evidence.py` ya usan
(`src.desktop_control_session.record_action`, herramienta
`"desktop_channel:decision"`, `note` JSON con `channel_fallback: true`) —
sin segundo almacén.

**Invalidación de generación (`session.invalidate_generation(session_id)`,
nueva en este lote):** fuerza el bump de generación y vacía la memoria de
snapshots de una sesión AHORA MISMO, sin esperar a que el próximo
`take_snapshot` note un cambio de ventana/app por sí solo. `desktop_act`
la llama cuando la decisión de canal exige visibilidad de fallback
(`native_a11y` dejó de estar disponible entre un `desktop_snapshot` y el
`desktop_act` que usa su ref): las acciones por píxeles esquivan el
sistema de refs por completo, así que cualquier ref que la sesión siga
sosteniendo deja de ser fiable sin una captura nueva. **Límite declarado:**
el otro disparador que describe la ficha — "el usuario toma el control" del
escritorio — no está cableado aquí: `src/desktop_control_session.py` (el
handshake de indicador + cancelación con Escape) no es un fichero de este
lote (`toca SOLO tus ficheros`), así que esa vía queda como
`invalidate_generation` disponible y documentada, sin llamada automática
desde la cancelación por Escape — pendiente para quien posea ese fichero.

## Wiring en `desktop_act`

`DesktopActTool._execute` calcula `choose_channel({"kind": "desktop"},
capabilities={"native_a11y": <backend.semantic() disponible>, "pixels":
True}, policy={"desktop_control_mode": ...})` antes de actuar. Hoy
`desktop_act` SOLO sabe ejecutar por `native_a11y` (es su razón de ser); si
la decisión no es `native_a11y`, se rechaza explícitamente señalando
`desktop_screenshot`/`desktop_click`/`desktop_type` (canal `pixels`) como
alternativa, en vez de degradar en silencio a una acción por coordenadas
que esta tool no sabe ejecutar. Una llamada exitosa devuelve `"channel":
"native_a11y"` en el resultado (`desktop_act` devuelve el canal usado, tal
como pide la ficha).
