# Voz en streaming (`/api/voice`)

Sesiones de voz efímeras ligadas a un chat (SID). La conversación sigue siendo el chat de siempre: mismo
historial, proyecto, herramientas, memoria, opciones y modelo global. La sesión solo transporta audio,
transcripciones y voz alrededor de ese chat.

- **Entrada**: PCM16 mono a 16 kHz por WebSocket, con *pre-roll* del cliente (la primera sílaba de una
  interrupción no se pierde), o un clip entero (WAV/WebM/Ogg/MP4/MP3 o PCM16 crudo) por HTTP o MCP.
  Transcripciones parciales mientras se habla y una final por intervención, con los mismos filtros que el
  panel de Studio (alucinaciones de silencio, frases de parada, palabra de activación opcional).
- **Envío**: en modo `server` el texto final se envía **una sola vez** a `/api/chat_stream`, llamado en el
  propio proceso como el dueño del chat (la misma llamada nativa que usan los grupos). En modo `client` lo
  envía el cliente (Studio) y la sesión solo sigue el turno.
- **Salida**: se sigue cualquier turno activo del SID (de Studio, de un Hoard, de MCP o de la propia sesión)
  con `agent_runs.subscribe`. Solo se habla el texto visible de la respuesta (nada de razonamiento, código,
  tablas ni URLs), frase a frase, sintetizado por adelantado: la frase siguiente está lista antes de que
  termine la actual. Si la respuesta se reescribe (pasada editorial, recuperación) no se repite ninguna frase.
- **Interrumpir** vacía la cola y deja de hablar esa respuesta; **no** cancela el trabajo del agente.
  `cancel_turn` es la acción separada y explícita que detiene el turno.

En modo automático, `language_hint: "es" | "en"` aporta el idioma inicial de la
conversación sin fijarlo. Las siguientes intervenciones usan el idioma de la
última intervención aceptada; el silencio y las frases ignoradas no lo cambian.
El proveedor local solo lo considera al volver a decodificar audio
muy breve cuya detección sea incierta; una detección clara o `language` explícito
prevalecen. El mismo hint opcional se admite en `/api/stt/transcribe` como campo
de formulario. El modelo configurado se conserva y la transcripción no fuerza
la aparición del nombre del asistente.

Solo al exigir la palabra de activación, se admiten además las variantes de
reconocimiento `Fosters, …` y `Foustas, …` con coma o dos puntos y en posición de
vocativo directo, también tras un saludo. Una mención como «Fosters makes beer»
o «I read Fosters, yesterday» no activa el asistente. No se reemplazan esos
nombres en el dictado normal; es tolerancia textual de activación, no un
detector acústico ni una garantía ante todos los vocativos ambiguos.

El selector de voz aprovecha también las respuestas cortas en inglés y español.
Los nombres, números o empates sin indicios suficientes conservan el idioma de
entrada. Esto no garantiza identificar el idioma de un homófono aislado.

El filtro de eco de Studio exige ahora un fragmento literal normalizado de tres
o más palabras del texto reproducido, dentro de su ventana de 1,5 segundos.
Cambios de números, negación u orden no se descartan por similitud de vocabulario.
Los asentimientos cortos se conservan. Es una heurística textual: no sustituye
la cancelación acústica de eco del navegador.

Descubrir capacidades no carga modelos. Las sesiones viven en memoria (máx. 4 por usuario, 15 min sin
actividad) y el audio no se guarda en disco. Un cambio de chat abre otra sesión; reconectar el WebSocket con
`after=<seq>` retoma los eventos sin mezclar nada.

## REST

| Método | Ruta | Qué hace |
|---|---|---|
| GET | `/api/voice/capabilities` | STT/TTS configurados y lo que admite el streaming (`streaming.available`, `speech_output`, `input_formats`, `pcm_sample_rate`, `send_modes`, `interrupt_cancels_turn: false`). |
| POST | `/api/voice/sessions` | `{session_id, language: auto|es|en, language_hint: es|en, read_aloud, partials, send_mode: server|client, wake_word, follow_current}` → estado + `token` + `stream_url`. |
| GET | `/api/voice/sessions` | Sesiones abiertas del usuario. |
| GET | `/api/voice/sessions/{id}` | Estado, últimas métricas por intervención. |
| GET | `/api/voice/sessions/{id}/events?after=N&timeout=S&audio=bool` | Eventos posteriores al cursor (espera larga ≤ 30 s); `audio=true` añade `audio_b64`. |
| POST | `/api/voice/sessions/{id}/utterance` | Un clip (cuerpo crudo o multipart `file`; `sample_rate`, `speech_end_at`): transcribe, filtra y envía. |
| POST | `/api/voice/sessions/{id}/say` | `{text}`: envía texto por la sesión; la respuesta se habla igual. |
| POST | `/api/voice/sessions/{id}/interrupt` | Deja de hablar (el turno sigue). |
| POST | `/api/voice/sessions/{id}/cancel_turn` | Detiene el turno del agente. |
| POST | `/api/voice/sessions/{id}/played` | `{run_id, at}`: el cliente empezó a reproducir (métrica de primer audio oído). |
| PATCH | `/api/voice/sessions/{id}` | `{read_aloud, partials, language, language_hint, wake_word}`; `language_hint: ""` borra la preferencia inicial. |
| DELETE | `/api/voice/sessions/{id}` | Cierra la sesión (el turno sigue). |

