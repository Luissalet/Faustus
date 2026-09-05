# ART-1 · Separar identidad de bytes de identidad de artefacto (B-017)

Nota de diseño previa a cualquier migración de datos. El lote ART-1 exige
diseño aprobado antes de tocar filas existentes, así que este documento fija el
modelo, el orden de migración y la frontera entre lo que ya se ha implementado y
lo que queda pendiente.

Ámbito: `src/artifact_store.py`, la tabla `artifacts` de `core/database.py`, el
nuevo contrato `src/contracts/blob.py` y el nuevo módulo `src/artifact_identity.py`.

## 1. Qué encontró la auditoría

`src/artifact_store.py::collect()` deriva el identificador lógico del contenido:

```python
"id": f"art_{digest[:24]}"
```

y `persist()` trata ese identificador como clave de idempotencia:

```python
if db.get(ArtifactRow, art.id) is not None:
    existing += 1
    continue
```

Consecuencia directa, reproducida en aislamiento por la auditoría: si Alice y
Bob —o dos runs del mismo usuario— producen exactamente los mismos bytes, la
segunda persistencia se considera "ya existente" y se descarta entera. Con ella
se pierden `owner`, `run_id`, `session_id`, `label`, `skill_id`/`skill_version`,
toda la `provenance` (modelo, licencia, receta, huella de receta, `inputs_digest`,
`seed`, `engine`, `engine_job_id`), la retención y la aprobación asociada. El
contador `Collected.deduplicated` cuenta ficheros no escritos, no ocurrencias
perdidas: nadie se entera.

El daño no es sólo de auditoría. La fila lógica pertenece al primer productor,
así que un run posterior referencia un ID cuya fila declara otro `owner`. La
deduplicación física —correcta y deseable— se convierte de facto en concesión de
acceso lógico. Hoy no hay ninguna ruta HTTP que sirva un artefacto por ID
(`path_of()` no tiene todavía consumidor en producción), por lo que el problema
es de registro y no de fuga; en el momento en que exista esa ruta y filtre por
`owner`, el bug pasa a ser de control de acceso.

Un segundo defecto, más silencioso: el nombre físico y el ID lógico están
truncados a distinta longitud. El fichero se llama `<sha256>.<ext>` y el ID es
`art_<primeros 24 hex>`. El ID no permite reconstruir el hash, sólo la fila lo
guarda en `sha256`. Cualquier alias que se construya después tiene que pasar por
la tabla.

Tercer defecto: la creación del fichero no es atómica. `collect()` hace
`os.path.exists(target)` y después `shutil.move(src, target)`. Dos escritores
con los mismos bytes pueden ver ambos "no existe" y uno pisa al otro. Con bytes
idénticos el resultado final es correcto por accidente, pero el patrón
comprobar-y-luego-escribir es el que hay que eliminar cuando la fila del blob
pase a llevar `refcount`.

## 2. El modelo que pide B-017

Tres capas, como en C-013 del mismo documento:

**Blob** — bytes inmutables, direccionados por hash, deduplicados físicamente.
Identidad = contenido: `blob_<sha256>`. Guarda `sha256`, `byte_size`,
`media_type`, `filename` (nombre desnudo dentro del store), `refcount` y
`created_at`. No tiene propietario, ni run, ni procedencia: los bytes no son de
nadie. Se crea atómicamente: la clave primaria derivada del hash es la que
resuelve la carrera de dos escritores del mismo contenido, no un `exists()`
previo.

**ArtifactOccurrence** — la ocurrencia lógica. Identidad = evento, no contenido:
`occ_<32 hex aleatorios>`. Apunta al blob por `blob_sha256` y conserva `owner`,
`project_id`, `run_id`, `session_id`, `label` (el nombre que eligió el run),
`kind`, `skill_id`/`skill_version`, `partial`, la `Provenance` completa, la
`Retention`, `approval_id` y las relaciones `variation_of`, `derived_from`,
`supersedes` y `part_of` que pide F-012. Dos ocurrencias del mismo blob son dos
filas independientes con dos procedencias distintas. Lleva además
`legacy_artifact_id`, único y anulable: es el ancla del alias descrito en §4.

