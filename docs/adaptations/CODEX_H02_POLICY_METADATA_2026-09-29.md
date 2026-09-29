# H02 parcial: política solicitada y entorno efectivo

Estado: **implementado y probado el contrato de resultados** para `bash/python`
que pasan por `sandbox_exec`, y para la ejecución directa del proveedor Docker.
H02 completo permanece pendiente: no hay un backend Windows nativo nuevo ni una
prueba de confinamiento de archivos/red/procesos hijos en este cambio.

Fuente: backlog H02 del [análisis local](CODEX_HARNESS_ANALISIS_2026-09-29.md),
inspirado en la separación de política y resultado de
[Codex ToolOrchestrator](https://github.com/openai/codex/blob/b1e72963c3b71a9265a551e54beff078384efed9/codex-rs/core/src/tools/orchestrator.rs).
Se reutiliza la revisión fijada; no se ha vuelto a investigar el repositorio.

## Resultado entregado al consumidor

Se añaden campos a los resultados con sandbox solicitado:

| Campo | Significado |
|---|---|
| `requested_policy` | `sandbox_auto` o `sandbox_strict`, capturado antes de ejecutar; `docker_container` en el proveedor directo |
| `effective_policy` | `host`, `docker_container` o `not_executed` |
| `policy_reason` | Motivo del destino host o del rechazo; vacío cuando se ejecuta en contenedor |
| `fallback_reason` | Motivo solo cuando la solicitud de sandbox terminó en host |

El consumidor real `PythonTool`/`BashTool` recibe estos datos, además de los
campos existentes `execution_target` y `sandbox_skipped`. Un rechazo ya no lleva
un destino `container` añadido por defecto: su `execution_target.kind` es
`not_executed`, con ruta y shell vacíos.

La captura usa `ContextVar`: dos llamadas concurrentes no comparten el modo ni
el motivo. Un cambio de preferencias mientras se ejecuta el comando tampoco
reescribe retrospectivamente la política solicitada. Se limpia cualquier motivo
anterior antes de despachar otra llamada.

No se cambian preferencias, aprobaciones ni selección de backend. En particular,
el código preexistente deriva al host en Windows **tanto en auto como en strict**;
este entregable hace explícita esa diferencia en el recibo, no afirma que strict
sea confinamiento Windows. En POSIX, indisponibilidad Docker en strict conserva el
rechazo y auto conserva el fallback documentado.

## Compatibilidad y alcance

Con sandbox desactivado se conserva el resultado anterior, sin claves nuevas:
esa compatibilidad está protegida por pruebas existentes. Los nuevos campos no
se prometen todavía para herramientas remotas, PowerShell directo, herramientas
de archivos ni otros motores. `effective_policy` identifica la ruta seleccionada
según la respuesta del ejecutor, no certifica por sí sola todos los límites de un
contenedor ni una ejecución que no haya devuelto resultado.

## Pruebas

```text
venv/Scripts/python.exe -m pytest tests/test_sandbox_policy_metadata.py tests/test_sandbox_exec.py tests/test_sandbox_provider.py -q -rs
```

**32 correctas, 5 omitidas**. Casos nuevos: host Windows auto/strict con salida real
de Python, Docker indisponible auto/strict, consumidor que recibe los campos,
solicitudes concurrentes con modos distintos, cambio de modo tras despacho,
compatibilidad off, limpieza del motivo y resultados del proveedor con éxito/rechazo.

Las cinco pruebas Docker reales se omiten porque Docker Desktop no expone su
daemon Linux (`dockerDesktopLinuxEngine` no encontrado). Las pruebas con dobles
no sustituyen esa evidencia: no se ha probado confinamiento real de contenedor.

## Pendiente de H02

- Revisar en un cambio separado el contrato de `strict` en Windows: si implica
  garantía de aislamiento, debe rechazar al faltar backend en lugar de pasar al
  host. El fallback heredado sigue presente y es un riesgo identificado, no una
  aceptación de esa semántica como política definitiva.
- Probe real con fixtures permitidos/prohibidos; raíces hermanas, junctions,
  procesos hijos y red.
- Backend Windows nativo verificable, o integración con uno que aplique permisos.
- Presentación del entorno efectivo antes de comenzar acciones y cobertura uniforme
  de todos los ejecutores. Este commit solo cambia los recibos de las rutas descritas.
