# H08 parcial: conservar replay por run

Implementado localmente el 30/09/2026. Corrige pérdida de evidencia al reemplazar
una ejecución. No cierra el ledger causal independiente de H08.

## Fuentes y matiz visitado

[Plan inspeccionado](CODEX_H08_PER_RUN_PLAN.md),
[análisis H08](CODEX_HARNESS_ANALISIS_2026-09-29.md),
[recorder original fijado](https://github.com/openai/codex/blob/b1e72963c3b71a9265a551e54beff078384efed9/codex-rs/rollout/src/recorder.rs),
[recuperación original fijada](https://github.com/openai/codex/blob/b1e72963c3b71a9265a551e54beff078384efed9/codex-rs/core/src/session/daemon_recovery.rs).
No se repitió revisión upstream, no se enviaron peticiones externas ni se usaron
datos reales. El matiz adaptado es separar identidad de ejecución y persistencia
de la vista de sesión: una ejecución nueva ya no trunca el replay anterior.

## Comportamiento

- Cada run crea exclusivamente `run-<sha256 sesión>-<inicio microsegundos>-<UUID>.jsonl`.
  El timestamp tiene anchura fija; permite ordenar sin abrir cuerpos históricos.
  La creación exclusiva rechaza colisiones sin truncar. El hash evita colisiones
  del saneamiento legacy, pero no anonimiza ni sustituye autorización.
- El run reemplazado conserva su escritor hasta cierre; cancelación previa a la
  primera iteración también termina su log. El nuevo run recibe otra ruta.
- Consulta de sesión selecciona el último archivo y valida su header. Sigue
  aceptando el archivo legacy, sin migrarlo. Los call_id reutilizados no se
  mezclan entre los replays de runs distintos. No se añade API histórica.
- Recuperación ordenada conserva los runs interrumpidos de una misma sesión,
  agrega advertencias de efectos inciertos con run_id y conserva las advertencias
  guardadas en un reinicio anterior. No reejecuta herramientas. Evita duplicar el
  mensaje si ya se guardó antes de escribir el terminal del log.
- `SessionManager.save_sessions()` es un no-op y `Session.add_message()` delega
  mediante singleton a persistencia que captura errores. Recuperación usa ahora
  `persist_recovered_message`: transacción SQLite/SQLAlchemy por sesión y run_id,
  confirmación de commit antes de marcar interrupted, actualización de memoria
  tras commit y reintento sin duplicar. El fallo conserva el log running. Managers
  antiguos/doubles mantienen compatibilidad mediante save_sessions; esa ruta no
  es la garantía durable de producción.
- Error al consultar sesiones conserva el archivo y permite continuar con otras.
  Una sesión inexistente no se reconstruye. Persistencia previa fsync de H03 se
  mantiene y sus pruebas vuelven a ejecutarse.
- Retención de arranque sigue en 48 horas por defecto para terminales. Limpieza
  manual sigue en 60 días por defecto y lleva log y sidecar juntos a papelera.
  Estados ilegibles o inconclusos no demuestran terminación y se conservan.
- Borrado explícito, múltiple, total, Incognito y auto-sort purgan logs de sesión,
  sus sidecars y escritores antiguos. La purga reconoce nombres estrictos incluso
  con header truncado, pero respeta headers que indican otra sesión. Los logs
  legacy cuyo header identifica otra sesión tampoco se leen ni se borran.

## Coste y límites

Resolver latest enumera nombres de la carpeta (O(n), sin índice persistente),
ordena los de esa sesión y abre solo el candidato más reciente. Un header
dañado devuelve ausencia de evidencia, nunca datos de un run anterior con el
mismo call_id. Legacy solo se usa cuando no existe candidato nuevo. La consulta global agrupa nombres
por hash antes de validar headers, mantiene el límite previo de 200 candidatos
y no promete búsqueda histórica completa. Archivos legacy requieren header para
identificarlos. Leer el replay elegido sigue abriendo ese archivo y puede sufrir
el antivirus de Windows; no se midió latencia real de antivirus/modelos.

Los eventos aún se compactan para replay: no existe ledger causal independiente,
índice histórico transaccional ni idempotencia externa. El upsert de recuperación
asume arranque secuencial; no incorpora constraint único JSON para múltiples
procesos recuperando simultáneamente. La purga cubre las rutas
de sesión inspeccionadas; código externo que llame directamente a otro gestor
de persistencia debe invocar también el hook. No elimina copias de seguridad ni
papeleras previas. Conservar más runs aumenta espacio hasta aplicar retención.
El nombre ordena por reloj de inicio; empates desempatan por UUID, no establecen
causalidad. Un cambio manual del reloj puede alterar el orden de latest.

## Validación local

Fixtures temporales: archivos exclusivos, legacy, colisión saneada, latest sin
abrir 30 headers antiguos, header truncado, sidecar huérfano, cierre/reemplazo,
dos runs con el mismo call_id, dos reinicios, fallo de consulta, purga, retención
de 48 horas, log activo mayor de 64 KiB sin estado en cola y terminal/sidecar.
SQLite temporal prueba fallo real de commit, mismo/nuevo gestor y caída entre
commit del mensaje y terminal del log; la fila mantiene el mismo ID sin duplicarse.
La suite incluye H03 durable, H04 partial/unknown, H08 identidad, aceptación A05,
colas, borrado de sesiones, observabilidad y limpieza. Resultado: **114 pruebas pasadas (11,36 s)**. `git diff --check` limpio.
Comando: `venv/Scripts/python.exe -m pytest tests/test_codex_h08_per_run.py
tests/test_agent_runs_queue_persist.py tests/test_agent_run_log_replacement.py
tests/test_session_delete_stops_run.py tests/test_codex_h03_durable_email_intent.py
tests/test_codex_h04_effect_results.py tests/test_codex_h08_replay_call_identity.py
tests/acceptance/test_a05_unknown_effect.py tests/test_p1_ops_ops07_cleanup_hygiene.py
tests/test_cleanup_service_utcnow.py tests/test_session_actions_cleanup.py
tests/test_obs_agent_runs.py tests/test_session_manager_cleanup.py -q`.
