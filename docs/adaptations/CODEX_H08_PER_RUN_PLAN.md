# H08: plan de conservación por run, previo a implementación

**Estado: implementado parcialmente y en validación local.**
Resultado y límites: [H08 conservación por run](CODEX_H08_PER_RUN.md). Inspección local 29–30/09/2026.
Separado de `b7089ff5`, que corrige únicamente la compactación por `call_id`.
Sirve para continuar sin repetir la lectura del código ni atribuir al sistema
garantías que todavía no tiene.

Fuentes: [análisis Codex H08 y 27.3/27.8](CODEX_HARNESS_ANALISIS_2026-09-29.md),
[recorder original fijado](https://github.com/openai/codex/blob/b1e72963c3b71a9265a551e54beff078384efed9/codex-rs/rollout/src/recorder.rs),
[recuperación original fijada](https://github.com/openai/codex/blob/b1e72963c3b71a9265a551e54beff078384efed9/codex-rs/core/src/session/daemon_recovery.rs).
No se volvió a consultar upstream ni se ejecutaron servicios externos.

## Evidencia local que motiva el cambio

- `_RunLog.__init__` en `src/agent_runs.py` calcula ruta por sesión y abre con
  `w`: un run nuevo trunca el replay anterior. `orphan()` evita que el escritor
  viejo corrompa el nuevo archivo, pero no conserva su contenido.
- `_trace_log_paths(session_id)` selecciona únicamente la ruta de esa sesión;
  la búsqueda global inspecciona hasta 200 JSONL, por fecha de modificación.
- `_read_log` conserva el header `run_id`/`session_id`; los slots de replay se
  reconstruyen por `seq`. `_peek_status` y el sidecar evitan leer logs grandes.
- El sidecar es `<log>.status`, con estado y tamaño. No constituye un índice
  de identidad ni garantiza que un run haya cumplido su objetivo.
- `recover_interrupted_runs` escanea JSONL de la raíz, recupera headers en
  `running`, guarda mensajes y usa `_INTERRUPTED[session_id]`. Actualmente ese
  índice puede representar solo un run interrumpido por sesión.
- La poda de arranque elimina logs terminales de más de 48 horas por defecto.
  `cleanup_service.runs_log_retention` usa otra operación, papelera y 60 días;
  mueve solo el JSONL. No deben confundirse ambas políticas.
- `routes/session_routes._stop_runs_for_deleted_sessions` llama a
  `agent_runs.stop_for_session`. Este detiene/cierra el run, pero no ofrece una
  purga completa de logs históricos por sesión.
- `support_bundle.recent_events` y `crash_recovery` también leen JSONL de la
  raíz. Añadir una subcarpeta sin revisar esos lectores ocultaría evidencia.

## Entregable mínimo propuesto

Conservar un archivo por run en la misma carpeta `DATA_DIR/runs`. Mantener el
formato JSONL actual y los headers, sin duplicar toda la información en otra base.
El nombre nuevo combinará un hash estable del identificador de sesión con el
`run_id` generado por el runtime. El hash evita colisiones por saneamiento o
truncado de nombres; no se presenta como anonimización ni control de acceso.

1. Constructor de ruta nuevo, separado de la función de ruta legacy. Validar
   `run_id` contra el formato generado internamente y contener la ruta en runs.
   Abrir el nuevo archivo con creación exclusiva. Si ya existe o el disco falla,
   no reutilizar ni truncar otro archivo; registrar indisponibilidad del log.
2. Headers conservan sesión y run exactos. No añadir cuerpo de correo,
   argumentos completos ni otros datos al header. H03 debe continuar negando
   un envío trazado si no obtiene su intención durable.
3. Un run conserva su propio escritor hasta su terminación. El reemplazo de
   una sesión cancela el run anterior como hoy, pero ya no necesita privarlo
   del log para evitar escritura sobre la ruta del run nuevo. Su estado final
   se registra en su archivo, con su propio sidecar. Revisar todos los usos de
   `orphan()` antes de retirar exclusivamente la llamada que dependía de ruta
   compartida; no eliminar una protección de cierre sin equivalente probado.
4. Lectores aceptan ambos nombres. No renombrar ni reescribir los logs antiguos
   al arrancar. Los headers mandan sobre los nombres; el fallback de nombre de
   sesión solo corresponde al formato legacy, nunca al nombre hash nuevo.
5. Mantener esquema público actual por defecto: consultar sesión significa
   run vivo o último run de esa sesión. No devolver sin más la unión de todos
   sus archivos históricos, ni interpretar un hash como autorización.

Esto conserva evidencia entre runs; **todavía no convierte el replay en ledger
causal independiente**, porque sus ticks siguen compactándose por presentación.

## Identidad y consulta: evitar mezclar call_id reutilizados

Los IDs de proveedor y los fallbacks como `call_1_0` pueden repetirse en runs
distintos. La identidad de un evento es como mínimo `(run_id, call_id)`, junto
al ámbito de sesión/owner que autoriza leerlo.

- Default de `trace_for_call(call_id, session_id=...)`: elegir un solo run,
  igual que la semántica actual por sesión, no fusionar coincidencias viejas.
- Consulta histórica explícita: añadir `run_id` solo con verificación de que
  pertenece a esa sesión autorizada. Nunca filtrar por coincidencia de prefijo
  de filename si sus headers no corresponden al ámbito solicitado.
- Recibos y artefactos se consultan hoy por `call_id` a solas. Hasta que sus
  metadatos permitan la identidad compuesta, no asociarlos como evidencia
  inequívoca a un run histórico. Conservar el resultado de eventos y señalar
  la limitación, o aplazar la exposición histórica completa.
- Búsqueda global: seguir acotada; no degradar una consulta reciente a escanear
  todos los años de historial. Si quedan archivos fuera del límite, no afirmar
  que un evento nunca existió.

Esta es una dependencia real para exponer trazas históricas correctas. Guardar
archivos adicionales no autoriza a enriquecer silenciosamente una consulta con
evidencia de otro run.

## Recuperación compatible

Agrupar archivos en `running` por sesión, ordenar por timestamp del header y
usar `run_id` como desempate estable. Guardar cada parcial como máximo una vez,
con el `run_id` correspondiente; los terminales no se vuelven a importar.

Resolver expresamente la representación de varios runs en `_INTERRUPTED`:
para el primer entregable puede conservar la ficha de sesión agregando una
lista de runs e incertidumbres, manteniendo campos legacy del más reciente.
No dejar que la última iteración del escaneo borre advertencias de efectos
pendientes de un run anterior. La proyección del próximo turno deberá conservar
todas esas advertencias; esto requiere prueba, no solo cambiar un diccionario.

Un run anterior cancelado normalmente no debe aparecer como crash. Los estados
cancelado/terminado no significan que todo efecto se haya revertido. El tratamiento
de incertidumbre en runs terminales es una brecha ya registrada de H04; si el
nuevo recorrido necesita esa garantía, incorporarla como aceptación separada,
no afirmar que la retención de archivos la resuelve.

## Retención y privacidad

- Mantener 48 horas como default de poda de logs terminales, sin ampliarlo por
  introducir históricos. No podar runs vivos para cumplir ese límite temporal.
- Eliminar/mover JSONL y sidecar como pareja; un sidecar huérfano no debe indicar
  estado de un archivo posterior ni permanecer indefinidamente. Revisar la
  operación de papelera de 60 días, cuya semántica distinta se conserva.
- Más runs conservados significa más contenido sensible retenido que el
  comportamiento actual de sobrescritura. No añadir campos sensibles ni ampliar
  los permisos de las rutas operativas, exportaciones o soporte.
- Antes de promover, implementar purga por sesión en el camino de borrado ya
  autorizado: detener escritor, impedir escrituras posteriores y purgar nuevos
  logs, legacy y sidecars de esa sesión. Comprobar que no se eliminen logs de
  otra sesión con identificadores que colisionaban tras saneamiento.
- No recuperar un chat borrado por encontrar un archivo viejo. La sesión debe
  seguir existiendo y conservar su autorización; el log no puede recrearla.
- Una copia en papelera o en un bundle de soporte no equivale a borrado físico.
  Respetar y describir las políticas existentes; no prometer borrado seguro de
  discos o copias externas.

## Pruebas de aceptación antes del commit funcional

| Caso | Evidencia exigida |
|---|---|
| Dos runs sucesivos de una sesión | Dos archivos distintos; bytes y header del primero intactos |
| Reemplazo mientras el anterior escribe | Cierre tardío solo afecta al archivo anterior; nuevo run sigue `running` |
| Misma call_id en dos runs | Consulta default no mezcla; selección explícita devuelve solo el run pedido |
| Reinicio con dos runs interrumpidos | Orden estable, parciales sin duplicar y avisos de ambos preservados |
| Log legacy | Lectura, recuperación, estado y poda siguen funcionando |
| Sesiones con nombres que saneaban igual | Rutas separadas y ninguna lectura/purga cruzada |
| Sidecar stale o ausente | Se verifica el log; no se pierde recuperación por metadata obsoleta |
| Terminal >48h y terminal reciente | Se poda solo el antiguo con su sidecar |
| Run vivo >48h | No se poda |
| Borrado de sesión activa | Escritor detenido, logs y sidecars purgados, nada reaparece al reiniciar |
| Error de disco/colisión de ruta | Ningún archivo viejo truncado; H03 deniega envío trazado sin intención |
| API de traza/soporte | Mismos controles de owner/admin, sin expansión accidental de contenido |

Usar directorios temporales y receptores sintéticos; no requiere red, SMTP ni
modelos. Ampliar las suites existentes de agent_runs, replacement, H03/H04,
observabilidad, borrado de sesiones, cleanup, soporte y crash recovery solo donde
el cambio afecte su contrato.

## Secuencia sugerida y dependencias

1. Implementar identidad/rutas/lectores legacy-latest y cerrar reemplazo, retención
   y borrado de sesión en un piloto local. No activar archivos nuevos sin cerrar
   el recorrido de recuperación/purga, pues aumentaría retención sin control.
2. Implementar recuperación de múltiples runs con avisos acumulados, antes de
   declarar compatible la conservación por run. Puede ser parte del mismo
   entregable si no hay una forma segura de desplegarlo por separado.
3. Exponer consulta histórica por identidad compuesta después de revisar los
   índices de recibos/artefactos y autorización de las rutas consumidoras.
4. Evaluar ledger causal independiente del replay como paso posterior: almacenar
   transiciones irreversibles con identidad propia y proyectar UI desde ellas.

No hay bloqueo por infraestructura externa. Las dependencias son contratos
locales y pruebas de recuperación, identidad y privacidad. El plan no autoriza
borrar datos reales para validarlo ni marca H08 completo.
