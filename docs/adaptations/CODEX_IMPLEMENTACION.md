# Adaptaciones del harness de Codex: implementación

Plan original: [CODEX_HARNESS_ANALISIS_2026-09-29.md](CODEX_HARNESS_ANALISIS_2026-09-29.md). Referencia fijada: [openai/codex b1e72963](https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9). Las implementaciones son propias y parciales salvo indicación contraria; no se ha ejecutado el producto Codex como benchmark.

## H01: declaración verificable del runtime host

El mínimo revisable está implementado: las descripciones nativas, catálogo y ajustes dicen que Code Mode ejecuta Python en el host con acceso a archivos/red. `tools.call` conserva su puerta de políticas, pero Python directo no la atraviesa. La herramienta ya estaba clasificada como `EXECUTE_CODE` y desactivada por defecto; se conserva esa clasificación.

Cada resultado del runner incluye `runtime_guarantees`: `host_process`, filesystem/network no aislados y alcance de políticas `tools.call_only`. Se incluye también al fallar/terminar por cuota. No se convierte `python -I`, el entorno mínimo ni el directorio temporal en una supuesta frontera del sistema operativo. La descripción de la pausa de aprobación refleja que depende del ajuste existente.

Prueba decisiva: el runner real escribe un señuelo temporal fuera del cwd del guest sin usar herramientas; el resultado declara correctamente acceso host. También se comprueba la declaración en un error real de Python. Pasaron 16 pruebas de runner, paridad de políticas, cuotas y aprobación.

**Pendiente de H01/H02:** aislamiento de filesystem/red efectivo y comprobaciones de procesos hijos. Este commit no proporciona confinamiento. No se modifica la configuración personal ni se habilita Code Mode.

## Estado del backlog

| ID | Estado |
|---|---|
| H01 | Entregable mínimo de autoridad host completado; confinamiento pendiente |
| H02 | Pendiente: política solicitada/efectiva y backend verificado |
| H03 | Pendiente: registro previo obligatorio para efectos no repetibles |
| H04 | En revisión: preservar resultados inciertos/parciales |
| H05 | En implementación: round-trip del catálogo; autoridad ejecutable completa pendiente |
| H06–H24 | Pendientes conforme al análisis original; no declarar cerrados por este documento |

Los pilotos de reinicio de procesos (`403756a9`) y memoria (`9e0e8e50`) ya aportan evidencia a H11/H20/H24, pero no completan esos cambios transversales.
