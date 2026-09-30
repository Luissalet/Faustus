# H21 parcial: identidad del contenido de instrucciones

## Hallazgo visitado e implementado

La caché de `project_instructions.block()` comparaba ruta y `mtime`, después
de un TTL de cinco segundos. Una sustitución del archivo por otro contenido
de igual longitud conservando `mtime` dejaba el bloque anterior indefinidamente.
La lectura directa ya devolvía el contenido nuevo. Es reproducible sin modelos,
proveedores ni datos reales: `alpha` → `bravo`, conservar timestamps con utime,
avanzar reloj más allá del TTL, comparar `read` y `block`.

Ahora cada llamada confiable lee una vez el fragmento con el límite existente
(6.000 caracteres por defecto, máximo 60.000). Identifica el texto normalizado
mediante SHA-256 y cachea el render por ruta, hash y estado de truncamiento.
Cambiar contenido, archivo seleccionado o presupuesto refresca inmediatamente
el bloque; no depende del reloj ni de metadatos del archivo. La nota sin confianza
conserva su TTL y continúa excluyendo el contenido. No cambia el texto del prompt
cuando las instrucciones son las mismas.

`read()` añade `text_sha256` junto a la ruta relativa/absoluta existente. Es el
hash del fragmento normalizado, no del archivo completo, del conjunto de reglas
ni de una aprobación. No se añade otra aprobación ni se amplía la precedencia
por directorio. Este incremento no instrumenta todavía el detalle por skill del
contexto H09.

## Confianza y límites

`workspace_trust` compara su propio digest del conjunto de archivos con el
aprobado; `agent_loop` pasa el veredicto booleano a `block`. Prueba real con almacén
temporal: aprobar A, editar a B con mismos metadatos, comprobar rechazo del digest
anterior y nota sin contenido, aprobar B y obtener B inmediatamente, revocar y
volver a excluir contenido. Las claves de caché confiable/no confiable permanecen
separadas. Se conserva la política existente de confianza y su modo desactivado.

**No es un snapshot aprobado atómico.** Continúa el intervalo TOCTOU entre el
veredicto booleano de confianza y la lectura del fragmento. Corregirlo requiere
un contrato que vincule los bytes aprobados con los bytes usados; queda pendiente.
También quedan pendientes instrucciones jerárquicas por directorio con trust
por ámbito y procedencia/versión de cada skill realmente revelada. Descubrimiento
de skills ya tiene límites de repositorio, origen, shadowing y revisión por digest;
no se duplicaron esos mecanismos.

Coste: una lectura acotada por llamada confiable; un archivo muy grande aún puede
sufrir costes de antivirus al abrirse. No se midió latencia en producción ni se
atribuye mejora de velocidad. El objetivo comprobado es coherencia del contenido.

## Fuentes

[Análisis local H21, sección 22](CODEX_HARNESS_ANALISIS_2026-09-29.md),
[Codex agents_md.rs fijado](https://github.com/openai/codex/blob/b1e72963c3b71a9265a551e54beff078384efed9/codex-rs/core/src/agents_md.rs).
Se usó la inspección ya registrada; no se repitieron revisiones upstream ni el
radar de 41 proyectos. Implementación propia de la corrección de caché; no se
copió código upstream.

## Pruebas

`venv/Scripts/python.exe -m pytest tests/test_codex_h21_instruction_cache.py
tests/test_project_instructions_remember.py tests/test_project_instructions_atomic_remember.py
tests/test_workspace_trust.py tests/test_agent_loop_workspace_trust.py -q`

Resultado: **66 pasadas, 1 omitida** (permisos POSIX, omitida en Windows), 3,38 s.
Casos nuevos: sustitución con igual tamaño y mtime dentro/fuera de TTL, edición
normal, cambio de prioridad de archivo, eliminación, presupuesto/truncamiento,
hash del fragmento normalizado y ciclo real de aprobación/cambio/reaprobación/revocación.

## Incremento posterior: caché de reglas (e62ad0eb)

project_rules.block conservaba firma path/mtime y podía devolver texto antiguo
con edición del mismo tamaño/mtime. Ahora captura discovery una vez y cachea
por SHA256 de la proyección acotada/decodificada, metadatos ordenados y error.
Render y nota sin confianza usan esa misma tupla; no segunda lectura. Pruebas
con tempfile reales cubren cambios/altas/bajas/error/recuperación y un solo scan.
Coordinador7 nuevas correctas0,70s; agente selección165 correctas/1 omitida6,20s.
No inferencia GPU ni servicios personales; omisión POSIX no certifica Windows.

Esta identidad no amplía digest de aprobación de workspace: rules/objetivos
siguen fuera. Captura secuencial no es transacción de directorio, ni hash completo
más allá del límite. Biblioteca/TTL/rendercache previos fuera de alcance.
Freshness de objetivos en reuse_scope H09 sólo evaluado: falta recibo de versión
de la fuente; guard actual sólo files. No implementado ni globalmente cerrado.
