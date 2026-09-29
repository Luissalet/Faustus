# H21 parcial: bytes comprobados y usados en las instrucciones

Fuente del patrón: [análisis local H21, sección 22](CODEX_HARNESS_ANALISIS_2026-09-29.md) y [Codex agents_md.rs fijado](https://github.com/openai/codex/blob/b1e72963c3b71a9265a551e54beff078384efed9/codex-rs/core/src/agents_md.rs). Implementación propia; se continuó el backlog local, sin nueva revisión del catálogo de 41 proyectos ni modelos reales.

## Fallo reproducido

El incremento anterior de [identidad de contenido](CODEX_H21_INSTRUCTION_CACHE.md) corrigió la caché por mtime, pero dejó explícitamente pendiente el intervalo entre confianza y lectura. `agent_loop` obtenía un booleano de `instructions_trusted` y luego `project_instructions.block` abría otra vez el archivo. El recordatorio posterior a compactación hacía además su propia lectura y render.

Prueba temporal con funciones reales y almacén aislado: aprobar `AGENTS.md = APPROVED_ALPHA` en modo strict; ejecutar el chequeo real, obtener True y escribir `UNAPPROVED_BRAVO` antes de devolver ese veredicto; construir el prompt con `_build_system_prompt`. Resultado anterior: el texto BRAVO entra en el sistema aunque `state_for` ya devuelve changed. La prueba que exigía excluirlo falló. No depende de caché ni metadatos.

## Cambio y garantía acotada

`instructions_snapshot` captura el orden de candidatos y los bytes acotados de todos los archivos usados por el digest existente. `state_for` y `resolve` pueden comprobar ese mismo conjunto capturado, sin cambiar sus llamadas públicas anteriores. El snapshot contiene dataclasses frozen, una tupla de archivos y bytes inmutables, el digest conjunto, estado, modo, condición degraded y el archivo seleccionado según la prioridad congelada.

El prompt inicial y el recordatorio posterior a compactación utilizan ese snapshot. El renderer no redescubre candidatos ni vuelve a abrir instrucciones después del veredicto: el fragmento procede de los bytes comprobados. Si no había archivo en la captura, aparecer uno después no lo introduce. Un nuevo archivo prioritario tampoco sustituye al seleccionado. Se conserva UTF-8 con reemplazo de errores, normalización universal de CRLF/CR antes del límite de caracteres, truncamiento y hash del fragmento normalizado.

No cambia el formato v1 de aprobación ni exige aprobar de nuevo. El digest conjunto sigue incluyendo nombres, tamaños y bytes con prefijos de longitud, en orden estable, con su límite existente de **1.000.000 bytes por archivo**. No es SHA-256 del archivo completo: cambios de igual tamaño después del límite continúan fuera de su cobertura. El fragmento máximo de 60.000 caracteres procede de la región capturada; su `text_sha256` sigue siendo identidad del texto renderizado y no una aprobación.

## Compatibilidad y límites

`instructions_trusted`, `read` y `block(trusted=...)` conservan sus APIs y política histórica. La nueva ruta usa `block_from_snapshot` y `read_snapshot`; las llamadas legacy no adquieren garantía de snapshot por sí mismas. `off` no lee el almacén ni calcula un digest: devuelve un snapshot marcado `legacy_read` que conserva la lectura tradicional. Errores que impiden construir un snapshot también quedan marcados `degraded` y `legacy_read`, manteniendo el fail-open previo. La autoaprobación ask se conserva, incluida la inyección degraded si no se puede guardar o consultar la aprobación. Un estado degraded no se presenta como aprobación persistida.

No se introduce una nueva autorización ni se cambia la política de fallos. En particular, fail-open puede seguir admitiendo contenido no aprobado cuando el subsistema falla: es una limitación explícita conservada, fuera de la garantía de una comprobación normal.

Tampoco es una transacción atómica sobre el directorio: los archivos se capturan secuencialmente, puede cambiar el conjunto durante esa captura y una revocación posterior no cancela retroactivamente un snapshot ya obtenido. La garantía es que el texto usado deriva de los bytes cuyo digest se comparó con la aprobación, bajo funcionamiento normal.

`project_rules` continúa recibiendo el booleano de confianza, pero sus archivos `.faustus/rules/` no forman parte de este digest y se leen por otra ruta. Los objetivos y otros consumidores tampoco quedan cubiertos. Este incremento no cierra la confianza global del repositorio, la jerarquía de instrucciones por directorio ni la procedencia completa de skills.

## Verificación

**171 pruebas correctas y 1 omitida** por permisos POSIX en Windows, 8,11 s. Incluye snapshot, ensamblador de prompt real, recordatorio real de compactación, almacén temporal, caché previa, aprobación/revocación, escritura de instrucciones y compactación.

Casos nuevos: edición posterior a captura, nuevo archivo prioritario, cambio de candidatos, ausencia inicial, edición antes de captura, conjunto de varios archivos, límite de bytes, UTF-8 malformado, CRLF y CR suelto, presupuestos 500/6.000/60.000, hash, inmutabilidad, off, ask autoaprobado, degraded y fail-open. La antigua comprobación de source del ensamblador se sustituyó por una comprobación del prompt real tras una edición; la prueba de excepción del ensamblador intercepta ahora su nueva API de snapshot y conserva la misma exigencia de fallback.

```text
venv/Scripts/python.exe -m pytest tests/test_instruction_approved_snapshot.py tests/test_agent_loop_workspace_trust.py tests/test_workspace_trust.py tests/test_codex_h21_instruction_cache.py tests/test_project_instructions_remember.py tests/test_project_instructions_atomic_remember.py tests/test_context_compactor.py tests/test_context_compactor_nonstring.py tests/test_context_compactor_regressions.py -q
```
