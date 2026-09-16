# Política del scheduler — DST, misfire y deduplicación (A24)

Autor: U3 (`docs/spec/paridad/CONTRATO.md` — ola 2). Cubre exactamente lo
que `src/task_scheduler.py` hace hoy; no es un diseño aspiracional.

## 1. Próximo disparo en la zona del ejercicio

`compute_next_run(schedule, scheduled_time, ..., tz_name=<IANA>,
dst_ambiguity_policy=...)` interpreta `scheduled_time`/`scheduled_day` como
hora LOCAL de `tz_name` (`zoneinfo.ZoneInfo`) y devuelve el instante en UTC
naive que la base de datos guarda. Sin `tz_name`, el comportamiento previo
(hora naive-UTC) queda intacto — ningún task existente cambia de hora al
desplegar esto. `tz_name` se resuelve por tarea en
`_resolve_task_timezone` (campo `timezone` propio, o el de su `CrewMember`).

### 1.1 Los dos huecos del DST

Dos veces al año la conversión hora-local → instante no es 1 a 1
(`src/task_scheduler.py::_dst_resolve`, PEP 495 / `datetime.fold`):

- **Hora inexistente** (salto adelante — `Europe/Madrid` 2026-03-29,
  02:00→03:00 CEST): `02:30` nunca ocurre. La política resuelve al primer
  instante VÁLIDO posterior — `02:30` extrapolado a través del salto cae en
  `03:30 CEST`, que es la lectura honesta más temprana de "cuándo llegó
  02:30" — y dispara una única vez.
- **Hora repetida** (salto atrás — `Europe/Madrid` 2026-10-25, 03:00→02:00
  CET): `02:30` ocurre dos veces, una hora exacta de diferencia. La política
  por defecto dispara en la PRIMERA ocurrencia (`fold=0`, todavía en CEST) y
  nunca en la segunda salvo que se declare `run_all`.

`dst_ambiguity_policy` (por tarea, `set_task_policy`/`get_task_policy`),
una de:

| Política | Hueco inexistente | Hueco repetido |
|---|---|---|
| `run_once` (default) | dispara una vez, en el primer instante válido tras el salto | dispara una vez, en la PRIMERA ocurrencia (`fold=0`) |
| `skip` | esa ocurrencia se abandona; avanza al siguiente ciclo real | esa ocurrencia se abandona; avanza al siguiente ciclo real |
| `run_all` | igual que `run_once` (solo hay un instante posible) | dispara las DOS ocurrencias — la segunda (`fold=1`, una hora después) se encola como el único disparo pendiente que `TaskScheduler._next_moment_after` drena antes de calcular un ciclo nuevo (`_maybe_queue_run_all_followup`) |

`run_all` es la ÚNICA forma de disparar dos veces la misma ocurrencia
ambigua del calendario, y solo porque se declaró así explícitamente.
Ninguna otra política duplica silenciosamente.

## 2. Misfire — el scheduler estuvo parado

En `TaskScheduler.start()`, cualquier tarea activa cuyo `next_run` ya quedó
en el pasado (el proceso estuvo caído, o el equipo apagado) se resuelve
según `misfire_policy` (por tarea, default `fire_once`):

| Política | Efecto |
|---|---|
| `fire_once` (= `fire_immediately`, alias histórico — mismo valor, nunca normalizado) | se dispara UNA vez, poco después del arranque (`next_run = ahora + 60s`), sin importar cuántas ocurrencias se perdieron |
| `skip` | TODAS las ocurrencias perdidas se abandonan; `next_run` avanza directamente al siguiente ciclo FUTURO — una tarea debida 3 veces en un apagón de 3 horas no corre 3 veces, ni tarde una vez: corre 0 veces para ese hueco |
| `catch_up_max:k` (`k` entero positivo) | se ejecutan hasta `k` de las ocurrencias perdidas — las `k` MÁS ANTIGUAS, cada una con su instante programado ORIGINAL (y por tanto su propio `fire_key`, ver §3) — el resto se abandona igual que `skip` |

