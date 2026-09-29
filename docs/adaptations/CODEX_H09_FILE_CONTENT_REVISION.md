# H09 parcial: identidad de contenido capturado por FileSource

## Diferencia reproducida

`FileSource` declaraba `source_revision` como `size:int(mtime)`. Sobre un archivo
temporal se cambió `old=1` por `new=2` conservando el tamaño y restaurando mtime.
Una nueva lectura devolvía contenido distinto con la misma revisión.
Además, el recorrido real de `deliver_round`, con compilador sintético que usaba
el adapter real, seguía reutilizando el mensaje anterior. Son dos cuestiones
distintas: este lote corrige la identidad de contenido, no el comportamiento de
reutilización, cuyo snapshot de turno continúa vigente.

## Cambio

La revisión ahora es `captured_utf8_sha256:<hex>`: SHA-256 de los bytes UTF-8 del
`candidate.body` final. El hash se calcula después de construir el candidato,
porque el constructor también recorta espacios exteriores y limita el cuerpo.
`meta.revision_scope=captured_body` declara ese alcance. Tamaño y mtime permanecen
como metadata informativa, fuera de la identidad.

Los helpers existentes devuelven una proyección: normalizan CRLF, sustituyen
UTF-8 inválido y renderizan/numeran las ventanas de líneas. La identidad corresponde
exactamente a esa proyección capturada, incluyendo marcadores de truncación,
no al archivo raw o completo. No hay otra lectura, hashing del archivo completo,
nuevos accesos ni cambios de permisos o políticas de fuente.

Una lectura que devuelve texto vacío realmente capturado tiene el hash de vacío.
Una fuente retenida o un helper que devuelve `None` conserva revisión vacía y
`revision_scope=unavailable`. Los helpers no distinguen binario y fallo de lectura;
se marca `withheld=unreadable_or_binary`, sin fingir que se leyó un archivo vacío.
Los motivos previos sensitive/outside_workspace/unreadable/too_large se conservan.

## Evidencia

10 pruebas nuevas usan FileSource real y archivos temporales. Comprueban:

- Mismo tamaño y mtime con contenido distinto producen revisiones distintas.
- Contenido capturado estable con mtime distinto conserva su revisión.
- Archivo vacío capturado difiere de archivo ausente sin revisión.
- Binario, archivo demasiado grande, secreto y permiso de lectura denegado no
  reciben una revisión de contenido que no fue capturado.
- Cambios dentro de la ventana o parte no truncada cambian identidad; cambios
  fuera de la ventana o del cuerpo truncado no la cambian.
- CRLF y LF que producen el mismo cuerpo conservan la misma identidad.

Validación: `venv/Scripts/python.exe -m pytest tests/test_file_content_revision.py
tests/test_context_engine_sources.py tests/test_context_delivery_reuse_scope.py -q`:
**95 correctas en 2,37 s**. El primer intento aislado tuvo 9 correctas y una
assertion fallida: reveló que `make_candidate` recorta el texto después de la
lectura. Se movió el hash al cuerpo final y la suite conjunta verifica la corrección.
No se ejecutan modelos, servicios ni bases de datos personales.

## Fuente y límites

Continuación de [reutilización H09](CODEX_H09_LIVE_REUSE_SCOPE.md) y H09 del
[análisis fijado](CODEX_HARNESS_ANALISIS_2026-09-29.md), con referencia original
[Codex b1e72963](https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9).
Implementación propia sin repetir revisión upstream ni radar de proyectos.

`deliver_round` aún no revalida versiones de archivos antes de reutilizar. La
prueba integrada conserva esa conducta; no demuestra frescura live. Documentos,
memoria y otras fuentes no cambian su versionado. El digest no identifica inode,
permisos, resolución de ruta, raw bytes, contenido excluido o historia ABA.
Dos fuentes con la misma proyección pueden compartir digest aunque difieran sus
bytes originales. No se vincula la lectura a un snapshot atómico del filesystem;
la metadata stat puede preceder a una modificación durante lectura. H09 permanece
parcial. El guard privado de referencias file efectivamente entregadas se deja
para la siguiente revisión, separado de este incremento de identidad.
