# H18 parcial: preparación de apply_patch y mutex de batch

## Diferencia reproducida

`apply_patch` preparaba el diff `a old → A old`, esperaba el gate de revisión y
escribía el texto preparado. Un doble de `check_edit` ejecutó durante ese await
otro `write_file` real que dejó `a newer` en un archivo temporal. Sin
`base_revision`, el patch devolvió éxito y sobrescribió el cambio con `A old`.
El journal capturaba los bytes actuales para compensación, pero no comprobaba
que fueran los mismos usados para preparar el patch.

## Cambio

El helper compartido `mutation_locks` normaliza rutas, deduplica sus claves,
las ordena, conserva referencias fuertes obtenidas bajo el guard del registro y
adquiere sus `RLock` en ese orden. Libera en orden inverso ante retorno o error.
`mutation_lock` individual usa la misma autoridad; el registro sigue siendo débil.
Los workers participantes adquieren su conjunto completo una vez; no se permite
envolver conjuntos con adquisiciones externas de rutas diferentes en otro orden.

El patch conserva la revisión raw de cada update/delete al preparar; add exige
ausencia comprobada mediante `lstat`, que también impide tratar un symlink colgante
como una ruta inexistente. Se rechazan operaciones duplicadas sobre una misma
ruta canónica con `DUPLICATE_PATCH_TARGET`, antes de llegar al batch: el parser y
journal anteriores no definían una composición segura de esas operaciones.
Moves siguen rechazados por el parser existente.

Después de todos los awaits de revisión, un worker adquiere todos los mutex.
Relee bytes y valida **todas** las revisiones preparadas antes de iniciar el
journal o escribir. Sólo `FileNotFoundError` representa ausencia; errores de
lectura se propagan como error de herramienta, no como un archivo ausente.
Una ruta add ocupada por un symlink colgante también se rechaza aunque no haya
bytes legibles en su referent.

El conflicto devuelve `status=conflict`, `error_code=PATCH_PREPARATION_MISMATCH`,
`source=patch_preparation`, ruta, revisión preparada/actual y existencia actual.
No se fabrica una base del llamador. El guard de preparación se aplica incluso
sin reviewer o con un reviewer fail-open: el texto preparado depende de esos
bytes independientemente de la aprobación. La política del reviewer no cambia.

Si las revisiones coinciden, `edit_journal.apply_batch` entero, sus snapshots,
escrituras y compensación ejecutan dentro de los mismos mutex. El estado parcial
y recibo de compensación existentes se conservan. Ningún lock atraviesa awaits.

## Evidencia

13 pruebas nuevas con handlers reales y archivos temporales comprueban:

- Update/add/delete obsoletos después de una revisión sintética y un write real:
  toda la llamada se rechaza, sin journal ni escrituras de patch.
- Batch válido update/add/delete y parser real rechazando duplicados literales,
  relativos, case aliases Windows y symlinks; moves permanecen no soportados.
- Dos batches con orden inverso de rutas llegan juntos al worker sin deadlock:
  uno ejecuta el journal y otro rechaza su preparación obsoleta.
- Un fallo en la segunda escritura compensa la primera mientras otro handler
  intenta adquirir su mutex. El escritor posterior entra después de compensar.
- Error de lectura de validación no se interpreta como ausencia; se liberan ambos
  mutex y handlers posteriores escriben correctamente. Adquisición anidada del
  mismo alias es reentrante.

Los fixtures no llaman modelos, servicios ni bases de datos personales.
Case aliases y symlinks se omiten donde no están disponibles; ambos pasaron aquí.

Validación final: `venv/Scripts/python.exe -m pytest tests/test_patch_mutation_serialization.py
tests/test_file_mutation_serialization.py tests/test_review_preview_revision.py
tests/test_edit_journal.py tests/test_edit_base_revision.py tests/test_edit_preservation.py
tests/test_doubt_review.py tests/test_rewrite_policy.py -q`:
**113 correctas, 2 omitidas por Windows en 4,52 s**. La suite nueva sola pasó
**13 pruebas en 1,07 s** antes de ampliar el caso de liberación a ambas rutas
y añadir existencia al payload; la integrada verifica esos cambios finales.

## Procedencia y límites

Continuación de [mutex de archivo](CODEX_H18_FILE_MUTATION_MUTEX.md) y
[preview revisada](CODEX_H18_REVIEW_PREVIEW_REVISION.md), H18 del
[análisis fijado](CODEX_HARNESS_ANALISIS_2026-09-29.md), fuente original
[Codex parallel.rs b1e72963](https://github.com/openai/codex/blob/b1e72963c3b71a9265a551e54beff078384efed9/codex-rs/core/src/tools/parallel.rs#L125).
Implementación propia sin repetir revisión upstream ni radar.

No hay transacción del filesystem ni aislamiento de otros procesos. Lecturas
ordinarias, comandos y otros handlers que no adquieren estos mutex pueden observar
o provocar estados intermedios. Hardlinks y cambios de symlinks/resolución quedan
fuera; no se capturan inodes o permisos. Restaurar los mismos bytes no detecta
historia ABA. La compensación puede fallar y conserva su recibo de incertidumbre.
El journal no recibe una nueva persistencia durable. No se crea un sistema general
de claims ni se promete paralelismo de todo el harness. H18 permanece parcial.