`catch_up_max:k` enumera las ocurrencias perdidas con
`_enumerate_missed_occurrences` (repite `compute_next_run` respetando la
misma `dst_ambiguity_policy` de la tarea) y encola las que exceden la
primera en `catchup_pending_json` (tabla `task_policies`), que
`TaskScheduler._next_moment_after` drena una a una — ANTES de un posible
seguimiento `run_all` pendiente, porque una deuda de misfire es más antigua
que la ambigüedad del ciclo que se está calculando ahora.

## 3. Deduplicación — `fire_key`

Cada ocurrencia de una tarea tiene un `fire_key = task_id + scheduled_at`
(`occurrence_key`/`fire_key`, alias del mismo nombre — la vocabulario del
contrato y el preexistente conviven sin normalizarse el uno al otro).

Dos mecanismos, en dos puntos distintos, y a propósito NO en el mismo:

- **`_claim_due_task`** (la fila `scheduled_tasks.lease_owner` con
  `UPDATE ... WHERE`) sigue siendo lo que decide quién dispatchea esta
  ocurrencia AHORA MISMO — sin cambios de A24. Un reintento LEGÍTIMO tras
  recuperar un lease expirado (`_recover_expired_leases`) reclama el MISMO
  `due_at`/`lease_key` a propósito: es el intento 2 de la misma ocurrencia,
  no un duplicado, y debe poder ganar la reclamación.
- **`claim_fire_key`/`_claim_fire_key_literal`** (nuevo, A24) es la
  restricción única real: una tabla `scheduler_fire_log` con `fire_key` como
  `PRIMARY KEY`. Se reclama en `_execute_task_locked`, justo antes de
  producir el efecto (junto al chequeo de fencing `A06`
  `_still_owner_or_fenced`), usando `task.lease_key` (que YA es el
  `fire_key` de esta ocurrencia, gracias al `COALESCE` de
  `_claim_due_task`). Ahí — y no en el claim del lease — es donde "intento 2
  de una ocurrencia cuyo intento 1 nunca llegó tan lejos" (reclama, sigue) y
  "esta ocurrencia ya produjo un resultado" (reclama, falla, se marca
  `fenced`) son distinguibles.

Una versión anterior de A24 reclamaba `fire_key` dentro de
`_claim_due_task` mismo; rompía exactamente el reintento legítimo descrito
arriba (`tests/test_scheduler_lease_claim.py::
test_a_recovered_lease_keeps_the_occurrence_key_and_counts_the_attempt`) —
ver el comentario en `_claim_due_task` y
`tests/acceptance/test_a24_scheduler_dst_misfire.py::
test_a_retry_after_lease_recovery_still_executes_once_not_zero_times`, que
guarda la regresión.

Al ser una tabla SQLite propia (con `PRIMARY KEY`), dos hilos/procesos que
reclaman el mismo `fire_key` de verdad en paralelo (no en secuencia) nunca
pueden ganar los dos — es la base de datos la que lo impide, no un `if` en
Python; ver
`tests/acceptance/test_a24_scheduler_dst_misfire.py::
test_two_concurrent_claims_of_the_same_fire_key_dedupe` (dos hilos reales,
liberados por un `threading.Barrier`).

## 4. Dónde vive cada cosa

Todo en `src/task_scheduler.py`, sin migración de `core/database.py`
(ningún fichero ajeno tocado):

- `task_policies` (SQLite propio, `_policy_connect`) — `dst_ambiguity_policy`,
  `misfire_policy`, `run_all_pending_at`, `catchup_pending_json` por tarea.
- `scheduler_fire_log` (misma base) — `fire_key` PRIMARY KEY, dedup real.

## 5. Cobertura de test

- `tests/acceptance/test_a24_scheduler_dst_misfire.py` — el caso A24 en
  sí: los dos huecos de DST de `Europe/Madrid` 2026, `catch_up_max:k` con
  reloj congelado, `fire_key`/deduplicación concurrente, y la interacción
  reintento-de-lease-vs-dedup.
- `tests/qa/test_qa_43_cambio_horario.py`,
  `tests/test_auto_02_task_dst_misfire.py` — cobertura preexistente y
  exhaustiva de `dst_ambiguity_policy`/`misfire_policy` (skip/fire_immediately),
  no reescrita por este lote.
