# H18/H16 parcial: mutex de mutación para edit_file y write_file

## Carrera reproducida

Dos llamadas reales a `edit_file` con la misma `base_revision` sobre un archivo
temporal `a b` leyeron la revisión original mediante una barrera en los workers.
Una escritura `a→A` precedió a otra `b→B`, ordenadas mediante un evento.
Ambas devolvieron éxito; el resultado `a B` perdió el primer cambio. El comentario
que atribuía exclusión a una sola llamada `asyncio.to_thread` era incorrecto:
distintos threads pueden entrar en ella a la vez.

`FileLockRegistry` existente controla propiedad en una delegación, permite al
mismo worker varias llamadas y no es un mutex de sección crítica.
`resource_ownership` añade leases sobre esa propiedad. `edit_journal` registra
y compensa batches, sin excluir otros escritores. No se cambian esas autoridades.

## Incremento implementado

`file_mutation_locks.mutation_lock` ofrece exclusión en este proceso, compartida
por los workers de `edit_file` y `write_file`. La clave se obtiene con
`normcase(realpath(abspath(path)))`; en Windows normaliza mayúsculas y resuelve
aliases de symlink actuales. El registro débil está protegido por un guard:
un usuario conserva una referencia fuerte mientras espera, entra y libera;
las rutas sin usuarios desaparecen. No crece indefinidamente por archivos usados.

Cada closure adquiere su mutex antes de leer y lo conserva durante el cálculo de
revisión, la comprobación de `base_revision` y la escritura. El segundo escritor
ve el cambio y devuelve el conflicto existente `BASE_REVISION_MISMATCH` cuando
comparte una base antigua. Los retornos tempranos y excepciones liberan el mutex.
El comentario del handler refleja ahora esta autoridad real.

La revisión previa y los awaits siguen fuera del mutex. Historia, presentación
y demás hooks posteriores también quedan fuera; el cambio no afirma su orden
global ni altera sus políticas. Archivos diferentes pueden progresar a la vez.

## Validación

Once pruebas nuevas usan handlers reales, archivos temporales y hooks aislados.
Los cuatro pares edit/edit, edit/write, write/edit y write/write fuerzan que el
segundo worker intente adquirir mientras el primero conserva su lectura:
el primero tiene éxito, el segundo observa la nueva revisión y devuelve conflicto.
Una barrera en archivos distintos prueba entrada simultánea en ambos workers.
Se comprueba identidad de mutex con case aliases de Windows y symlinks, liberación
tras excepción directa y fallos sintéticos de escritura de ambos handlers, y
desaparición de 100 rutas sin usuarios del registro débil. Las pruebas de symlink
se omiten cuando el sistema no permite crearlos; en esta ejecución pasaron.

Comando: `venv/Scripts/python.exe -m pytest tests/test_file_mutation_serialization.py
tests/test_edit_file_boolean_contract.py tests/test_edit_file.py
tests/test_edit_base_revision.py tests/test_edit_preservation.py -q`.
Resultado sobre el código final: **60 correctas, 2 omitidas por Windows, 3,03 s**.
Las omisiones históricas son ejecutable POSIX y demostración de traducción de EOL.
El primer intento de la nueva suite tuvo cuatro assertions incorrectas que
esperaban `conflict=True`; se corrigieron al contrato vigente `status=conflict`
y no se cuenta ese intento como validación exitosa.

## Fuente y límites pendientes

Implementación propia basada en el alcance H18 del
[análisis fijado](CODEX_HARNESS_ANALISIS_2026-09-29.md) y su referencia original
[Codex parallel.rs b1e72963](https://github.com/openai/codex/blob/b1e72963c3b71a9265a551e54beff078384efed9/codex-rs/core/src/tools/parallel.rs#L125).
No se repite revisión upstream ni el radar de proyectos.

- `apply_patch`, su preparación, journal y compensación aún no participan.
  Requieren adquisición ordenada de todas las rutas y revalidación del contenido
  preparado antes de escribir; bloquear únicamente su escritura sería insuficiente.
- Otros procesos, editores, comandos, otros handlers y hardlinks no participan.
  No se proporciona transacción del filesystem ni atomicidad entre archivos.
- La identidad depende de la resolución de ruta al adquirir. Cambios de symlinks
  o sustituciones de rutas mientras se ejecuta siguen fuera de la garantía.
- Una preview aprobada por doubt review puede quedar obsoleta durante su await.
  El mutex no vincula esa aprobación al contenido; este lote no cambia esa política.
- Sin `base_revision`, los dos handlers se serializan pero conservan sus propias
  reglas actuales de edición/sobrescritura. No se convierte ausencia en conflicto.
- Historia posterior y recibos no son una transacción con la escritura.

H18/H16 permanecen parciales. No se ejecutan modelos, servicios ni datos personales.
