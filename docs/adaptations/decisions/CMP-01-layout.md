# CMP-01-layout — Tres disposiciones del mismo trabajo (Studio)

- **Lote:** W2-A2 (`CONTRATO_CMP_W2.md`, ola 2 de CMP).
- **Versión base:** `master` en `95747d9`; sin git en el entorno de trabajo
  (repo montado sin `.git`), así que este documento y el propio código son
  el recorrido — no hay commit que citar más allá del punto de partida.
- **Ficha origen:** `INFORME_COMPARATIVO_V2.md` §3.1 (CMP-01), la parte de
  disposición/layout — CMP-01/02/03 en conjunto cubren "sesión documental
  compartida" (W2-A1) y "tres disposiciones" (este documento, W2-A2). W2-A1
  posee `docs/adaptations/decisions/CMP-01.md` para su parte; este fichero
  es la mitad de layout, tal como nombra `CONTRATO_CMP_W2.md`.

## Qué pedía el informe

Un conmutador en la cabecera del Studio (más un atajo) entre tres
arreglos del mismo trabajo — la MISMA conversación, el MISMO documento
activo del panel, sin segunda copia de ninguno de los dos:

1. **`conversation`** — la disposición de hoy: conversación al centro,
   panel lateral opcional.
2. **`document`** — el documento activo del panel ocupa el centro, la
   conversación pasa a una columna lateral estrecha, "el resto del panel
   en una tercera zona opcional".