**DerivedArtifact** — preview, thumbnail, proxy, waveform, subtítulo, transcode
o embedding. Regenerable por definición: borrarlo no pierde nada que no se pueda
volver a calcular.

Decisión no obvia y deliberada: **un derivado cuelga del blob, no de la
ocurrencia**. El thumbnail de unos bytes es función de esos bytes; si colgara de
la ocurrencia, dos propietarios con el mismo original generarían y almacenarían
dos thumbnails idénticos. La identidad del derivado es determinista —
`der_<fingerprint(source_sha256, derived_kind, recipe, recipe_version)>` — para
que regenerarlo sea idempotente en vez de acumular filas. El control de acceso
no se pierde: al derivado sólo se llega a través de una ocurrencia que el
llamante puede ver, nunca por el hash del original.

Regla que resume el bug: **la deduplicación física no concede acceso lógico.**
Compartir blob no es compartir artefacto. El único punto donde eso se decide es
la resolución de ocurrencia a ruta de fichero, y por eso el helper de lectura
`path_for(occurrence_id, owner=...)` exige propietario y no acepta un hash.

## 3. Cómo mapea el almacenamiento de hoy

| Hoy | Mañana |
|---|---|
| `artifacts.id = art_<hash[:24]>` | `artifact_occurrences.id = occ_<aleatorio>` + `legacy_artifact_id = art_<hash[:24]>` |
| `artifacts.sha256`, `byte_size`, `media_type`, `filename` | `artifact_blobs.sha256`, `byte_size`, `media_type`, `filename` |
| `artifacts.owner/project_id/run_id/session_id/label/kind/partial` | columnas homónimas de `artifact_occurrences` |
| columnas de procedencia de `artifacts` (`model` … `provenance_note`) | mismas columnas en `artifact_occurrences` |
| `artifacts.retention_*` | `artifact_occurrences.retention_*` |
| `artifacts.preview_filename` | `artifact_derivatives` con `derived_kind = "preview"` |
| `artifacts.legacy_gallery_id` | se queda donde está: es el puente a `gallery_images`, no al store |
| fichero `<sha256>.<ext>` en `ARTIFACT_STORE_DIR` | el mismo fichero, mismo nombre, referenciado por `artifact_blobs.filename` |

Los bytes en disco **no se mueven**. El nombre físico ya es el hash, que es
exactamente lo que el modelo de blob necesita; mover ficheros sería trabajo
gratuito con riesgo de dejar filas apuntando a rutas inexistentes.

Cardinalidad real del mapeo: `artifacts` tiene una fila por *contenido*, no por
ocurrencia, precisamente por el bug. Es decir, la migración puede reconstruir
como máximo una ocurrencia por fila existente. **Las ocurrencias ya perdidas no
se recuperan**: no quedó rastro de ellas en ninguna tabla. Lo que la migración
consigue es que no se pierda ninguna más a partir de ese punto.

Las filas importadas de la galería (`id = gal_<gallery_id>`, con
`legacy_gallery_id` no nulo) son un caso aparte: su `sha256` puede ser nulo
—`_backfill_artifacts_from_gallery()` copia `img.file_hash`, que muchas veces no
existe— y su `filename` es el nombre original de la galería, no `<sha256>.<ext>`.
Sin hash no hay blob. Esas filas se tratan en la fase 3 de §4.

## 4. Qué tendría que hacer la migración, y en qué orden

Cinco fases. Cada una es desplegable y reversible por separado; ninguna anterior
depende de la siguiente.

**Fase 1 — esquema (hecha, en esta entrega).** Crear `artifact_blobs`,
`artifact_occurrences` y `artifact_derivatives`. Aditiva pura: tres tablas
nuevas, ninguna columna tocada en `artifacts`, ninguna fila reescrita. La
reversión es `DROP TABLE` de las tres, igual que `rollback_artifacts_table()`.
El contrato y el módulo de lectura/escritura sobre las tablas nuevas también
entran aquí, sin cablear a `collect()`/`persist()`.

