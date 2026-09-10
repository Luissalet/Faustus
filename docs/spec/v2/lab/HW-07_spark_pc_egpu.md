# HW-07 — Spark + PC + eGPU: laboratorio (LAB, opt-in, fuera del núcleo)

Estado: **diseño + benchmark reproducible únicamente**. Cero código en
`src/`, `routes/` o `services/hwfit/`; nada de esto se carga ni se importa
desde la aplicación. `scripts/lab/hw07_spark_pc_egpu_benchmark.py` es el
único artefacto ejecutable y solo corre si alguien lo invoca explícitamente
desde la línea de comandos, con un flag de reconocimiento explícito
(`--i-know-this-is-experimental`).

## Por qué esto es un laboratorio y no una función

El backlog pide, literalmente: *"no afirmar RDMA, pooling o failover
transparente sin probarlos"* y *"solo se declara soportada una combinación
que supera carga, generación, cancelación, caída de nodo y recuperación en
esa configuración"*. Ninguna de esas cinco pruebas está hecha con hardware
real (no hay un Spark, un PC de escritorio y un eGPU conectados a esta
máquina de CI/dev). Escribir código de "soporte" en el núcleo sin haberlo
probado en la topología real sería exactamente la afirmación sin probar que
el requisito prohíbe. De ahí que HW-07 se resuelva aquí con tres cosas:

1. **Este documento**: qué se mediría y cómo, para que quien tenga el
   hardware pueda ejecutar el benchmark y juzgar por los números, no por una
   promesa.
2. **Un script reproducible** (`scripts/lab/hw07_spark_pc_egpu_benchmark.py`)
   que mide lo que SÍ se puede medir sin la topología completa (reparto de
   capas simulado, ancho de banda del enlace disponible, latencia de una
   llamada remota) y deja explícito, en su propia salida, qué NO mide
   (RDMA real, pooling de memoria entre nodos, failover transparente).
3. **Ninguna declaración de soporte** en `src/capability_registry.py`, ni un
   backend nuevo, ni una ruta. HW-06 (`src/remote_worker_registry.py`, este
   mismo lote) es la pieza reutilizable si algún día alguien ejecuta las
   cinco pruebas reales sobre esta topología: un "Spark + PC + eGPU" no es
   más que tres nodos remotos registrados y con salud — la infraestructura
   de registro/reserva/reconciliación ya sirve, sin un camino de código
   aparte para esta topología en concreto.

## Topología asumida

```
┌─────────────┐      red local / Thunderbolt      ┌──────────────┐
│  PC (host)  │◄──────────────────────────────────►│  Spark (nodo)│
│  eGPU local │                                     │  GPU propia  │
└─────────────┘                                     └──────────────┘
```

- **PC**: la máquina que orquesta, con una eGPU conectada localmente
  (Thunderbolt/USB4 u OCuLink).
- **Spark**: un segundo nodo de cómputo (real o simulado), alcanzable por
  red, registrado vía `src.remote_worker_registry.register_node` una vez
  emparejado por SSH (`src.ssh_trust`).
- **eGPU**: tratada como una GPU local más a efectos de `services/hwfit`
  (`detect_system` ya distingue GPUs por índice); el reparto de capas entre
  la GPU local y el nodo remoto es lo que el script de benchmark mide.

## Qué mide el script (`--mode capacity`, sin red)

Sin tocar la red ni un nodo remoto: reparto teórico de capas de un modelo
dado entre "GPU local" y "nodo remoto", a partir de sus VRAM medidas
(reutiliza `src.vram_fit`), y una estimación de cuántas capas caben en cada
lado. Esto es aritmética, no una medición de hardware — el script lo marca
como tal en su salida (`"measured": false`).

## Qué mide el script (`--mode link`, con un nodo emparejado)

Con un `host` ya emparejado por SSH (`ssh_trust.is_paired`):

- **Latency**: round-trip de `ssh ... true`, N repeticiones, min/p50/p99.
- **Bandwidth**: transferencia de un payload de tamaño configurable por
  `scp`/`ssh cat`, para tener una cifra real del enlace disponible (no la
  nominal del cable) — el número que decide si repartir capas por ese
  enlace tiene sentido o es más lento que no repartirlas.

Ambas cifras son reales (`"measured": true`) porque las produce una
ejecución real contra el nodo, no una tabla de referencia.

## Lo que este laboratorio NO afirma

- **RDMA**: no probado, no implementado, no mencionado como disponible en
  ninguna salida del script.
- **Pooling de memoria transparente entre nodos**: no existe; cada nodo
  sigue siendo un proceso de inferencia separado. "Repartir capas" aquí es
  candidatura aritmética, no ejecución partida real de un modelo.
- **Failover transparente**: `src.remote_worker_registry.reconcile()` (este
  lote) da diagnóstico + liberación cuando un nodo desaparece — eso es
  *reconciliación*, no *failover*: el trabajo en curso en el nodo caído se
  pierde y se reporta como incierto, nunca se reintenta solo en otro nodo.

## Cómo declarar la combinación "soportada" (criterio de aceptación)

Solo cuando, con hardware real, las cinco pruebas del criterio de aceptación
pasen de verdad:

1. Carga: el modelo se carga repartido sin error.
2. Generación: produce tokens/frames coherentes de principio a fin.
3. Cancelación: un cancel a mitad de generación limpia ambos lados (local +
   remoto) sin dejar el nodo remoto "ocupado" para siempre.
4. Caída de nodo: matar el proceso del lado Spark a mitad de generación
   produce el resultado de `reconcile()` (incierto, nunca éxito falso).
5. Recuperación: re-registrar el nodo y reintentar el trabajo funciona sin
   reiniciar el proceso del PC.

Cuando alguien ejecute y documente esas cinco pruebas con hardware real, el
resultado (con logs) es lo que justificaría escribir código de soporte en
`src/capability_registry.py` — no antes. Este documento y el script son la
preparación para ese día, no una declaración de que ya llegó.
