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
| H01 | `664894a5`: autoridad host explícita, 16 pruebas; confinamiento pendiente |
| H02 | `5f781023`: política solicitada/efectiva y motivo, 32 pruebas y 5 omitidas sin Docker. Modo obligatorio en preparación; backend Windows pendiente |
| H03 | En implementación acotada: registro previo durable para envíos de correo dentro de runs rastreados; otras rutas pendientes |
| H04 | `0572e1af`: 55 pruebas de normalización→evento→reinicio, partial/unknown preservados. UI completa, errores genéricos e identidad de intentos pendientes |
| H05 | `7c558eee`: 262 pruebas, round-trip de 220 herramientas y MCP; `155fad3d`: 58 pruebas de errores numéricos PDF. Autoridad ejecutable común aún pendiente |
| H06–H24 | Pendientes conforme al análisis original; no declarar cerrados por este documento |

Los pilotos de reinicio de procesos (`403756a9`) y memoria (`9e0e8e50`) ya aportan evidencia a H11/H20/H24, pero no completan esos cambios transversales.

Informes de entregables: [H02](CODEX_H02_POLICY_METADATA_2026-09-29.md), [H04](CODEX_H04_EFFECT_RESULTS.md), [H05](CODEX_H05_RUNTIME_ROUNDTRIP_2026-09-29.md). Cada uno conserva referencia original, pruebas y límites. No repetir estas inspecciones salvo regresión o pregunta nueva.