**Fase 2 — copia (pendiente).** Por cada fila de `artifacts` con `sha256` no
nulo: `ensure_blob()` con el hash, tamaño, `media_type` y `filename` de la fila,
e insertar una ocurrencia con `legacy_artifact_id = artifacts.id` y todos los
campos lógicos copiados. Idempotente por el índice único de
`legacy_artifact_id`, exactamente como el backfill de galería es idempotente por
`legacy_gallery_id`. `artifacts` **no se toca**: ni se borra, ni se marca, ni se
le añade una columna. Es un `INSERT`-only sobre tablas nuevas, así que una
interrupción a medias se reanuda sin limpieza previa.

**Fase 3 — filas sin hash (pendiente).** Las filas de galería sin `sha256` no
pueden producir un blob honesto. Dos opciones, y la elección no está tomada:
(a) calcular el hash leyendo el fichero, lo que exige que el fichero siga ahí y
convierte la migración en I/O sobre toda la galería; (b) crear la ocurrencia con
`blob_sha256` nulo y un motivo explícito, aceptando una ocurrencia que no
resuelve a bytes. La opción (b) obliga a hacer `blob_sha256` anulable, lo que
debilita el invariante en todas las demás filas. Preferencia actual: (a),
ejecutada como tarea de fondo reanudable y no dentro de `init_db()`.

**Fase 4 — corte de escritura (pendiente).** `collect()` deja de fabricar
`art_<hash>` y `persist()` deja de escribir en `artifacts`: pasan a
`ensure_blob()` + `record_occurrence()`. A partir de aquí, dos runs con los
mismos bytes producen dos filas. Requiere reconciliar el `refcount` una vez tras
el corte, porque las ocurrencias creadas en la fase 2 y las nuevas conviven.

**Fase 5 — corte de lectura y retirada (pendiente).** Los consumidores
(`src/media_runs.py::_collect`, los handlers de workflow, `mcp_servers/workers_server.py`)
pasan a leer ocurrencias. `artifacts` queda como tabla histórica de sólo
lectura; su borrado, si llega, es un lote posterior con su propia nota.

Orden obligatorio: **esquema antes que copia, copia antes que corte de
escritura, corte de escritura antes que corte de lectura.** Invertir cualquiera
de los tres pasos deja una ventana en la que se escriben artefactos que ninguna
lectura encuentra.

## 5. Qué se rompe si se hace mal

- **Copiar y borrar en el mismo despliegue.** Si la fase 2 borrase `artifacts`,
  una reversión de código a la versión anterior se queda sin datos. `artifacts`
  tiene que sobrevivir intacta hasta que la lectura nueva lleve tiempo en
  producción.
- **Cortar la lectura antes que la escritura.** Los consumidores buscarían
  ocurrencias que `persist()` todavía no crea: artefactos que existen en disco y
  no aparecen por ningún lado. Es el fallo más caro porque parece pérdida de
  datos.
- **Ocurrencia sin blob.** Insertar la ocurrencia antes que el blob, o en otra
  transacción, deja filas que no resuelven a bytes. `record_occurrence()` exige
  el blob y hace el `INSERT` y el incremento de `refcount` en la misma
  transacción por esto.
- **`refcount` como leer-modificar-escribir.** Dos ocurrencias simultáneas del
  mismo blob leen 3, escriben 4, y el contador queda en 4 en vez de 5. Un
  `refcount` bajo hace que el recolector borre bytes que alguien referencia. Por
  eso el incremento y el decremento son `UPDATE ... SET refcount = refcount ± 1`
  en SQL, y por eso el recolector no confía en el contador: cuenta las
  ocurrencias reales y trata la diferencia como deriva a reportar.
