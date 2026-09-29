# H15: comentarios de arquitectura contrastados con los callers

Implementado el alcance señalado por el análisis: encabezados de
`src/context_engine/wiring.py` y `src/code_mode/bridge.py`.

El motor de contexto dispone de entrega real mediante `deliver_round`, además
de observación shadow independiente. Ambas opciones están desactivadas por
defecto. La entrega real sí puede modificar el contexto del modelo; solo la
observación está limitada a la primera ronda. Se elimina también la promesa
incorrecta de que toda compilación debe costar menos de un segundo.

La ruta normal de Code Mode transmite `security_context` y `tool_policy` desde
el dispatcher al handler, runner y bridge. El contexto nuevo es un fallback
para callers que lo omiten; no reconstruye la historia de un run. Las políticas
de `tools.call` no confinan el Python directo del proceso host.

Verificación: lectura de los callers actuales en `tool_execution.py`,
`agent_tools/code_mode_tool.py`, `code_mode/runner.py`, `agent_loop.py` y los
defaults en `settings.py`. Solo cambian docstrings; no se añaden pruebas que
validen redacción ni se altera comportamiento. Se compara AST sin docstrings
contra HEAD antes del commit.

Procedencia: hallazgo H15 del [análisis local](CODEX_HARNESS_ANALISIS_2026-09-29.md),
realizado al comparar con [Codex b1e72963](https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9).
Redacción propia, sin copia de código upstream. Esto cierra esos dos comentarios
históricos; no implica una auditoría de todos los comentarios del repositorio.
