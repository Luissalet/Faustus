# Receta «Revisar respuestas de candidaturas» — Lote F4

CONTRATO_CONECTORES.md, Lote F4 (Fase F, lado Faustus). Implementación:
`src/candidature_responses.py` (clasificador determinista + matching de
candidatura, sin FastAPI, sin LLM), `docs/recipes/review-candidature-responses.json`
(la receta, id `review-candidature-responses`, cargada por `src/recipes.py`),
`routes/calendar_routes.py` (idempotencia por `external_ref`, F4.1) y
`routes/email_routes.py` (filtros `since`/`until`/`unread_only` en `GET /list`,
F4.2). No hay ruta ni módulo nuevo que "ejecute" la receta: una receta es un
procedimiento inyectado en el prompt (`src/recipes.py::procedure_block`), no
un job programado — **la receta no se programa ni se ejecuta al instalarse**.

## Qué hace (y qué no hace)

La receta guía a un agente para revisar las respuestas de reclutadores a
candidaturas de empleo y, cuando corresponde, anotar una entrevista en el
calendario. Explícitamente **no**:

- marca correos como leídos (ni al listar ni al leer: ver F4.2 más abajo);
- crea candidaturas nuevas (esa es competencia de Jobhunter, fuera de este lote);
- invita a reclutadores ni envía ningún correo;
- inventa fecha, hora o zona horaria de una entrevista — un acuse de recibo
  (`ack`) nunca es aceptación ni entrevista, y una entrevista mencionada sin
  fecha/hora/zona resolubles se dejan sin evento, para revisión manual.

El cuerpo del correo es **dato**: se cita como contenido citado al
clasificar y al pasarlo a Jobhunter, nunca se interpreta como instrucción.

## F4.1 — Idempotencia de calendario (`external_ref`)

`POST /api/calendar/events` acepta ahora un campo opcional
`external_ref: str` (≤200 caracteres). Si ya existe un evento del mismo
owner con ese `external_ref`, la ruta devuelve el evento existente con
`{"ok": true, "uid": "...", "created": false}` (200) en vez de duplicarlo;
si lo crea, la respuesta añade `"created": true` (campo nuevo, aditivo — no
cambia el contrato previo de `{"ok": true, "uid": "..."}"`).

`GET /api/calendar/events?external_ref=...` busca directamente por esa
clave, sin necesitar `start`/`end` (siguen siendo obligatorios cuando no se
pasa `external_ref`, así que ninguna llamada existente se rompe). Cada
evento devuelto (por esta vía o por el listado normal) incluye ahora
`"external_ref"` en su dict.

Persistencia: columna `calendar_events.external_ref` (nullable, indexada),
migración idempotente `_migrate_add_calendar_external_ref` en
`core/database.py` (mismo patrón que las demás columnas de
`calendar_events`, registrada en `_formal_migration_steps()`).

**Límite documentado**: la comprobación de duplicado es *check-then-insert*,
no una restricción `UNIQUE` a nivel de base de datos (no hay columna
`owner` en `CalendarEvent`, solo en `CalendarCal`, así que una restricción
única compuesta no es directa). Dos POST **concurrentes** con el mismo
`external_ref` podrían ambos superar la comprobación antes de que el
primero confirme (`commit`) — la receta llama a esta ruta secuencialmente
por mensaje, que es el caso que este lote cierra; la protección ante
duplicado concurrente queda fuera de alcance.

## F4.2 — Filtros de correo (`GET /api/email/list`)

Parámetros opcionales nuevos, aditivos:

- `since` / `until`: ISO 8601 **con zona horaria explícita** (`Z` o
  `+HH:MM`); un valor sin zona o no parseable devuelve 400.
- `unread_only: bool` (default `false`).

Se aplican como un **post-filtro** sobre la página que ya haya respondido
cualquiera de las fuentes de `/list` (fixture de pruebas, índice local,
caché en memoria o IMAP real) — no se empujan al `SEARCH` de IMAP, porque
`SINCE`/`BEFORE` de IMAP son de granularidad de día, no de segundo, y cada
rama de `_list_emails_sync` construye su propio criterio de búsqueda. Por
eso `total` en la respuesta describe cuántos mensajes de **esta página**
cumplen el filtro, no un recuento de toda la carpeta — quien necesite
cubrir una ventana amplia debe pedir un `limit` generoso.

