# H22 parcial — intentos sin observación no borran pruebas anteriores

Fecha: 2026-09-30. Adaptación local del [backlog H22](CODEX_HARNESS_ANALISIS_2026-09-29.md),
con fuente original fijada [Codex b1e72963c3b71a9265a551e54beff078384efed9](https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9).
No se repite la revisión upstream ni se ejecutan modelos o servicios externos.

## Pérdida reproducida

`save_tested` fusionaba registros con update sin distinguir observación y ausencia
de observación. Un probe omitido por presupuesto agotado (`ok=None`) sustituía
un True anterior; al recuperar el manifiesto, la assertion pasaba de verified a
claimed. Un False anterior perdía igualmente el rechazo y su degraded fallback.
La docstring ya prometía no borrar resultados al omitir vision, pero sólo
protegía claves ausentes, no los skipped que `run_calibration` genera explícitamente.

## Cambio acotado

Si el manifiesto ya tiene un booleano exacto True/False y el intento entrante no
tiene otro booleano exacto, conserva la observación, evidence, source y tested_at.
Añade `last_attempt` con sólo ok=None, attempted_at (hasta 64 caracteres) y estado
fijo skipped/unknown. La nota sustituye a la anterior, sin historial creciente ni
copias nuevas de prompts, errores, URLs o credenciales. El timestamp observado
no cambia ni se afirma revalidación. updated_at sigue indicando actualización del
manifiesto, no fecha de prueba.

Un resultado booleano nuevo sí reemplaza la observación anterior y la nota del
intento. Sin observación previa se mantiene el registro desconocido tal cual;
una capacidad sólo anunciada sigue siendo claimed, nunca verified por este cambio.
El anuncio y las otras capacidades siguen fusionándose como antes. No cambia la
key, el formato superior del manifiesto ni los lectores de assertions. El side
ledger de deployment conserva su ruta previa; un skipped no genera evidencia nueva.

## Evidencia local

`tests/test_model_calibration_skipped_probes.py` usa save/get real sobre JSON temporal
y assertions reales. Desactiva el side write para no tocar SQLite personal.
Trece casos: skipped/unknown tras True y False, inicial unknown, cuatro reemplazos
booleanos, evidence/source/timestamp originales, nota acotada y mezcla de otras
capacidades. Selección inicial con calibration/capabilities: 46 pasan en 2,08 s;
la selección ampliada también incluye router y model_identity: **104 pasan en
3,86 s**. `git diff --check` limpio salvo aviso CRLF.

## Límites

La observación retenida es histórica, no garantía de capacidad actual: errores de
transporte recientes quedan como last_attempt=unknown. No añade caducidad ni política
de selección frente a error reciente; los lectores existentes siguen mostrando la
última observación disponible. No se implementan probes por proveedor/protocolo.

El aislamiento endpoint/protocolo queda pendiente explícito: los manifests Ollama
con digest todavía comparten key entre endpoints. Una prueba en A puede verse como
verified desde B. Corregirlo requiere migración de identidad y consumidores, sin
convertir evidencia legacy de origen desconocido en una prueba local.
