# La puerta de VRAM — `src/vram_admission.py` (OBJ-1, HW-01/HW-03, INF-05 Lote B)

Autoridad única de capacidad. Chat, research, el botón «Load», Cookbook
`serve` y el banco de benchmarks pasan **todos** por este módulo — nunca
hay un segundo contador de VRAM en el repositorio, y ninguna exclusión de
la puerta es implícita: si algo la salta, tiene que decirlo explícitamente
en su propio código, no simplemente no llamarla.

Reglas que no cambian en este lote: nunca se mata ni se descarga un
proceso ajeno (solo modelos Ollama, vía `keep_alive: 0`); nunca se
reinicia un servidor; una lectura obsoleta o una arquitectura desconocida
nunca autorizan una carga arriesgada, pero la ignorancia tampoco bloquea
— se reporta.

## `assess()`/`admit()` — el gate por nombre de modelo Ollama (INF-03/HW-01)

Sin cambios de comportamiento en este lote. `assess(root, model)` responde
si `model` cabe AHORA MISMO junto a lo que ya está residente en `root`
(`fits: True|False|None`); `admit(endpoint_url, model, ...)` es la
coroutine que de verdad decide — reserva, pregunta a la persona (modo
`ask`), descarga automáticamente (modo `auto`), o deja pasar sin más
(modo `off` o cuando `endpoint_url` no es un Ollama de esta máquina).

## Reservas que no caducan durante una carga viva (§12, T12)

Una reserva (`try_reserve`) es memoria apartada del presupuesto desde el
instante en que se concede, hasta que `assess()` ve el modelo realmente
residente o pasa su `ttl` (180 s por defecto) — lo que llegue antes. Ese
`ttl` basta para una carga típica, pero no para un modelo de 27B por PCIe,
que puede tardar varios minutos. Tres funciones nuevas cierran esa
ventana:

- **`mark_loading(reservation_id)`**: marca la reserva como protegiendo
  una carga EN CURSO. Desde ese momento, `ttl` deja de importar:
  `_expire_reservations_locked` nunca la libera antes de
  `HARD_CAP_SECONDS` (900 s) desde su creación, pase lo que pase con los
  latidos. Pasado ese tope sí se libera — con `logger.warning`, como
  último recurso, nunca en silencio.
- **`heartbeat(reservation_id) -> bool`**: renueva el reloj de la reserva.
  Devuelve `False` cuando ya no existe (liberada, caducada, o forzada tras
  el tope duro) — la señal para que quien llama deje de asumir que esa
  memoria sigue reservada.
- **`heartbeat_while_loading(reservation_id, *, interval=25.0,
  max_seconds=HARD_CAP_SECONDS - 30.0)`**: tarea de fondo autolimitada —
  `asyncio.create_task(...)` y olvidarla. Se detiene sola en cuanto
  `heartbeat()` dice que la reserva ya no existe, o al llegar a
  `max_seconds`, lo que ocurra antes. Nunca es una tarea sin límite.

Tres llamadores usan este patrón, cada uno con la precisión que su propio
bucle permite:

| Llamador | Cuándo cancela el latido |
|---|---|
| `src/bench/runner.py::_run_one_case` | En el primer `delta` streameado — momento exacto, visto en su propio bucle de `stream_llm`. |
| `routes/local_models_routes.py::api_load` (botón «Load») | Al terminar la llamada bloqueante a `_set_keep_alive` (que puede tardar hasta `_LOAD_TIMEOUT` = 600 s). |
| `routes/chat_routes.py::_vram_admission_events` | No tiene forma barata de ver "el primer token" desde ese generador — usa el límite propio de `heartbeat_while_loading` en vez de cancelar antes. Sigue siendo seguro: nunca deja expirar una reserva antes del tope duro, solo late un poco más de lo estrictamente necesario. |

## Alias de endpoint (T14)

`ollama_root("http://localhost:11434/v1")` sigue devolviendo
`"http://localhost:11434"` tal cual (los llamadores existentes dependen de
ese valor literal) — pero las reservas, los pins y el "last active" ahora
se guardan bajo una clave **canónica** (`_canonical_root`, función privada):
`localhost`/`127.0.0.1`/`::1`/`0.0.0.0`/`host.docker.internal` colapsan al
mismo `127.0.0.1`. Una reserva hecha contra `http://localhost:11434` la ve
`reserved_bytes("http://127.0.0.1:11434")`, y un pin puesto por un alias
protege el modelo sin importar qué alias pregunte después.

