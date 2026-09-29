# H18 parcial: revisión vinculada a la preview de edición

## Reproducción y cambio

Con los handlers reales y un doble de `check_edit`, la revisión de bloque recibió
el diff `a old → A old`. Mientras esperaba, otro `write_file` sobre el mismo
archivo temporal escribió `a newer`. Sin `base_revision`, la llamada original
devolvía éxito: `write_file` dejaba `A old` y borraba el cambio intermedio;
`edit_file` dejaba `A newer`, una edición sobre contenido distinto del revisado.
El mutex previo funcionaba: no retenía ningún lock durante el await de revisión.

Ahora ambos handlers conservan la revisión capturada junto al texto de la preview.
Arman su precondición sólo cuando el gate devuelve una revisión efectiva: dict con
verdict `ok` o `concerns`, sin `error` ni `unparsed`. Un noop (`review=None`) o
un fallo del reviewer conserva el comportamiento fail-open vigente. La denegación
de revisión sigue retornando el resultado existente antes del worker.

El worker lee dentro del mutex y compara la revisión de preview antes de evaluar
la política de reescritura, la base del llamador o escribir. Si cambió, devuelve
`status=conflict`, `error_code=REVIEW_PREVIEW_MISMATCH`, `source=review_preview`,
`preview_revision`, `current_revision` y `next_action=read_current_and_reconcile`.
No inventa un `base_revision` del llamador ni solicita una nueva aprobación.

Para `write_file`, un `FileNotFoundError` al leer la preview significa ausencia
comprobada: `preview_revision=None`. Si aparece un archivo durante la revisión,
hay conflicto; si sigue ausente, la creación procede. Otros errores de lectura
no demuestran ausencia: la revisión queda sin identidad comparable y se conserva
el comportamiento anterior. Una eliminación posterior de un archivo revisado
también genera conflicto, con `current_revision=None`.

## Evidencia

19 pruebas nuevas usan archivos temporales, handlers reales y `check_edit`
sintético; el cambio durante el await lo ejecuta otro handler real. Los contadores
de escritura e historia prueban que sólo escribió el handler intermedio y que la
llamada original no añadió efectos tras el conflicto. Cubren ambos handlers,
preview estable, noop, error/unparsed fail-open, denegación, archivo ausente que
aparece o sigue ausente, eliminación y errores de lectura de preview.
Un gate con revisión efectiva reutilizada se trata igual que uno recién producido;
no se afirma haber ejercitado el cache interno real de `doubt_review`.

Comando: `venv/Scripts/python.exe -m pytest tests/test_review_preview_revision.py
tests/test_file_mutation_serialization.py tests/test_edit_base_revision.py
tests/test_edit_file_boolean_contract.py tests/test_doubt_review.py
tests/test_rewrite_policy.py -q`.
Resultado final, incluida comprobación de que el conflicto precede a la política
de reescritura: **120 correctas en 3,88 s**. Suite nueva sola antes de añadir esa
assertion: **19 correctas en 1,18 s**.
Sin modelos, servicios ni datos personales.

## Procedencia y límites

Continuación de [mutex por archivo](CODEX_H18_FILE_MUTATION_MUTEX.md), criterio H18
del [análisis fijado](CODEX_HARNESS_ANALISIS_2026-09-29.md) y fuente original
[Codex parallel.rs b1e72963](https://github.com/openai/codex/blob/b1e72963c3b71a9265a551e54beff078384efed9/codex-rs/core/src/tools/parallel.rs#L125).
Implementación propia, sin repetir revisión upstream ni radar.

No cambia la política del reviewer, los campos de confirmación ni `apply_patch`.
El guard cubre únicamente las previews efectivas con identidad conocida en estos
dos handlers. Lecturas desconocidas conservan fail-open; una lectura actual
desconocida en `write_file` tampoco permite afirmar mismatch o ausencia.
Cambiar contenido y restaurar los mismos bytes conserva su revisión: no se detecta
historia ABA. El guard no incluye identidad de inode, path, permisos o configuración
del reviewer. El mutex sigue siendo local al proceso; procesos externos, hardlinks,
cambios de resolución de rutas y otros escritores quedan fuera. La revisión puede
cambiar después de comprobarla por un escritor no participante. No hay transacción
del filesystem ni aprobación universal por contenido. H18 permanece parcial.
