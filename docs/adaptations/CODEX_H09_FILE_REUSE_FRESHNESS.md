# H09 parcial: revalidación de archivos entregados antes de reutilizar

## Diferencia y cambio

La [identidad de FileSource](CODEX_H09_FILE_CONTENT_REVISION.md) detecta cambios
en su proyección capturada, incluso con tamaño/mtime iguales. `deliver_round`
aún reutilizaba el paquete si coincidían ámbito/política/solicitud y presupuesto,
sin consultar esa identidad. Este incremento añade un guard para los items
`source_type=file` efectivamente incluidos en secciones entregadas; no recorre
todos los refs explícitos o fuentes omitidas.

Cada entrega conserva `_file_reuse_receipts`, una tupla privada e inmutable de
referencia y revisión `captured_utf8_sha256`. No guarda contenido, descripciones
ni credenciales, y no se incluye en mensaje, metadata pública o reporte.
Excluye `recent_messages`, que el renderer no entrega en este bloque. Conserva
como máximo 40 pares distintos; por encima del límite marca continuidad no
reutilizable, sin validar sólo un prefijo de un paquete mayor.

Después de comprobar ámbito y presupuesto, el guard relee esos refs mediante
`FileSource.fetch` real y la solicitud actual: usa las mismas guardas de workspace,
contenido sensible y política, sin implementar otro lector. Cuando las revisiones
coinciden, conserva el mismo mensaje byte por byte y los recibos. Un cambio,
desaparición, contenido retenido, error o validación no disponible obliga a
compilar otra vez. La validación tiene el deadline existente `timeout_s`; la
compilación conserva su deadline separado. Son dos fases acotadas, no una promesa
de que su tiempo conjunto siga siendo el deadline de una sola compilación.

Un archivo vacío capturado tiene digest legítimo y también se valida. Un ref
entregado sin digest comparable (ausente/retenido/identidad anterior) fuerza
recompilación; así puede descubrir disponibilidad posterior. Los paquetes antiguos
sin el backing privado también recompilan: no se deduce ausencia de archivos a
partir de `report.sources`, cuya lista pública está truncada a 40 filas.

Sin archivos, el backing es tupla vacía y no añade lecturas. Motor desactivado,
ámbito distinto o presupuesto insuficiente no ejecutan la revalidación. El estado
es local a cada entrega, sin «últimos refs» globales compartidos por consumers.

## Evidencia

16 casos nuevos usan entrega real con compilador sintético que consume FileSource
real sobre archivos temporales. Comprueban cambio con stat igual, contenido estable,
refs no entregados sin lecturas, desaparición/binario/permiso denegado, cambio fuera
y dentro de ventana, cola truncada no capturada, motor apagado, gate de política
sin lectura de contenido, consumidores concurrentes, identidad legacy ausente,
overflow, excepción durante validación, archivo vacío que desaparece y referencia
ausente que aparece. El caso legacy sustituye el reporte por 40 fuentes memory:
la entrega vuelve a compilar sin intentar certificar ni reconstruir refs del prefijo.
Los recibos privados no aparecen en reporte ni mensaje.

Validación: `venv/Scripts/python.exe -m pytest tests/test_file_reuse_freshness.py
tests/test_file_content_revision.py tests/test_context_delivery_reuse_scope.py
tests/test_context_engine_wiring.py -q`: **79 correctas en 2,88 s**. Sin modelos,
servicios ni bases de datos personales. Las comprobaciones previas de ámbitos
siguen pasando, sin repetir su implementación.

## Fuente y límites

Continuación de [identidad de archivo](CODEX_H09_FILE_CONTENT_REVISION.md),
[ámbito de reutilización](CODEX_H09_LIVE_REUSE_SCOPE.md) y H09 del
[análisis fijado](CODEX_HARNESS_ANALISIS_2026-09-29.md), referencia original
[Codex b1e72963](https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9).
Implementación propia sin repetir revisión upstream ni radar de proyectos.

La revisión corresponde a la proyección capturada por FileSource antes de
transformaciones posteriores del packet o renderizado. El guard puede invalidar
conservadoramente cambios en partes de esa proyección que ya no queden visibles
tras reducir el item: no se presenta como hash exacto del fragmento final visible.
Cambios fuera de la ventana capturada/truncación permanecen fuera del digest.

No se versionan documentos, memoria u otras fuentes; no se inventan revisiones de
doc chunks. No hay snapshot atómico del filesystem entre validación y envío, y
los bytes pueden cambiar después. No se certifican inode, permisos, configuración,
raw bytes o historia ABA. Recompilar puede volver a recibir contenido degradado;
si falla, se conserva el fallback previo del caller, sin retirar información que
el modelo ya recibió. El backing es efímero, no un índice durable. H09 sigue parcial
y el motor permanece desactivado por defecto.
