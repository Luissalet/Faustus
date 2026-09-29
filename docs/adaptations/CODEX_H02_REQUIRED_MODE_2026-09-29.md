# H02 parcial: modo de comandos `required`

Estado: **implementado sin activarlo**. La configuración por defecto sigue siendo
`auto`; no se modificó la configuración del usuario. Se preservan los modos
preexistentes `auto` y `strict`, incluido su comportamiento de compatibilidad Windows.

Origen: H02 del [análisis local de Codex](CODEX_HARNESS_ANALISIS_2026-09-29.md) y
su referencia ya revisada a
[ToolOrchestrator](https://github.com/openai/codex/blob/b1e72963c3b71a9265a551e54beff078384efed9/codex-rs/core/src/tools/orchestrator.rs).

## Contrato

Al activar `agent_sandbox_execution` y seleccionar `agent_sandbox_mode=required`:

- `bash` y `python` usan el backend de sandbox compatible o devuelven un rechazo;
  no pasan al host cuando falla su disponibilidad.
- En Windows nativo se rechazan antes de crear un proceso, porque no existe un
  backend compatible de confinamiento Windows en esta ruta.
- `powershell` también rechaza: su implementación actual es exclusivamente host.
- Los resultados indican `requested_policy=sandbox_required` y
  `effective_policy=not_executed` en esos rechazos, con razón explícita.
- El modo y el interruptor se capturan al comenzar el despacho. Cambiarlos durante
  el probe asíncrono no permite rebajar a host una llamada que empezó en required.
  La siguiente llamada sí observa la nueva preferencia; no se reescribe el ajuste.

La descripción del ajuste, el diagnóstico y el contexto entregado al agente
explican el rechazo Windows en lugar de asegurar que todo se ejecutará en este PC.
El agente recibe la instrucción de comunicar la limitación sin redirigir el mismo
comando a otra herramienta host ni cambiar la política del usuario.

## Alcance exacto

Es una política de las tres herramientas de comandos indicadas. No es un sandbox
universal para herramientas de archivo, Git, workers remotos o Code Mode. En
particular, el modo host de Code Mode tiene su propia activación y permanece fuera
de este contrato. El interruptor sandbox desactivado conserva las rutas anteriores.

No se implementa un backend Windows ni se afirma que se hayan comprobado todos los
límites de Docker. Los probes reales de fixtures, junctions, red y procesos hijos
siguen pendientes del backend disponible. `required` garantiza aquí la ausencia
del fallback host en las rutas cubiertas; no sustituye la validación del backend.

## Validación

Las pruebas usan comandos señuelo que escribirían un archivo, y verifican que no
se crea el proceso ni el archivo en Windows required y ante daemon ausente.
También prueban required → auto/off durante el probe, la restauración del contexto
al terminar y los mensajes de prompt/diagnóstico. Las suites existentes preservan
el comportamiento de auto/strict y sus consumidores.

```text
venv/Scripts/python.exe -m pytest tests/test_sandbox_required_mode.py tests/test_sandbox_policy_metadata.py tests/test_sandbox_exec.py tests/test_windows_native_execution.py tests/test_agent_settings_schema.py tests/test_command_timeout_outcomes.py -q
```

Resultado: **124 correctas, 5 omitidas**. Las omitidas necesitan un daemon Docker
real; no se cuentan como validación de confinamiento. El control positivo usa un
proveedor doble disponible y comprueba una única ejecución sin fallback host.
La implementación no se activa automáticamente.