Con token API hace falta el alcance `sessions` (el mismo que `/api/chat_stream`).

## WebSocket `/api/voice/sessions/{id}/stream?token=…&after=N`

Cliente → servidor: `{"type":"utterance_start","sample_rate":16000,"preroll_ms":N}`, tramas binarias PCM16,
`{"type":"utterance_end","speech_end_at":<epoch s>}`, `utterance_cancel`, `interrupt`, `cancel_turn`,
`{"type":"say","text":…}`, `{"type":"played","run_id":…}`, `{"type":"options",…}`, `ping`, `stop`.

Servidor → cliente: cada evento como JSON (`seq`, `type`, …). Un evento `audio` (`run_id`, `text`,
`language`, `mime`, `synth_ms`, `audio_bytes`) va seguido de una trama binaria con el audio.

Tipos de evento: `ready`, `transcript` (`final`, `text`, `raw`, `verdict`: ok/empty/hallucination/
stop_phrase/no_wake_word, `language`, `stt_ms`), `sent` (`run_id`, `client_message_id`), `turn`
(`state`: running/done/failed/question, `text`), `audio`, `speech_end`, `interrupted`, `turn_cancel`,
`error` (`stage`: input/stt/turn/tts), `closed`, `keepalive`.

## Métricas

Cada intervención guarda (en memoria, en `metrics` del estado): `speech_end_to_transcript_ms`,
`transcript_to_request_ms`, `request_to_first_text_ms`, `first_text_to_first_audio_ms`,
`speech_end_to_first_audio_ready_ms` y, cuando el cliente avisa, `speech_end_to_first_audio_played_ms`.
El panel de Studio sigue publicando su cronología (`/api/voice-timeline`) igual que antes.

## MCP

Servidor integrado `voice` (`mcp_servers/voice_server.py`):

- `voice_capabilities`
- `voice_session` con `action`: `start`, `status`, `events`, `utterance` (`audio_path`/`audio_b64`),
  `say`, `interrupt`, `cancel_turn`, `stop`, `list`. Con `wait=true`, `utterance` y `say` esperan al final
  de la voz de la respuesta y pueden guardar cada frase hablada en `save_audio_dir`.

`start` admite `language_hint: "es" | "en"` y `wake_word: true` opcionales, con
los mismos efectos que en REST; sin ellos se conservan los valores anteriores.

## Studio

El panel de voz usa esta sesión cuando el servidor transcribe (no reconocimiento del navegador) y la opción
«Voz en streaming» está activa (por defecto). Studio sigue enviando el mensaje él mismo (`send_mode=client`)
para que su vista del chat sea la de siempre; el servidor habla el turno y el panel reproduce lo que llega.
Desactivar la opción vuelve al camino anterior (grabar, transcribir y sintetizar cada frase a petición).

## Comportamiento fino

- **Primera frase antes**: el primer fragmento hablado puede cortarse en una coma, punto y coma o dos puntos
  a partir de ~40 caracteres, para que la voz empiece sin esperar a la frase entera. Los emojis y
  pictogramas no se leen.
- **Parciales que no estorban**: si el reconocedor tarda más de ~0,7 s por segundo de audio (Whisper en
  CPU), las transcripciones parciales se apagan solas para no competir con la final.
- **Transcripción especulativa**: tras ~350 ms de pausa el servidor transcribe lo que tiene; si se vuelve a
  hablar, esa conjetura se retira y no cuesta nada más.
- **Pre-roll desde el primer momento**: Studio mantiene el anillo de pre-roll lleno en cuanto el micro y la
  sesión están abiertos, así que incluso la primera interrupción conserva sus primeras sílabas. El audio se
  convierte a 16 kHz con el remuestreador del navegador cuando éste lo permite.
- **Sesión huérfana**: si el WebSocket se cae y nadie se reconecta en 90 s, la sesión se cierra sola (el
  turno del chat sigue). Cerrar el panel la cierra al momento.
- **Métricas en modo `client`**: los tiempos de una intervención quedan asociados al turno que su
  transcripción inicia aunque Studio lo envíe antes de que el servidor termine de anotarla; una transcripción
  que el cliente descarta no presta sus tiempos a un turno posterior (caducan a los 20 s).
