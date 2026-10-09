# Línea de tiempo de un turno de voz - `/api/voice/timeline*`

Implementación: `src/voice_timeline.py` (marcas, huecos, almacén, p50/p90),
`routes/voice_timeline_routes.py` (REST), `studio/src/voice/timeline.ts` (marcas
del cliente y línea compacta), herramienta MCP `voice_turn_timeline` del
servidor integrado `harness`.

## Qué mide

Un turno de voz pasa por seis instantes. Cada uno se anota donde ocurre, en
milisegundos desde la época (reloj de pared):

| Marca | Cuándo | Quién la anota |
|---|---|---|
| `speech_end` | el detector de silencio cierra el turno, o se suelta el pulsador | cliente |
| `transcript_ready` | termina la voz a texto (y la limpieza opcional del dictado) | cliente |
| `request_sent` | el mensaje sale hacia el modelo | cliente |
| `first_token` | llega el primer texto de la respuesta | cliente |
| `tts_chunk` | vuelve la primera frase hablada, lista para sonar | cliente |
| `first_audio` | ese audio empieza a sonar | cliente |

Todas las marcas del cliente vienen del mismo reloj, así que los huecos entre
ellas son exactos. El servidor aporta lo que solo él mide, como números
aparte (nunca se restan de un instante del cliente):

- `stt_server_ms`: lo que tardó `POST /api/stt/transcribe` (también devuelto
  en su respuesta como `server_ms`).
- `tts_server_ms`: lo que tardó `POST /api/tts/synthesize` (cabecera
  `X-Faustus-Synth-Ms`).
- `server_ttft_ms`: tiempo hasta el primer token según el servidor; el campo
  existe y se guarda si llega, pero el estudio todavía no lo envía.

## Huecos

Se calculan en milisegundos; `null` si falta una marca o el orden es
imposible (negativo o de más de 10 minutos).

| Hueco | De -> a |
|---|---|
| `speech_to_text` | `speech_end` -> `transcript_ready` |
| `text_to_first_token` | `transcript_ready` -> `first_token` |
| `first_token_to_audio` | `first_token` -> `first_audio` |
| `text_to_request` | `transcript_ready` -> `request_sent` |
| `request_to_first_token` | `request_sent` -> `first_token` |
| `first_token_to_tts_chunk` | `first_token` -> `tts_chunk` |
| `tts_chunk_to_playback` | `tts_chunk` -> `first_audio` |
| `total` | `speech_end` -> `first_audio` |

Los tres primeros son los de la línea compacta del panel de voz
("habla->texto 0,4 s . texto->1er token 1,3 s . 1er token->audio 0,6 s"); el
resto, junto con los tiempos del servidor, salen al pasar el ratón por encima.

Un turno cuyo texto el usuario revisó y envió a mano (transcripción sin
auto-envío) **no se mide**: esa espera es suya, no del sistema. Con la voz de
respuesta apagada se guarda lo medido hasta el primer token, sin
`tts_chunk`/`first_audio`.

## Rutas

### `POST /api/voice/timeline`

Cuerpo: `{ session_id, turn_id, speech_end?, transcript_ready?, request_sent?,
first_token?, tts_chunk?, first_audio?, stt_server_ms?, tts_server_ms?,
server_ttft_ms? }`. Los campos desconocidos se ignoran; los valores no
numéricos, negativos, muy lejos de la hora actual (más de 7 días) o
duraciones de más de 10 minutos se descartan. Una segunda llamada para el
mismo `(session_id, turn_id)` **completa** el turno (une las marcas). `422` si
falta `session_id` o `turn_id`.

Respuesta: `{ "turn": { session_id, turn_id, recorded_at, marks, server, gaps, complete } }`.
`complete` es verdadero cuando existen `speech_end`, `transcript_ready`,
`first_token` y `first_audio`.

### `GET /api/voice/timeline?session_id=&limit=`

Turnos recientes (más nuevo primero, `limit` 1-500, 50 por defecto), con
`session_id` solo los de esa conversación, y su agregado:
`{ session_id, turns: [...], aggregate }`.

### `GET /api/voice/timeline/aggregate?session_id=&limit=`

Solo el resumen: `{ session_id, turns: N, gaps: { <hueco>: { n, p50_ms, p90_ms } } }`.
Percentiles por interpolación lineal sobre los turnos devueltos; `n` cuenta
solo los turnos donde ese hueco existe.

Cada usuario ve sus turnos (y los anotados sin dueño); el token interno del
agente ve todos, como en las demás rutas del PC de un solo usuario.

## Herramienta MCP `voice_turn_timeline`

En el servidor integrado `harness` (solo lectura). Argumentos: `session_id`
(opcional), `limit` (1-200, 20 por defecto). Devuelve `turns` (sin el dueño),
`aggregate` y una nota. Lee el mismo fichero SQLite que las rutas
(`DATA_DIR/voice_timeline.db`), porque corre en otro proceso.

## Privacidad y límites

- Solo se guardan identificadores e instantes, nunca texto de la conversación.
- Se conservan las últimas 2000 filas.
- Reloj del cliente: si el navegador y el servidor están en máquinas con la
  hora desfasada, solo se ven afectadas las marcas absolutas, no los huecos.
- La voz del navegador (`speechSynthesis`) no tiene paso de síntesis: la marca
  `tts_chunk` coincide con el arranque y `tts_server_ms` no existe.
- Un solo POST por turno, al primer audio (o al acabar la respuesta si no hubo
  audio). Cerrar el panel antes de eso pierde ese turno.