- **Recolectar bytes que la tabla vieja todavía nombra.** Durante las fases 2 a
  5 conviven dos censos de referencias sobre los mismos ficheros. Un recolector
  que sólo mire `artifact_blobs.refcount` borraría el fichero de un artefacto
  que `artifacts` sigue sirviendo. Por eso `collect_garbage()` sólo desenlaza
  bytes con `delete_bytes=True` y, aun así, se niega ante cualquier `sha256` o
  `filename` presente en `artifacts`.
- **Alias derivado del ID en vez de leído de la tabla.** `art_<hash[:24]>` está
  truncado: no se puede recomponer el `sha256` desde el ID. Un resolutor que lo
  intente falla en silencio para todas las filas. El alias tiene que ser una
  columna indexada (`legacy_artifact_id`), no una transformación de cadena.
- **Convertir todo adjunto en artefacto permanente.** B-021 pide unificar
  uploads con Blob/ArtifactOccurrence, pero el propio documento avisa de no
  promover automáticamente cada adjunto temporal. Un blob compartido entre un
  adjunto efímero y un artefacto con retención `keep` necesita que la retención
  se evalúe por ocurrencia, nunca por blob.

## 6. Qué entra en esta entrega

Sólo lo que no toca ningún dato existente:

- `src/contracts/blob.py`: `Blob`, `ArtifactOccurrence`, `DerivedArtifact`,
  `Relations`, `DERIVED_KINDS`. Mismas reglas que el resto de `src/contracts`:
  clave desconocida es error, nada se coacciona entre tipos, desconocido es
  `None` y no un valor plausible.
- `core/database.py`: `BlobRow`, `ArtifactOccurrenceRow`, `DerivedArtifactRow`;
  `_migrate_create_artifact_identity_tables()` en el estilo manual e idempotente
  de las migraciones existentes, llamada al final de `init_db()`; y
  `rollback_artifact_identity_tables()` como reverso explícito.
- `src/artifact_identity.py`: creación atómica de blob, alta de ocurrencia con
  `refcount` transaccional, resolución de lectura con propietario, alias
  heredado, borrado de una ocurrencia y recolector conservador.
- `tests/test_artifact_identity.py`: la matriz que exige B-017 —mismo contenido
  con dos runs, mismo contenido con dos propietarios, borrado de una ocurrencia,
  recolección y carrera simultánea— más el testigo de regresión que documenta el
  comportamiento actual de `persist()`.

`src/artifact_store.py` no cambia de comportamiento: sigue siendo la ruta de
escritura en producción hasta la fase 4.

## 7. Qué se deja deliberadamente para después

- **Las fases 2 a 5 de §4**: copia de `artifacts` a ocurrencias, hash de las
  filas de galería, y los dos cortes. Todas reescriben o reencaminan datos
  existentes, que es exactamente lo que esta nota tenía que preceder.
- **Borrado real de bytes en la recolección.** Implementado, pero con
  `delete_bytes=False` por defecto y con la negativa ante referencias heredadas
  descrita en §5. Se activa cuando `artifacts` deje de ser censo de referencias,
  es decir, después de la fase 5.
- **Regeneración de derivados.** La tabla y el contrato existen; la fábrica de
  derivados es F-029/MEDIA-1 y depende de handlers de workflow, no de este lote.
  Aquí no se genera ningún thumbnail.
- **`preview_filename` → `artifact_derivatives`.** Es parte de la copia de la
  fase 2 y hoy no tiene ningún productor, así que mover la columna ahora sería
  migrar una columna vacía.
- **Rutas HTTP por ocurrencia.** No existe hoy ninguna ruta que sirva artefactos
  por ID; añadirla es donde el control de acceso pasa a ser observable, y
  pertenece a AUTH-1 con la matriz de autorización, no a ART-1.
- **Unificación con uploads (B-021/UPLOAD-1).** Requiere acordar semántica de
  retención por ocurrencia antes de compartir blobs entre adjuntos y artefactos.
- **`schema_version` > 1 y evolución del contrato.** Todo se escribe con
  `schema_version = 1`; no hay todavía ningún lector que necesite discriminar.