**Verificación de "no marcar leído"**: `GET /list` nunca ha tocado el flag
`\Seen` (ni en el índice local, ni en IMAP, ni en el fixture) — confirmado
leyendo `_list_emails_sync`, `_email_index_list` y `_fixture_email_list`
antes de tocar nada. La ruta que sí puede marcar como leído es
`GET /api/email/read/{uid}`, con `mark_seen: bool = Query(True)` (para que
abrir un correo en la UI lo marque leído, comportamiento existente que no
se toca). La receta debe pasar **`mark_seen=false` explícitamente** en toda
lectura de cuerpo que haga — el parámetro ya existía; no había que añadir
ninguno nuevo, solo usarlo así desde la receta (paso 1 de la receta lo dice
explícitamente).

## F4.3 — Clasificador y matching (`src/candidature_responses.py`)

### `classify(message) -> {kind, confidence, interview_at, tz, evidence}`

- `kind`: `ack | info_request | rejection | interview | offer | unknown`.
- Precedencia determinista: `rejection` y `offer` se comprueban primero
  (ambos correos suelen abrir con una frase de acuse — "gracias por tu
  candidatura" — antes del contenido real; comprobar `ack` primero los
  clasificaría mal). Luego una propuesta de entrevista **con fecha/hora/zona
  resolubles**; solo si eso falla se comprueba `ack` (para que un correo de
  acuse que de pasada menciona "ya te contactaremos para una entrevista"
  nunca se reporte como entrevista); si tampoco es un `ack` claro pero sí
  menciona entrevista, se devuelve `kind="interview"` con
  `confidence="low"` e `interview_at=None` (caso "la semana que viene").
- `interview_at`/`tz` solo se rellenan cuando el parser resuelve **las
  tres** cosas: fecha, hora y zona horaria explícita — "sin fecha/hora/zona
  determinadas → `interview_at=None` con `confidence="low"` (nunca
  inventar)". No hay dependencia de `dateparser` (no está en
  `requirements.txt` y la instrucción del lote es no añadirlo): fechas
  ES/EN por regex (`D de <mes> [de AAAA]`, `<Month> D[, AAAA]`, ISO,
  `DD/MM/AAAA`) + hora (24h o AM/PM) + zona (nombre IANA vía `zoneinfo`,
  abreviatura de tabla fija — `CEST`, `CET`, `UTC`… — u offset numérico
  explícito).
- `evidence`: fragmento (≤300 caracteres) del propio texto alrededor
  de la coincidencia que decidió la clasificación — nunca inventado.

### `match_job(message, jobs) -> {job_id, how, ambiguous}`

Precedencia: identificador exacto primero —
`external_id` → URL → pertenencia al mismo hilo (`message_id`/
`in_reply_to`/`references`) —, y solo si ninguno resuelve, texto de
empresa+puesto. Si el fallback por texto encuentra **dos o más**
candidatos de la misma empresa, devuelve `job_id=None` y
`ambiguous=[job_id, ...]` — nunca actualiza "otra candidatura del mismo
empleador" a ciegas.

## F4.4 — La receta (`review-candidature-responses`)

`inputs`: `period_start`, `period_end`, `timezone`, `accounts/folders`,
`jobhunter_context_id`. `tools`: `email.list`, `email.read`,
`jobhunter.list_jobs`, `jobhunter.get_application`,
`jobhunter.record_employer_response`, `calendar.create_event`. Los 8 pasos
(texto completo en el JSON) resumidos:

1. Listar correo del periodo sin marcar leído (`since`/`until`/`unread_only`;
   toda lectura de cuerpo con `mark_seen=false`).
2. Emparejar cada mensaje con una candidatura vía `match_job()` —
   ambiguo → se deja para revisión manual, nunca se adivina.