## Reservas por dispositivo físico

`try_reserve(..., device=)` acepta un índice (`int`, como antes) o una
identidad física (`str`, un uuid de GPU) — ambos son solo material de
clave aquí; decidir CUÁL es la identidad correcta para un llamador dado es
cosa de `src/gpu_topology.py`/`src/memory_budget.py` (INF-05 Lote A).
`reserved_bytes(root, device=, include_devices=False)` sigue devolviendo
solo el cubo pedido por defecto (nada cambia para los llamadores
actuales); `include_devices=True` sirve para consultas nuevas que quieran
ver pool + dispositivos como una sola cifra.

## `admit_bytes` — la puerta engine-agnóstica para Cookbook `serve` (§12, B2)

Cookbook lanza `llama-server`/`vllm`/`sglang`/`mlx` con la misma frecuencia
que Ollama, y ninguno de esos tiene un `/api/ps` que preguntar por nombre
de modelo. `admit_bytes` es la MISMA autoridad (misma tabla de reservas,
misma tabla de tickets) alcanzada por bytes + GPUs físicas en vez de un
nombre:

```python
admit_bytes(*, label: str, bytes_needed: int | None, gpu_indices: list[int],
           owner: str = "", mode: str | None = None, timeout: float | None = None,
           ollama_roots: list[str] = ()) -> {
  "decision": "proceed" | "blocked" | "unknown" | "off",
  "reservation_id": str | None,
  "ticket": str | None,
  "assessment": dict,  # mismas claves que assess() + "kind": "serve"
}
```

- **`bytes_needed=None`** → `decision: "unknown"` con
  `assessment.reason: "weights size unknown: nothing to check against"` —
  nunca bloquea por ignorancia, pero lo reporta.
- **`mode="off"`** → `decision: "off"`, nada se comprobó.
- El presupuesto sale de `_physical_budget_for(gpu_indices)` (función
  privada de este módulo, construida sobre `src.gpu_topology.snapshot()` y
  `src.memory_budget.physical_budgets()` — la misma autoridad física de
  INF-05 Lote A, nunca una segunda cuenta) menos las reservas ya hechas
  bajo la clave dedicada `_physical_reservation_root(gpu_indices)`
  (namespace separado de cualquier reserva por Ollama sobre la misma
  tarjeta — cada consumidor reserva sus propios bytes).
- Lectura obsoleta (`stale`) o sin lectura de GPU en absoluto (`source:
  "absent"`) → `decision: "unknown"`, nunca `"fits"` por una lectura vieja.
- **No cabe**: `decision: "blocked"`, se abre un ticket
  (`open_ticket(..., kind="serve")`) con el MISMO `assessment` que
  `assess()` devuelve (para que `VramAdmissionDialog`/`vramBlockedFrom` lo
  pinten sin cambios), y la función devuelve — **nunca espera a una
  persona** aquí (a diferencia de `admit()` en modo `ask`): una petición de
  serve es una sola llamada HTTP, no un stream que pueda quedarse abierto
  esperando un clic.
- `suggestion` solo ofrece residentes **Ollama** en esas GPUs concretas
  (vía `gpu_placement.placement`, restringido a los `ollama_roots` que el
  llamador declaró) — un proceso ajeno (un navegador, ComfyUI) nunca es
  candidato; su memoria queda dentro de `others_bytes`, sin nombre, sin
  acción.
- T13 (dos admisiones simultáneas): la misma técnica de test-and-set bajo
  lock de `try_reserve` que ya protegía `admit()` — dos `admit_bytes` por
  los mismos bytes en la misma GPU nunca los conceden ambos.

**Deadlock padre/hijo (T16, §12)**: `admit_bytes` no importa ni toca
ningún lock de Cookbook/`llm_core` (`_LOCAL_MODEL_LOCK` incluido) y sus
únicos `await` son lecturas de presupuesto/colocación y, en modo `auto`,
`unload_and_wait` sobre residentes Ollama que ella misma acaba de elegir
— nada aquí puede sostener un recurso que un hijo necesite para terminar
y luego esperar a ese mismo hijo. La pregunta de *scheduling* (¿debe un
padre ceder capacidad a un hijo bloqueado?) es del orquestador de
subagentes, fuera de este lote.

