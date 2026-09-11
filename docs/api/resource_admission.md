# Admisión por pools de recursos (ADP-32) — `src/resource_admission.py`

Pools explícitos de recursos (GPU/CPU/remoto) con un tope de concurrencia
configurado, y una única ruta de estado: `GET /api/ops/admission` (admin).
**No sustituye ni toca** el candado global de `src/llm_core.py`
(`_LOCAL_MODEL_LOCK`) — ver la sección "Cómo se conectaría" más abajo. Nada
está cableado todavía; este módulo existe para que, cuando alguien mida y
decida que hace falta más concurrencia real, cablearla sea llamar a
`define_pool`/`acquire`, no rediseñar.

## Medición: pendiente, declarada

La ficha pide medir primero si la serialización actual (un `asyncio.Lock`
único para todo tráfico local) limita tareas reales. Esa medición necesita
telemetría de producción (¿con qué frecuencia coinciden dos llamadas a
endpoints *de verdad* independientes dentro de la misma ventana?) que este
lote, sin acceso a producción, no puede recoger. Se deja como `unknown`
explícito, no como supuesto. Lo que sí se hace, permitido por la propia
ficha con independencia de esa medición, es dejar lista la *forma* que
tendrían los pools.

## Modelo

```
Pool{pool_id, kind ∈ {gpu, cpu, remote}, endpoints: [url normalizada, …], max_concurrent}
```

- `normalize_endpoint(url)` reduce cualquier URL a `scheme://host:port`
  (reutiliza `endpoint_resolver.normalize_base` para quitar el sufijo de
  ruta — `/v1/chat/completions`, `/api/generate`, … — y luego se queda solo
  con la identidad de red, la misma granularidad que
  `vram_admission._reservation_key` ya usa para reservar VRAM).
- `define_pool(pool_id, kind, endpoints, max_concurrent=None)` normaliza y
  deduplica: dos URLs del mismo servidor terminan siendo **un** endpoint en
  la lista, nunca dos. Si un endpoint normalizado ya pertenece a OTRO pool,
  `PoolError` — dos pools sobre el mismo servidor físico duplicarían la
  capacidad de admisión sobre hardware que solo tiene una.
- `max_concurrent` por defecto es `1` (ruta serial segura) si no se
  configura explícitamente.

## Admisión

- `acquire(pool_id, *, priority="background"|"foreground", owner="", timeout=None) -> Lease`
  espera un hueco y lo toma. `priority="foreground"` adelanta a cualquier
  `"background"` que siga en cola — la misma distinción que
  `llm_core._gate_workload` ya usa para el candado único, aquí como orden de
  cola, no como la cancelación de tarea en curso que ese candado además
  hace (fuera de alcance: nada usa esta puerta todavía).
- `release(lease_id) -> bool` libera. Un `lease_id` ya liberado (o nunca
  emitido) devuelve `False` sin efecto — esto es lo que hace que un
  resultado tardío no reabra un trabajo: no hay generación que reabrir,
  cada lease tiene un id único de un solo uso.
- `async with resource_admission.lease(pool_id, priority=...):` adquiere,
  libera siempre en `finally` — también si la tarea que sostiene el bloque
  es cancelada, así que una cancelación libera su hueco de inmediato en vez
  de dejarlo huérfano.
- `acquire(..., timeout=…)` lanza `AdmissionTimeout` en vez de bloquear para
  siempre cuando el pool sigue lleno.

## `GET /api/ops/admission` (admin)

Devuelve `{"pools": [{pool_id, kind, endpoints, max_concurrent, in_use,
foreground_waiting, available}, …]}` — solo lectura, sin efectos. Gateada
como el resto de `routes/ops_routes.py` (nombrar los pools es información
operativa).

## Cómo se conectaría a `src/llm_core.py` (no hecho en este lote)

`llm_core._local_model_slot()` ya distingue `workload="foreground"` de
`"background"` y ya cancela una llamada `background` en curso cuando llega
una `foreground` — una política más fuerte que la de este módulo (aquí solo
se reordena la cola de espera, nunca se cancela lo que ya está en curso).
Lo que `_local_model_slot` NO distingue es *qué servidor* responde: dos
Ollama distintos (o un local y un remoto) comparten el mismo
`_LOCAL_MODEL_LOCK`, aunque no compitan por ningún recurso real.

La conexión, si se decide tras medir, sería: en vez de un candado único,
`_local_model_slot` resolvería `pool_id = resource_admission.pool_for_endpoint(target_url)`
(definiendo pools con `define_pool` a partir de los endpoints configurados,
`kind="gpu"` para los locales con GPU, `"cpu"` para los locales sin ella,
`"remote"` para los que no son de esta máquina) y usaría
`resource_admission.lease(pool_id, priority=kind)` en vez de
`_LOCAL_MODEL_LOCK.acquire()/.release()`. La cancelación de fondo cuando
llega una petición foreground (la parte que este módulo no replica) seguiría
viviendo en `llm_core`, por encima de la admisión. Esto NO se ha hecho aquí:
haría falta primero la medición pendiente, y `llm_core.py` en este lote es
solo lectura.

## Límites declarados

- No se suma VRAM/capacidad nominal como garantía — este módulo no mide
  hardware, solo cuenta cuántas admisiones simultáneas hay contra un tope
  que alguien configuró.
- No se lanzan benchmarks para descubrir a ciegas el límite de la máquina
  (la ficha lo prohíbe explícitamente).
- Los pools viven en memoria del proceso (igual que las reservas de
  `vram_admission`); no hay persistencia en `DATA_DIR` porque no hay estado
  que sobreviva a un reinicio que tenga sentido conservar — un pool es una
  política de admisión, se redefine al arrancar.

## Tests

`tests/test_adp32_resource_pools.py`: dos URLs del mismo servidor → un solo
pool; un segundo pool no puede reclamar un endpoint ya poolado;
`max_concurrent` por defecto es 1; una carga concurrente real nunca supera
el `max_concurrent` configurado (con `max_concurrent=2` y 8 tareas, el pico
medido es exactamente 2); `"foreground"` adelanta a `"background"` en cola;
cancelar la tarea que sostiene un lease libera el hueco; una segunda
liberación del mismo lease (resultado tardío) es un no-op y no reabre
capacidad; `acquire(timeout=...)` no cuelga; la ruta HTTP exige admin y
refleja el estado real.
