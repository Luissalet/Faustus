# Condensar a mano — `/api/session/{id}/condense*`

CONTRATO_CABLES2 F3. Un botón manual, distinto de la compactación
automática: el usuario elige un rango explícito de turnos ya cerrados
("esto ya está resuelto, pliégalo") y lo colapsa en una única fila de
resumen, con deshacer completo.

Implementación: `src/condense.py` (lógica + acceso a BD, sin FastAPI),
`routes/condense_routes.py` (rutas).

## Cómo se relaciona con la compactación automática

`src.context_compactor.maybe_compact` compacta el **prompt** de un turno
cuando se acerca al límite de contexto del modelo — automático, disparado
por presupuesto, opera sobre mensajes ya filtrados por `build_chat_context`.

`condense()` opera sobre `session.history` directamente, por índice de
fila — manual, disparado por el usuario, y **persiste**: de verdad reduce
lo que cada turno futuro tiene que reenviar, vía
`SessionManager.replace_messages` (el mismo camino de escritura durable que
usa `context_compactor._update_session_history` y el restore de versiones
en `routes/history/history_routes.py`).

Ambos comparten el mismo prompt de auto-resumen, la misma resolución de
modelo/endpoint y la misma puerta de privacidad: `condense()` llama a
`src.context_compactor.summarize_rows(rows, endpoint_url=..., model=...,
headers=..., owner=...)`, la función que `maybe_compact` extrajo para este
mismo fin — un rango condensado a mano y uno compactado automáticamente se
leen igual de cara al modelo.

## Reglas del rango

- `0 <= start < end <= len(history) - 2` — **la última fila de la historia
  nunca es elegible**: es el turno vivo con el que el usuario está a mitad
  de conversación, no historia asentada.
- El rango cubre al menos 2 filas.
- Ninguna fila del rango puede llevar ya `metadata.condensed` (ya
  condensada a mano) ni `metadata.compacted` (ya plegada por compactación
  automática) — condensar una fila condensada anidaría resúmenes en
  silencio; el arreglo es expandirla primero (`error_class:
  "condense.nested"`, 409).

Owner-scoped como `src.side_threads`: una sesión ajena responde
exactamente igual que una inexistente (404). Errores planos: `{"error":
str, "error_class": "condense.<motivo>"}` — nunca anidados bajo `detail`.

## Deshacer: `condensed_from`

`condense()` guarda las filas originales completas (role, content,
metadata — sin `_db_id`, que `replace_messages` reasigna de nuevas en cada
escritura) dentro de `metadata["condensed_from"]` de la propia fila de
resumen. `expand()` no lee nada más: reconstruye esas filas tal cual y las
reinserta donde estaba el resumen.

CONTRATO_CABLES2 mencionaba `src.chat_versions` como red de seguridad
opcional ("si la API lo permite sin cambios"). Este módulo no lo integra:
`chat_versions` está pensado para guardar una COLA TRUNCADA (el undo de
`/truncate` vía `keep_count`), no un rango arbitrario a mitad de la
historia — forzarlo aquí dejaría `condensed_from` como una segunda copia
redundante y con otra forma de las mismas filas. `condensed_from` ya es un
deshacer completo y exacto (round-trip byte a byte, cubierto por
`tests/test_condense.py`), así que se queda como el único mecanismo.

## Rutas

| Método | Ruta | Body / Query | Respuesta |
|---|---|---|---|
| GET | `/api/session/{id}/condense/preview` | query `start`, `end` | `{"rows", "tokens_before", "tokens_after_estimate", "turns": [{"index","role","excerpt"}]}` |
| POST | `/api/session/{id}/condense` | `{"start", "end"}` | `{"summary_index", "removed", "tokens_before", "tokens_after"}` |
| POST | `/api/session/{id}/condense/{index}/expand` | — | `{"restored": n}` |

`GET .../condense/preview` no hace ninguna llamada a LLM — solo valida el
rango y estima tokens (`src.model_context.estimate_tokens`), para que el
`CondenseDialog` del Studio lo pueda llamar en cada edición de `start`/
`end` sin coste.

`POST .../condense` es async: llama a `summarize_rows` (una llamada real al
modelo de utilidad de la sesión). Si falla — el modelo, la red, la puerta
de privacidad — no degrada en silencio: propaga `condense.summary_failed`
(502) y la historia queda intacta (el `replace_messages` final nunca se
alcanza).

`error_class` conocidos: `condense.not_found` (404), `condense.
range_invalid` (400), `condense.nested` (409), `condense.summary_failed`
(502), `condense.not_condensed` (404), `condense.persist_failed` (500).

## Fila de resumen

La fila que reemplaza el rango es `role="system"`, con `content` de la
forma `[Condensed: turns {start+1}–{end+1}]\n{resumen}` y
`metadata`:

```json
{
  "condensed": true,
  "condensed_from": [{"role": "...", "content": "...", "metadata": null}, ...],
  "range": [start, end],
  "model": "...",
  "created_at": "..."
}
```

`summary_index` en la respuesta de `POST .../condense` es el índice de esa
fila en la NUEVA historia — igual a `start`, ya que todo lo anterior al
rango no se mueve. Ese mismo índice es el que `POST .../condense/{index}/
expand` espera.