3. **`review`** — documento + `ReviewPane` (W2-A1, CMP-01/02/03: hilos de
   comentarios anclados, aceptar sugerencia con diff, "enviar comentarios
   como tarea al agente").

Cambiar de disposición no debía crear otra conversación, documento ni
borrador.

## Solución actual (antes de este cambio)

Studio solo tenía una disposición: `.fs-studio` es una rejilla de 2 o 3
columnas (`264px` sesiones · `1fr` conversación · panel lateral opcional,
`studio/src/screens/studio.css`), sin ningún concepto de "documento al
centro" — el documento activo (`panel.doc`, `studio/src/screens/studio/
panel.ts`) solo podía verse dentro del panel lateral, siempre estrecho
(`minmax(320px, min(var(--fs-panel-width,520px), 50vw))`), nunca dominante.
No existía atajo ni conmutador de disposición, ni `ReviewPane` (ese
componente es de W2-A1, CMP-01/02/03, en paralelo con este lote).

## Solución de referencia (INFORME_COMPARATIVO_V2.md §3.1)

Tres vistas explícitas y nombradas del mismo estado — conversación,
documento, revisión —, con conmutador visible y atajo de teclado, y la
garantía explícita de que cambiar de vista es una operación de
presentación, no de datos (no dispara guardado, no crea nada).

## Mecanismo concreto de diferencia

- **Nuevo estado `layout ∈ {conversation, document, review}`**
  (`studio/src/screens/Studio.tsx`, tipo `StudioLayout`), persistido por
  sesión en `localStorage` bajo `faustus_studio_layout:<sessionId|'new'>`
  (`readLayoutFor`/`writeLayoutFor`) — mismo patrón que el borrador
  (`DRAFT_PREFIX`) y los adjuntos pendientes (`ATTACHMENTS_PREFIX`) que ya
  usaba este fichero: una ranura por conversación, sin ranura para el valor
  por defecto (`conversation` nunca escribe clave).
- **`setLayout`** es el ÚNICO punto donde la UI cambia de disposición.
  Hace dos cosas y nada más: `setLayoutState(next)` y, si `next !==
  'conversation'`, `panelDispatch({type:'open', tab:'doc'})` — el mismo
  mecanismo que ya usaba el botón "Control de código fuente" de la
  cabecera (`panelDispatch({type:'open', tab:'git'})`). No llama a
  `createSession`, `createDoc`, `ensureSession`, `uploadFiles`, `navigate`,
  `setParams` ni ningún `save` — verificado por fuente en
  `studio/checks/studio_layout.check.mjs` (extrae el cuerpo exacto de la
  función y comprueba que ninguna de esas llamadas aparece).
- **Sin duplicar estado**: `layout` es una cadena, nada más. El documento y
  la conversación que las tres disposiciones muestran siguen siendo
  `panel`/`turns` tal cual — `Studio.tsx` no clona `panel.doc` ni `turns`
  para las vistas nuevas, solo cambia cómo se disponen los mismos hijos de
  React en la rejilla.
- **Rejilla, no reescritura**: `grid-template-columns` gana dos variables
  CSS nuevas, `--fs-stage-col` y `--fs-side-col` (`studio.css`), con
  *fallback* al valor de cada punto de ruptura existente cuando no hay
  override — así las 6 reglas de rejilla que ya existían (sesiones abiertas
  / ocultas, panel abierto / superpuesto / cerrado, ≥1280px / <1280px) no
  se han tenido que triplicar; solo se añadió una regla que fija esas dos
  variables cuando `[data-layout='document']` o `[data-layout='review']`
  coincide con `[data-panel]`, y una cuarta columna (`review`) para
  `ReviewPane`. Por debajo de 1280px el panel ya era una capa superpuesta
  (`position: absolute`), no una columna de rejilla — ahí `layout` no tiene
  efecto visual y la conversación completa sigue siendo la vista por
  defecto (límite documentado más abajo, no un error).
- **Conmutador**: tres botones `role="radio"` dentro de un
  `role="radiogroup"` en la cabecera (`fs-studio__layout-switch`, variante
  de tres posiciones de `fs-studio__seg`, cuyo thumb ya estaba fijado a
  50%/50% y no servía para tres). Atajo `Ctrl+Alt+L` (cicla las tres) con
  su propio listener — **no** se añadió a `adapters/settings.ts`
  (`DEFAULT_KEYBINDS`/`KEYBIND_LABELS`) porque ese fichero no está en el
  lote de W2-A2 (`CONTRATO_CMP_W2.md`: "tú eres el único en
  `Studio.tsx`/`studio.css`"); documentado como punto a cablear si se
  quiere que el atajo sea configurable como los demás.
- **`ReviewPane`**: importado perezosamente desde
  `studio/src/screens/documents/ReviewPane.tsx` (`lazy(() =>
  import('./documents/ReviewPane').then(m => ({default: m.ReviewPane})))`
  — exportación nombrada, no por defecto). Ese fichero es de W2-A1
  (CMP-01/02/03). Se comprobó su existencia antes de importar, tal como
  pide el contrato; **no existía** en el momento de este lote, así que se
  creó un stub mínimo (`export function ReviewPane(){return null}`) con un
  comentario explicando que W2-A1 lo sustituye entero. **Aviso para el
  orquestador**: cuando W2-A1 publique el fichero real, confirmar que
  conserva una exportación nombrada `ReviewPane` y que su firma de props
  sigue aceptando (al menos) `{docId: string | null}` — si cambia, este
  fichero (la llamada en `Studio.tsx`) necesita un ajuste de una línea.

## Cobertura

**Presente** (implementada y verificada en este lote): conmutador de tres
disposiciones, persistencia por sesión, atajo de teclado, rejilla que
reordena sin duplicar estado, importación perezosa de `ReviewPane` con
coordinación de stub documentada.

**Parcial** (limitación conocida, documentada, no bloqueante):

1. "El resto del panel en una tercera zona opcional" (modo `document`) —
   el informe permite explícitamente que sea opcional. No se implementó:
   `SidePanel.tsx` es de W2-A1 y mezcla en un solo árbol de React la
   pestaña activa (`doc`, `git`, `file`, `board`…) con su propia barra de
   pestañas; separar "contenido del documento" de "resto del panel" exige
   tocar ese componente, fuera del alcance de este lote
   (`CONTRATO_CMP_W2.md`: "SOLO layout... W2-D/W2-F NO tocan Studio.tsx",
   y simétricamente este lote no toca `SidePanel.tsx`). En su lugar, en
   modo `document`/`review` el panel ENTERO (cualquiera que sea su pestaña
   activa) ocupa la columna dominante — normalmente será la pestaña `doc`
   porque `setLayout` la abre explícitamente, pero si el usuario cambia de
   pestaña dentro del panel después, ve esa otra pestaña a tamaño grande,
   no un documento fijo con una tercera zona aparte.
2. Por debajo de 1280px de ancho, el panel ya era una capa superpuesta
   (`position: absolute`) antes de este lote; `data-layout` no tiene
   ningún efecto de rejilla ahí (documentado en `studio.css` junto a la
   regla) — la disposición se comporta como `conversation` en pantallas
   estrechas. No es una regresión: nunca hubo una columna de panel real
   en ese rango para reordenar.
3. `ReviewPane` es un stub (`return null`) mientras W2-A1 no publique el
   suyo — el modo `review` hoy muestra tres columnas donde la cuarta está
   vacía. El grid, el conmutador y el atajo funcionan igual; falta que
   W2-A1 aterrice para que la cuarta columna tenga contenido.

**No verificado**: comportamiento con lector de pantalla real de los tres
`role="radio"` (se siguió el patrón `role="radiogroup"`/`aria-checked` ya
usado por el selector Chat/Agente del compositor, pero no se probó con un
lector de pantalla real, solo por inspección de atributos).

## Estado comparativo

**Ventaja propia** en el mecanismo de no-duplicación de estado (la
solución de referencia no especifica cómo evitar una segunda copia del
documento/conversación; aquí quedó forzado por diseño — `layout` es un
`string`, nada más — y verificado por fuente que `setLayout` no crea
nada). **Comparación pendiente** en el resto: el informe no da una
implementación de referencia concreta que auditar línea a línea (es una
especificación de comportamiento, no un repositorio externo), así que no
hay "paridad demostrada" ni "ventaja externa documentada" que evaluar —
esto es una hipótesis de mejora sobre el hueco que el informe señala
(Studio no tenía ninguna disposición alternativa), medida contra los
criterios de aceptación explícitos del propio informe (conmutador, atajo,
sin duplicar conversación/documento/borrador), no contra un producto de
terceros.

## Decisión

**Conservar** el mecanismo de rejilla existente (`grid-template-columns`
por punto de ruptura) y **extenderlo** con las dos variables de ancho y la
cuarta columna, en vez de sustituirlo por un sistema de disposición nuevo
(p. ej. `grid-template-areas` con reordenación completa) — el coste de
tocar las 6 reglas responsivas ya existentes sin necesidad no se
justificaba frente a añadir 2 variables con *fallback*. **Componer** con
el trabajo de W2-A1 (`ReviewPane`) en vez de bloquear en su ausencia,
mediante el stub con export nombrado que el contrato autoriza
explícitamente.

## Pruebas ejecutadas

Desde `/home/claude/faustus`:

```
$ ./node_modules/.bin/tsc --noEmit -p tsconfig.json
(sin salida — compila limpio)

$ ./node_modules/.bin/vite build
✓ built in 16.00s   (solo el aviso preexistente de tamaño de chunk > 500kB,
                      no relacionado con este lote)

$ node studio/checks/studio_layout.check.mjs
ok studio_layout   (47 comprobaciones, 0 fallos)

$ python3 -m pytest tests/test_cmp01b_layout_js.py -q -p no:cacheprovider -W ignore
... (ver informe final)

$ python3 -m pytest tests -q -p no:cacheprovider -W ignore -k "studio_guards"
... (ver informe final — guards repo-wide, incluye el CSS/TSX de este lote)

$ python3 scripts/i18n_es.py
es.ts: 6679 strings   (4 filas nuevas: Layout, Main conversation (Ctrl+Alt+L),
                        Main document (Ctrl+Alt+L), Review (Ctrl+Alt+L))
```

## Puntos a cablear por el orquestador

1. Cuando W2-A1 publique `studio/src/screens/documents/ReviewPane.tsx`
   real, confirmar que sigue exportando `ReviewPane` por nombre y que
   acepta `{docId: string | null}` (o ajustar esa única línea en
   `Studio.tsx`).
2. Si se quiere que `Ctrl+Alt+L` sea configurable como los demás atajos,
   añadir `toggle_layout` a `DEFAULT_KEYBINDS`/`KEYBIND_LABELS`
   (`studio/src/adapters/settings.ts`, fuera del alcance de este lote) y
   sustituir el listener dedicado de `Studio.tsx` por una entrada más en
   `shortcutsRef.current`.
3. Si más adelante se separa "documento" de "resto del panel" dentro de
   `SidePanel.tsx` (limitación (1) de Cobertura), la tercera zona opcional
   del modo `document` puede añadirse como una quinta columna con el mismo
   patrón `--fs-*-col` que ya existe aquí.
