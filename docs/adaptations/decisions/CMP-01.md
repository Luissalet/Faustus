# CMP-01 (sesión documental) + CMP-02 (sugerencias sin ambigüedad) + CMP-03 (revisión contextual)

- **Lote:** W2-A1 (`CONTRATO_CMP_W2.md`, ola 2 de CMP).
- **Versión base:** `master` en `95747d9`; sin git en el entorno de
  trabajo (repo montado sin `.git`) — este documento y el propio código
  son el recorrido, no hay commit que citar más allá del punto de
  partida. Ola 1 ya había añadido `src/document_comments.py` y
  `src/document_links.py` (W1-E) con sus rutas — este lote los CONSUME
  desde el frontend, no los reimplementa.
- **Ficha origen:** `INFORME_COMPARATIVO_V2.md` §3.1 (CMP-01, la mitad de
  "sesión documental compartida" — la mitad de "disposición/layout" es
  `CMP-01-layout.md`, W2-A2, en paralelo) y §3.2 (CMP-02, CMP-03).

## Qué pedía el informe

1. **CMP-01** — una sesión documental por `docId` compartida por el panel
   (`SidePanel.tsx`'s `DocTab`) y el editor completo (`Editor.tsx`):
   identidad estable, revisión base, borrador, selección como rangos,
   undo/redo, propuestas pendientes. Cambiar de panel a editor completo, a
   otra conversación y volver conserva selección, borrador y propuestas.
   Un fichero sin cambios nunca se reescribe por alternar modos.
2. **CMP-02** — `applySuggestion` deja de aplicar la primera ocurrencia de
   `find` a ciegas: ante ambigüedad, muestra las ocurrencias con contexto
   y el usuario elige (o "todas").
3. **CMP-03** — seleccionar texto → referencia `{doc, rangos, cita}` para
   el compositor (nunca copiada como si fuera del usuario); comentarios
   anclados con revisión (abrir, resolver, aceptar propuesta, huérfano
   visible); "enviar comentarios como tarea al agente" sin ejecutar
   cambios por sí solo.

## Solución actual (antes de este cambio)

- `SidePanel.tsx`'s `DocTab` guardaba el borrador en `PanelState.drafts`
  (`studio/src/screens/studio/panel.ts`), una tabla por `docKey`/`fileKey`
  que vive dentro del estado de React de `Studio.tsx` — se pierde por
  completo al desmontar ese árbol (navegar a `/documents/{id}` es OTRA
  ruta, otro árbol montado).
- `Editor.tsx` tenía su propio `useState` de texto, poblado únicamente por
  `getDoc(id)` al montar — sin ningún conocimiento de un borrador que el
  panel pudiera tener para el mismo documento.
- `applySuggestion` en `DocTab`: `next.includes(sg.find)` /
  `next.replace(sg.find, sg.replace)` — sustituye la PRIMERA aparición;
  con texto repetido (el caso decisivo del informe) aplica al párrafo
  equivocado sin avisar.
- No existía ningún mecanismo de "referencia de selección al compositor"
  ni una superficie de revisión para los comentarios que W1-E ya permite
  crear vía API (`/api/documents/{id}/comments`) — la única forma de
  verlos era una llamada manual a la API.

## Solución de referencia (`INFORME_COMPARATIVO_V2.md` §3.1-3.2)

OpenKnowledge: identidad de selección estable (documento/superficie/
fragmento), edición visual con agentes al lado. Herdr Studio: inspector
acoplable que restaura selección y disposición del recurso; anotación de
pasajes con anclaje desactualizado visible y compilación de feedback para
un agente. Ninguno de los dos es código de este repo (referencia
descriptiva del informe, no un repositorio auditado línea a línea).

## Mecanismo concreto de diferencia

### CMP-01 — `studio/src/lib/docSession.ts`

Un **singleton a nivel de módulo** (`Map<docId, DocSessionState>`), no
estado de componente: `getSession`/`setDraftText`/`sync`/`markSaved`/
`undo`/`redo`/`setSelection`/`mergeSuggestions` mutan una entrada y
notifican a sus suscriptores (`useSyncExternalStore`). Como el módulo
sigue cargado mientras dure la página, la sesión sobrevive exactamente a
las tres navegaciones que pide la prueba decisiva:

- **Panel → editor completo → panel**: son dos árboles de React distintos
  montados en rutas distintas, pero ambos llaman a
  `useDocSession(docId, ...)`, que lee la MISMA entrada del `Map`.
- **A otra conversación y vuelta**: `panelReducer`'s `session-switch`
  (`panel.ts`, sin tocar) sigue vaciando `PanelState` (las pestañas
  abiertas, que es su responsabilidad), pero `docSession` vive fuera de
  `PanelState` — si el usuario vuelve a abrir el MISMO `docId` más tarde
  (misma conversación u otra), `getSession(docId)` devuelve el borrador,
  la selección, el undo/redo y las propuestas tal como estaban.
- **Borrador persistente entre recargas**: además del `Map` en memoria,
  cada borrador "sucio" (`draftText !== null && draftText !== baseText`)
  se escribe en `localStorage` (`faustus.docSession.draft.<docId>`),
  envuelto en `try/catch` (modo privado/cuota llena degradan a "solo esta
  carga de página", nunca lanzan). Un documento SIN borrador no ocupa fila
  en `localStorage` — así "sin cambios" es observable desde fuera sin
  leer el JSON.
- **`isDirty(session)`** es el único punto que ambas superficies consultan
  para habilitar `Save` y para decidir si llamar a `saveDoc`. Cambiar de
  pestaña/vista NUNCA llama a `setDraftText` ni a `save()`, así que un
  documento sin cambios no se reescribe por alternar modos — verificado
  por fuente en `tests/test_cmp01_doc_session_js.py`
  (`test_never_rewrites_an_unchanged_document_on_a_mode_switch`) y por
  construcción en `doc_session.check.mjs` (la sección "A document nobody
  touched is never dirty across a mode switch").
- **`sync(docId, server)`**: cuando el servidor manda contenido nuevo (SSE
  `doc_update`, una versión restaurada, un `getDoc` fresco), SIN borrador
  pendiente el session simplemente sigue al servidor; CON borrador
  pendiente el borrador se conserva intacto y solo `baseText`/
  `baseRevision` avanzan — la regla "tu borrador se conserva; revisa antes
  de guardar" que `SidePanel.tsx` ya tenía, ahora también en `Editor.tsx`.
  Un flag nuevo, `rebasedPending`, es cómo cada superficie sabe si debe
  mostrar ese aviso (antes vivía como una comparación ad-hoc
  `draft.base !== doc.content` solo en el panel).
- **`markSaved`** es el único punto que limpia el borrador tras un guardado
  real — reemplaza tanto `dispatch({type:'draft-saved',...})` (panel, solo
  para documentos: `FileTab` lo sigue usando para ficheros del workspace,
  sin tocar) como el `setDoc(saved)` a secas del editor.
- **Selección como rangos** (`DocRange {start,end}[]`), no el texto
  seleccionado — capturada desde `selectionStart`/`selectionEnd` del
  `<textarea>` (modo edición) en AMBAS superficies. La vista previa
  (`<Rich text={text}/>`, HTML renderizado) no tiene un mapeo trivial de
  vuelta a offsets de la fuente Markdown — **límite documentado abajo**,
  no implementado en este lote.
- **Undo/redo explícito** (`undoStack`/`redoStack`, tope 50): no sustituye
  el undo nativo del navegador para TECLEAR (`setDraftText(id, text)` sin
  `record` no apila nada — cada pulsación seguiría rompiendo el undo
  nativo de un `<textarea>` controlado de todas formas, algo que YA era
  cierto antes de este lote). Donde sí aporta: las ediciones programáticas
  (aplicar una sugerencia, aceptar una propuesta de comentario, el
  formato Markdown de `Editor.tsx`, "reemplazar todo") pasan `{record:
  true}` y quedan deshacibles — antes, ninguna de ellas lo era.

### CMP-02 — ocurrencias, nunca la primera

`findOccurrences(text, needle)` (mismo `_CTX_CHARS`/40 que
`src/document_comments.py::locate_quote`, reimplementado en TS puro —
sin importar Python, sin nueva dependencia) devuelve TODAS las
apariciones con su contexto. `applySuggestion` en `DocTab`:

- 0 ocurrencias → aviso "ya no está en el documento" (como hoy).
- 1 ocurrencia → aplica directamente.
- >1 ocurrencias → `OccurrencePicker`: lista cada aparición con 40
  caracteres de contexto a cada lado, botón por aparición y "Aplicar a
  todas las apariciones" (reemplaza cada una por igual, sin adivinar cuál
  se quería). Nunca aplica la primera en silencio.

"Apply all" (aplicar TODAS las sugerencias pendientes) recorre la lista en
orden usando la misma lógica; la primera sugerencia que resulte ambigua
DETIENE el lote y abre su selector — el modo masivo tampoco adivina.

**Límite conocido, documentado en el propio código** (`docSession.ts`,
comentario de cabecera de la sección "Selección -> composer, comentarios
-> composer"): el contrato `anchor` (quote+before+after) que el informe
sugiere para que la SUGERENCIA del agente traiga su propio
desambiguador automático depende de que `adapters/chat.ts`'s
`DocSuggestion` (y el evento `doc_suggestions` del backend) lleven ese
campo — `chat.ts` no está en el lote de W2-A1 (lo toca W2-D esta ola para
`context_receipts`), así que no se tocó. La UI de ocurrencias cubre el
caso — el usuario SIEMPRE puede desambiguar — pero no hay auto-resolución
por anclaje cuando el agente ya sabía cuál aparición quería decir. Punto
para el orquestador más abajo.

### CMP-03 — referencia de selección y comentarios, nunca instrucción

- **Backend ya existe (W1-E, ola 1):** `src/document_comments.py` +
  `routes/document_comments_routes.py` — anclaje cita+contexto,
  reubicación al guardar, `accept()` que falla cerrado
  (`document_comments.base_changed`, 409) si el anclaje ya no es
  inequívoco. Este lote NO reimplementa nada de eso: `adapters/
  documents.ts` añade `listDocComments/createDocComment/
  updateDocComment/deleteDocComment/acceptDocComment` (más
  `listDocLinks/listDocBacklinks` para `src/document_links.py`) como
  clientes finos, igual que `docFrom` para el propio documento.
- **`ReviewPane.tsx`** (nuevo): lista de comentarios (filtro abierto/
  todos), cada uno con cita, cuerpo, autor, y si trae propuesta un diff
  en contexto con "Aceptar propuesta" (llama a `acceptDocComment`; un 409
  se muestra como aviso, nunca como "aplicado a medias"); "Resolver" sin
  aplicar; huérfano (`state==='orphan'`) visible como "anclaje perdido —
  reubicar a mano", nunca reubicado a otro párrafo en silencio (mismo
  vocabulario que el propio `document_comments.py`). Selección múltiple
  + "Enviar N comentarios al agente como tarea".
  Compacta además `Links`: salientes (`listDocLinks`, con su `status`:
  resuelto/ambiguo/roto/rechazado) y "documentos que enlazan aquí"
  (`listDocBacklinks`).
- **Selección → compositor y comentarios → compositor, sin
  `Composer.tsx`**: `docSession.ts` expone `COMPOSER_CONTEXT_EVENT`
  (`'faustus:composer-doc-context'`) y `sendComposerContext(detail)` —
  un `CustomEvent` en `window` cuyo `detail` es exactamente
  `{doc:{id,title}, ranges, action, items:[{quote,note?}]}`. `DocTab`'s
  chip "Sobre esta selección…" (con las tres acciones rápidas del
  informe: Aclara esto / Mantén este término / Corrige esto) y
  `ReviewPane`'s "enviar como tarea" lo disparan; NINGUNO copia el texto
  al chat como si el usuario lo hubiera tecleado, y ninguno ejecuta un
  cambio por sí solo — es contexto para el PRÓXIMO mensaje, que el humano
  sigue teniendo que escribir y enviar. Se eligió un evento (no tocar
  `Composer.tsx` ni `adapters/composer.ts`, ambos fuera del lote de
  W2-A1 y activamente en manos de W2-F esta ola) sobre extender
  `mentions.ts` (ese mecanismo son tokens `@ruta` de texto plano del
  WORKSPACE, resueltos por `src/file_mentions.py` — una cita+rango de un
  documento no es una ruta de fichero, forzarlo ahí habría sido
  reutilizar la forma sin el significado). **Punto a cablear por el
  orquestador**: `Composer.tsx` necesita un `useEffect` que escuche
  `COMPOSER_CONTEXT_EVENT` y pinte el chip/adjunto — no se cableó aquí
  porque ese fichero es de W2-F.

## Cobertura

**Presente** (implementada y verificada en este lote):

- Sesión documental compartida (identidad/base/borrador/selección/
  undo-redo/propuestas) entre `DocTab` y `Editor.tsx`, con
  `isDirty`/`sync`/`markSaved` como único camino de guardado.
- `applySuggestion` sin aplicar la primera ocurrencia a ciegas —
  selector de ocurrencias con contexto, "aplicar a todas".
- `ReviewPane.tsx` real (no el stub que `CMP-01-layout.md`, W2-A2,
  documenta haber creado en su ausencia): lista de comentarios,
  resolver, aceptar propuesta con diff en contexto, huérfano visible,
  enlaces salientes/entrantes.
- Evento tipado `COMPOSER_CONTEXT_EVENT` para selección/comentarios →
  compositor, documentado como el contrato que W2-F debe consumir.

**Parcial**:

1. La selección solo se captura con precisión de rango en modo
   TEXTAREA (edición); en la vista previa (HTML renderizado) no hay
   mapeo de vuelta a offsets de la fuente — seleccionar ahí no ofrece el
   chip. El texto del informe ("seleccionar sobre el documento o diff")
   se cumple para el caso de edición, que es donde CMP-02/CMP-03 son más
   accionables (hay que poder editar el `find` alrededor); la vista
   previa queda como hueco documentado.
2. El `anchor` (quote+before+after) que permitiría que una SUGERENCIA
   del agente traiga su propia desambiguación automática no está
   cableado — depende de `adapters/chat.ts`'s `DocSuggestion` (fuera de
   este lote esta ola). El usuario siempre puede desambiguar a mano vía
   `OccurrencePicker`; falta la vía automática.
3. `COMPOSER_CONTEXT_EVENT` no tiene todavía un oyente en `Composer.tsx`
   — el evento se dispara pero nada lo pinta aún. Frontend-only por
   diseño de este lote (contrato del encargo: no editar `Composer.tsx`).

**No verificado**: comportamiento con lector de pantalla real del
`OccurrencePicker` y del `ReviewPane` (se siguieron los patrones ARIA ya
usados en el resto de Studio — `role="group"`, `role="radio"` en filtros
— pero no se probó con un lector de pantalla real).

## Estado comparativo

**Ventaja propia** en el mecanismo de sesión compartida: la referencia
del informe (OpenKnowledge/Herdr Studio) describe el COMPORTAMIENTO
deseado (selección/borrador que sobreviven), no un mecanismo de
implementación auditable; aquí el mecanismo es un singleton de módulo +
`useSyncExternalStore`, con la prueba explícita (`doc_session.check.mjs`)
de que dos "superficies" leyendo el mismo `docId` obtienen literalmente
el mismo objeto de sesión. **Hipótesis de mejora, no medida contra
producto real**: el resto (CMP-02/CMP-03) es una especificación de
comportamiento del informe, no un repositorio de terceros con el que
diferenciar línea a línea — se evaluó contra los criterios de aceptación
explícitos del propio informe (mostrar ocurrencias en vez de adivinar;
contexto sin ejecutar cambios), no contra código externo.

## Decisión

**Extender**, no sustituir: `fileConflict.ts` (three-way) y el guardado
con `expected_content`/versión base de `saveDoc` siguen intactos —
`docSession.ts` decide QUÉ texto y QUÉ `baseText` se mandan a `saveDoc`,
nunca cómo se resuelve un 409. **Componer** con W1-E
(`document_comments.py`/`document_links.py`): `ReviewPane`/`adapters/
documents.ts` son clientes nuevos sobre un backend ya construido y
probado, cero reimplementación. **Integrar** (no descartar)
`PanelState.drafts`: sigue siendo la fuente de verdad para `FileTab`
(ficheros del workspace) — solo los DOCUMENTOS migraron a `docSession`.

## Pruebas ejecutadas

Desde `/home/claude/faustus`:

```
$ node studio/checks/doc_session.check.mjs
ok: ... (46 comprobaciones)
ALL OK

$ python3 -m pytest tests/test_cmp01_doc_session_js.py -q -p no:cacheprovider -W ignore
7 passed

$ python3 -m pytest tests -q -p no:cacheprovider -W ignore -k "document or side_panel or editor"
(ver informe final)

$ python3 -m pytest tests/test_studio_guards.py -q -p no:cacheprovider -W ignore
11 passed

$ ./node_modules/.bin/tsc --noEmit -p tsconfig.json
studio/src/i18n/es.ts(3737,3): error TS1117 (clave "Recipe" duplicada — de
  otro lote concurrente de esta misma ola, W2-F; no reproducible desde
  ningún fichero de W2-A1)
studio/src/screens/studio/model.ts(665,54): error TS2366 (preexistente,
  fichero fuera de este lote)
studio/src/screens/workflows/NodeInspector.tsx(72,19): error TS18047
  (de W2-E, concurrente, fuera de este lote)
  — ninguno de los tres referencia un fichero de W2-A1; el orquestador
    los resolverá en la consolidación final (regeneración de `es.tsv`
    incluida).

$ ./node_modules/.bin/vite build
✓ built in 26.59s   (mismo aviso preexistente de chunk > 500kB; incluye
                      el chunk nuevo ReviewPane-*.js sin error)

$ python3 scripts/i18n_es.py
es.ts: 6802 strings   (39 filas nuevas añadidas al final de
                        docs/ui/i18n/es.tsv por este lote)
```

## Puntos a cablear por el orquestador

1. **`Composer.tsx` (W2-F)**: añadir un oyente de `COMPOSER_CONTEXT_EVENT`
   (`studio/src/lib/docSession.ts`) que pinte el chip/adjunto de contexto
   con `{doc, ranges, action, items}` — hoy el evento se dispara pero
   nada lo consume todavía.
2. **`adapters/chat.ts`'s `DocSuggestion` (W2-D toca este fichero esta
   ola)**: si se quiere que una sugerencia del agente traiga su propio
   `anchor` (quote+before+after, contrato de `document_comments.py`) para
   desambiguar automáticamente sin mostrar el `OccurrencePicker`, añadir
   un campo opcional `anchor?: {quote,before,after}` a `DocSuggestion` y
   al evento `doc_suggestions` del backend; `findOccurrences`/`replaceAt`
   en `docSession.ts` ya están listos para consumirlo.
3. **`docs/ui/i18n/es.tsv`**: tiene una clave duplicada (`Recipe`,
   aparecida por trabajo concurrente de otro lote de esta ola) que rompe
   `tsc --noEmit` (no `vite build`, que usa esbuild y tolera la
   duplicación silenciosamente) — deduplicar en la regeneración final.
4. **Vista previa (modo no-edición)**: la selección con rango preciso solo
   funciona en el `<textarea>`; extenderla a `<Rich text=.../>` requeriría
   mapear una `Selection` del DOM de vuelta a offsets de la fuente
   Markdown — no intentado aquí (ver Cobertura, parcial 1).
