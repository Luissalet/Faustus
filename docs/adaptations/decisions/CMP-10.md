# CMP-10 — Canal de acción explícito para el escritorio semántico

**Versión y recorrido:** base `95747d9` (branch `master`, sin git en este
lote). Ola 1 de hoy había construido `src/desktop_semantics/` (ADP-08/09:
`contracts.py`, `session.py`, `evidence.py`, `fake_backend.py`,
`windows_uia.py`) y las tres tools `desktop_snapshot`/`desktop_find`/
`desktop_act` (`src/agent_tools/desktop_semantic_tools.py`) — ver
`docs/adaptations/baseline.md` filas ADP-08/09/10. Este lote (W2-H) añade
`channel.py` encima de eso y cablea `desktop_act` para usarlo.

**Solución actual (antes de este cambio):** `desktop_act` siempre asumía
el canal `native_a11y` — llamaba a `ds.act(...)` directamente, y si el
backend semántico no estaba disponible, `ds.act` lanzaba
`BackendUnavailableError` con un mensaje genérico. No existía el concepto
de "canal" como decisión explícita, ni distinción entre `app_api`/
`dom_cdp`/`native_a11y`/`pixels`, ni un evento/evidencia específico para un
cambio de riesgo semántico→píxeles, ni una forma de invalidar refs
explícitamente sin esperar al siguiente `take_snapshot`.

**Solución de referencia (INFORME §3.9):** `choose_channel(target, *,
capabilities, policy) -> ChannelDecision{channel ∈ {app_api, dom_cdp,
native_a11y, pixels}, reason, risk_change: bool,
requires_visible_fallback: bool}` — decisión por objetivo/capacidades/
permisos, no un orden fijo; un fallback que cambia el riesgo
(semántico→píxeles) queda VISIBLE; el usuario toma el control o cambia de
ventana invalida refs (`session.invalidate_generation()`); `desktop_act`
devuelve el canal usado.

**Mecanismo concreto de la diferencia:** `src/desktop_semantics/
channel.py::choose_channel` construye el orden de candidatos por
`target["kind"]` (`api`/`browser`/`desktop`, con un orden por defecto que
también deja `pixels` al final), filtra por `capabilities` (lo realmente
disponible EN ESTA llamada) y por `policy["allow_pixels"]`, y devuelve una
`ChannelDecision` inmutable. `risk_change` es `True` exactamente cuando el
canal elegido no es semántico pero el canal natural para ese `kind` sí lo
era. `record_fallback(session_id, decision, target=...)` añade UNA entrada
al MISMO rastro de auditoría que `desktop_act`/`evidence.py` ya usan
(`src.desktop_control_session.record_action`, sin segundo almacén), con
`channel_fallback: true` en el `note` JSON. `session.invalidate_generation
(session_id)` (nuevo en `session.py`) fuerza el bump de generación +
vacía snapshots sin esperar al próximo `take_snapshot`. `desktop_act`
(`src/agent_tools/desktop_semantic_tools.py`, edición anclada) calcula la
decisión antes de actuar; si no es `native_a11y`, se rechaza explícitamente
señalando las tools de coordenadas como alternativa (hoy `desktop_act` solo
sabe ejecutar por `native_a11y`); si `requires_visible_fallback`, registra
el evento y llama a `invalidate_generation`; una llamada exitosa devuelve
`"channel": "native_a11y"` en el resultado.

**Cobertura:** **parcial, declarada.** Cubierto: la decisión de canal en
sí (los cuatro canales, orden no fijo, capacidades/política, `risk_change`/
`requires_visible_fallback`), la visibilidad del fallback (evidencia con
`channel_fallback: true`), y `desktop_act` devolviendo el canal usado.
**No cableado en este lote** (límite declarado, no silencioso): el
disparador "el usuario toma el control del escritorio" (la otra mitad de
"invalida refs" en la ficha) — `src/desktop_control_session.py` (el
handshake de indicador + cancelación con Escape) no es un fichero de este
lote (regla "toca SOLO tus ficheros"; ningún otro CMP de esta ola lo toca
tampoco, así que queda como trabajo disponible, no como conflicto). Los
canales `app_api`/`dom_cdp` existen en `choose_channel` como lógica pura
(con tests) pero no están cableados a ningún llamador real todavía — solo
`native_a11y`/`pixels` tienen un consumidor real (`desktop_act`).

**Estado comparativo:** hipótesis de mejora sobre lo que ya existía
(ADP-08/09/10 ya cubrían snapshot/ref/resolve/act/evidencia; esto añade la
pieza de canal explícito que la ficha pedía y que no existía). No hay
"solución externa" con la que comparar profundidad aquí — la ficha describe
un contrato propio, no una integración de terceros — así que "ventaja
externa documentada" no aplica.

**Decisión:** **extender.** `channel.py` se añade sobre el módulo
`desktop_semantics` ya existente sin sustituir nada de `contracts.py`/
`session.py`/`evidence.py`; `desktop_act` se edita mínimamente (ancla
única) para consumirlo.

**Pruebas ejecutadas:**
- `python3 -m pytest tests/test_cmp10_channel.py -q -p no:cacheprovider -W ignore` → 11 passed.
- `python3 -m pytest tests/test_adp08_desktop_semantics.py -q -p no:cacheprovider -W ignore` → 35 passed (sin regresión: los fakes siempre ofrecen `native_a11y`, así que la decisión de canal es transparente para esos tests).
- `python3 -c "import app"` (boot completo) + `TestClient` contra `/api/external-runtimes/herdr/*` (ver CMP-06) confirmando que `app.py` sigue arrancando con el resto de rutas del lote — no aplica directamente a `channel.py` pero comparte el mismo `app.py` editado.
- No ejecutado: nada contra un backend UIA/Windows real (declarado pendiente ya desde ADP-09).