## Reconciliación al arrancar (T12)

`reconcile_on_start()` (registrado en `app.py`, junto a
`launch_receipts.reconcile_on_start`/`bench.runner.reconcile_on_start`):
vacía reservas y tickets pendientes — son en memoria, un reinicio ya los
pierde de todos modos — y lo dice en el log (`cleared`, `pending_cleared`).
No relee nvidia-smi por su cuenta: cualquier proceso que haya sobrevivido
al reinicio (un Ollama externo, un servidor gestionado por otro medio) ya
aparece como `used` en la siguiente lectura real que `assess()`/
`admit_bytes()` hagan — no se devuelve capacidad fantasma solo por haber
reiniciado.

## El modelo por defecto está protegido, no solo priorizado (X-B, §109)

`pin_model(root, model)` (§HW-03) ya hacía que un modelo con pin fuera el
último candidato de `assess()`'s `suggestion`, ofrecido solo cuando nada
sin pin bastaba para liberar el hueco (`suggestion_protected_used`). Desde
el lote X-B (FAUSTUS.md §109) `src/model_warmup.py` fija ese pin sobre el
modelo por defecto (Ajustes → Default AI) en cada ciclo de su guardián de
residencia, y dos caminos que antes podían tocar ese "último recurso" sin
que nadie mirara quedan cerrados del todo en vez de solo desincentivados:

- **`admit()` en modo `"auto"`** filtra `suggestion_protected_used` de la
  lista que de verdad descarga — un modelo con pin nunca sale expulsado
  por un camino que actúa sin que una persona lo vea, ni siquiera como
  último recurso.
- **`admit_bytes`** (`_ollama_suggestion_candidates`, lanzamientos de
  Cookbook `serve`) excluye los modelos con pin de sus candidatos por
  completo — ni se sugieren, ni un ticket de admisión deja elegirlos.

El modo `"ask"` de `admit()` sigue sin cambios: una persona puede elegir
explícitamente descargar un modelo con pin desde la tarjeta de admisión
(`resolve(action="unload", names=[...])`), igual que el botón «Unload» de
Ajustes → Local models (que no pasa por `vram_admission` en absoluto). La
protección es contra la expulsión SILENCIOSA, nunca contra el dueño de la
máquina.

`src/run_model_pin.py::restore_keep_alive` (el ping que devuelve el
`keep_alive` guardado al terminar una ejecución de agente) tiene la misma
regla por su lado: si el modelo que va a restaurar es el modelo por
defecto, el valor nunca baja de `-1`, sea cual sea el `keep_alive`
guardado para él.

**Recomendado, cinturón y tirantes, fuera de este repositorio.** Arrancar
el servicio de Ollama con `OLLAMA_KEEP_ALIVE=-1` en su entorno hace que
cualquier carga sin `keep_alive` explícito — de cualquier cliente, no solo
Faustus — nazca ya sin caducidad. No sustituye al guardián de
`src/model_warmup.py` (otro cliente puede seguir mandando su propio
`keep_alive` corto de forma explícita, que sigue ganando esa petición
concreta), pero reduce cuántas veces hace falta que el guardián actúe.

## Lo que esta puerta jamás hace

- Matar o descargar un proceso que no sea un modelo Ollama (`keep_alive:
  0`, el mismo camino que ya usa la pantalla Local models).
- Reiniciar un servidor, con o sin permiso — reiniciar nunca está entre
  sus acciones posibles.
- Tratar una lectura obsoleta, o una arquitectura/tarjeta desconocida,
  como "cabe" — ambas resuelven a `unknown`, nunca a `fits: true`.
- Bloquear un lanzamiento solo porque no se sabe cuánto pesa — la
  ignorancia se reporta (`decision: "unknown"`), nunca detiene por sí
  sola.
- Sumar la memoria compartida de WDDM como si fuera VRAM, ni la suma
  nominal de un pool multi-GPU como si fuera la capacidad de una sola
  tarjeta (eso vive en `src/memory_budget.py`, INF-05 Lote A).