3. Clasificar con `classify()`.
4. Si `kind=interview` y `interview_at`/`tz` están resueltos: crear el
   evento con `external_ref = "jobhunter:{job_id}:{message_id}"`.
5. Llamar a `record_employer_response` con `calendarEventId` cuando el
   paso 4 creó/encontró uno.
6. **Recuperación**: si una ejecución anterior se cortó entre los pasos 4 y
   5, la siguiente ejecución encuentra el evento por `external_ref` (la
   propia idempotencia de `POST /events` ya lo garantiza) y solo repite el
   paso 5 — nunca crea un segundo evento ni una segunda candidatura.
7. Nunca crear candidatura, invitar reclutador ni enviar correo.
8. Resumen final: candidaturas actualizadas, entrevistas añadidas (con
   enlace al evento) y pendientes de revisión manual (ambiguas o con
   horario no resoluble).

### Contrato de `record_employer_response` (lo implementa Jobhunter)

```
record_employer_response({jobId, externalId, kind, evidence, receivedAt,
  interviewAt?, timezone?, calendarEventId?, notes?})
→ {job, applied: bool, reason?}
```

Idempotente por `externalId` (aquí, el `message_id`); un `ack` nunca cambia
el estado de la candidatura; un estado terminal (`rejected`/
`offer_received`) se preserva salvo evidencia posterior explícita. Faustus
no implementa esta función — solo la documenta y la usa en fixtures
(`tests/fixtures/candidature_mail/`, `_FakeJobhunter` en
`tests/test_candidature_responses.py`) para probar el flujo completo.

## Qué necesita credenciales reales y qué no

- **No** necesita nada real en este entorno: el clasificador y el matcher
  son puros (regex + `zoneinfo`, sin red); las pruebas usan fixtures de
  correo (`tests/fixtures/candidature_mail/*.json`) y un `_FakeJobhunter`
  en memoria; el calendario de las pruebas es el real de Faustus mismo,
  contra una base SQLite temporal (no una cuenta real).
- **En producción sí hace falta**: una cuenta de correo configurada
  (`routes/email_routes.py` ya soporta esto) y el conector Jobhunter
  conectado (Lote F1/F2 de este mismo contrato) con
  `list_jobs`/`get_application`/`record_employer_response` disponibles y
  permitidos para la tarea/sesión que ejecute la receta (F2, whitelist de
  conectores).

## Fixtures (`tests/fixtures/candidature_mail/`)

| Fichero | Caso |
|---|---|
| `ack.json` | Acuse de recibo puro — nunca entrevista ni aceptación. |
| `rejection.json` | Rechazo explícito. |
| `interview_tz.json` | Entrevista con fecha, hora y zona resolubles (`Europe/Madrid`, 10:30 CEST → `2026-09-20T10:30:00+02:00`). |
| `ambiguous_time.json` | "la semana que viene" — sin fecha/hora resoluble → sin evento. |
| `reschedule_1.json` / `reschedule_2.json` | Mismo hilo, segundo correo reprograma — `interview_at` distinto, `external_ref` distinto por `message_id`, el primer evento no se toca. |
| `ambiguous_job.json` | Mensaje de una empresa con dos puestos abiertos → `match_job` ambiguo. |
| `jobs.json` | Catálogo de candidaturas usado por los tests de `match_job` y el end-to-end. |

## Tests

- `tests/test_candidature_responses.py`: `classify()`/`match_job()` sobre
  cada fixture, un `_FakeJobhunter` idempotente y con estado terminal
  preservado, y dos pases end-to-end contra el calendario real de Faustus
  (temp SQLite) que confirman cero duplicados y el mismo resumen en la
  segunda pasada.
- `tests/test_calendar_external_ref.py`: idempotencia de `POST /events`,
  aislamiento por owner, `GET /events?external_ref=`.
- `tests/test_email_list_filters.py`: parseo estricto de `since`/`until`,
  filtro puro (`_apply_email_list_window`) y una integración a través del
  mecanismo de fixture de correo ya existente.
