# Topología física de GPU — INF-05 §11 (Lote A)

`src/gpu_topology.py` + `GET/POST /api/hardware/topology*`. Antes de este
contrato, `src/gpu_placement.py` y `src/gpu_shared_memory.py` ya sabían
"cuántos bytes tiene la GPU 0 ahora mismo", pero "GPU 0" era un índice de
enumeración de `nvidia-smi` — estable mientras nadie reconecta nada, y falso
en cuanto alguien reconecta una eGPU (AORUS u otra) o el driver reordena las
tarjetas tras un reinicio. Este módulo añade la identidad física (uuid, bus
id) que un índice nunca es, y lo poco que se puede saber del enlace/
transporte sin inventarlo.

## Identidad física frente a índice

`src.contracts.inference.identity_key(gpu)`: `uuid` si `nvidia-smi` lo dio,
si no `bus_id`, si no `None`. **Nunca** el índice. Todo lo que compara dos
lecturas en el tiempo (`reconcile_indices`, `hardware_profiles.
reconciliation_against`) usa esta clave — nunca "misma posición en la lista"
como prueba de "mismo dispositivo".

`reconcile_indices(previous, current) -> tuple[IndexReconciliation, ...]`,
uno por GPU con identidad conocida en cualquiera de los dos lados, con
`state`:

- `same` — misma clave, mismo índice.
- `moved` — misma clave, índice distinto (la AORUS que era "GPU 1" ahora es
  "GPU 0").
- `missing` — tenía clave en `previous`, no aparece en `current`
  (desconectada, o apagada).
- `new` — aparece en `current`, no estaba en `previous`.
- `unidentifiable` — ninguna de las dos lecturas tiene uuid ni bus_id para
  esa GPU. **Nunca** se reporta `same` para una GPU sin identidad solo
  porque el índice no cambió — eso sería adivinar, no reconciliar.

## `GET /api/hardware/topology?host=&ssh_port=`

```json
{
  "snapshot": {
    "host": "localhost",
    "gpus": [
      {"index": 0, "name": "NVIDIA GeForce RTX 4070 Ti", "uuid": "GPU-...",
       "bus_id": "0000:01:00.0", "vram_bytes": 12878610432,
       "driver": "535.129.03",
       "link": {"gen_current": 4, "width_current": 16, "gen_max": 4,
                "width_max": 16, "source": "observed"},
       "transport": null}
    ],
    "observed_at": "2026-09-12T00:00:00Z", "topology": "known",
    "provenance": "nvidia-smi"
  },
  "reconciliation": [
    {"key": "uuid:GPU-...", "previous_index": 1, "current_index": 0, "state": "moved"}
  ],
  "profile_id": "a1b2c3d4e5f6"
}
```

`reconciliation` es contra el perfil de hardware **capturado más
recientemente** (`hardware_profiles.list_profiles()[0]`) — este lote no
introduce un concepto de "perfil activo" separado; si no hay ningún perfil
guardado, `reconciliation: []` y `profile_id: null`, nunca una comparación
inventada. Sin `nvidia-smi` (o sin GPU NVIDIA), `snapshot.topology:
"unknown"`, `gpus: []`, y `provenance` dice por qué — nunca una lista vacía
disfrazada de "no hay GPUs" (eso es indistinguible de "no se pudo mirar").
Un `host` remoto todavía no se lee (`topology: "unknown"`, motivo explícito
en `provenance`) — ver "Límites" al final.

Cacheado 8 s, igual que `gpu_shared_memory.vram_snapshot`. Nunca arranca
inferencia ni lanza un benchmark.

## Enlace PCIe y transporte

`link` son las columnas `pcie.link.*` de `nvidia-smi`, cada campo `None`
cuando el driver responde `[N/A]`/`[Not Supported]` — nunca 0. `transport`
tiene tres orígenes posibles, nunca confundibles entre sí:

- `observed` — nadie lo produce en este lote (`nvidia-smi` no reporta el
  transporte físico); el campo queda reservado para cuando exista una fuente
  real.
- `heuristic` — **solo** cuando el enlace observado es estrecho
  (`width_current <= 4` con `gen_current` también observado):
  `{"kind": "unknown", "source": "heuristic", "note": "narrow link (x4): may
  be an external enclosure; verify manually"}`. Nunca se deduce de una
  etiqueta comercial: una GPU llamada "AORUS" o "eGPU" en el nombre **no**
  es evidencia de transporte, ni siquiera cuando el enlace es ancho.
- `manual` — una anotación de una persona (siguiente sección). Gana sobre
  `heuristic`; nunca sobre `observed`.

## `POST /api/hardware/topology/annotate`

```json
{"gpu_key": "uuid:GPU-...", "kind": "thunderbolt", "note": "eGPU box por USB4", "profile_id": "a1b2c3d4e5f6"}
```

`kind` ∈ `pcie | thunderbolt | oculink | usb4 | unknown` — cualquier otro
valor es `400 hardware.invalid_annotation`. `require_admin`. Si
`profile_id` no nombra un perfil existente, se captura uno nuevo primero
(`hardware_profiles.collect_profile()` — sin arrancar ningún benchmark,
igual que HW-05 ya garantiza) y la anotación se guarda ahí. Devuelve
`{"profile": {...}}`, el perfil actualizado con `topology_annotations`.

## Límites pendientes

- Topología remota (`host` no vacío) no se lee todavía: no hay un
  `nvidia-smi` por SSH genérico reutilizable en este repo; `snapshot()`
  devuelve `topology: "unknown"` con el motivo explícito en vez de fingir
  una lectura. `services.hwfit.hardware.detect_system` sí soporta SSH para
  otras métricas — cablear la topología por el mismo camino es trabajo
  pendiente, no simulado aquí.
- `transport.source == "observed"` no tiene productor todavía —
  `nvidia-smi` no expone el tipo de enlace físico directamente.
