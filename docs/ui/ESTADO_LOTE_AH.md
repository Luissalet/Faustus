# Estado del lote AH — Markdown completo y fichas de mención

Fecha: 05-09-2026. Rama `feat/studio-ui`. Cierra dos filas de PARIDAD §4:
«Markdown completo (tablas, notas al pie)» (era **Parcial**) y «Fichas de
mención pulsables» (era **Anterior**, la única de la sección). Con esto la
§4 no tiene ninguna fila pendiente; la fila «Chat / agente» de §1 sigue
**Parcial** a la espera de los comandos `/` (§3, lote AI).

## Brief (banco de trabajo)

**Trabajo.** Leer una respuesta larga de un modelo sin tener que
descifrarla. Una comparativa llega como tabla, un plan como jerarquía, una
cita como cita; y un `@fichero` que escribiste hace veinte turnos se abre
de un clic sin buscarlo en el árbol.

**Lo que había.** `rich.tsx` era un lector deliberadamente pequeño: código
en vallas, código en línea, `**negrita**`, enlaces, títulos dibujados todos
como el mismo `<p>` en negrita, y listas planas. Todo lo demás caía como
texto — una decisión honesta (nada se oculta) tomada contra un presupuesto
de 350 KB que suponía que una librería costaba 40–90 KB. La consecuencia
era que una tabla llegaba como un muro de pipes, seis niveles de plan como
seis líneas idénticas, y la vista previa del editor de documentos no servía
para revisar un documento (PENDIENTES 101).

Las fichas de mención existían solo en la anterior: `mentionChips.js`
recorría el DOM del mensaje con un `TreeWalker` en cada tick del stream,
reemplazaba nodos de texto por `<button>` y memoizaba el workspace porque
la barrida costaba dinero. Un parche sobre HTML ajeno.

**Dirección.** Escribir el parser en vez de importarlo, y separarlo del
dibujo: `lib/markdown.ts` es texto → árbol, sin React ni DOM, y `rich.tsx`
es árbol → React. La parte pura es la que los tests pueden sujetar, y el
coste real medido es de 7,2 KB en el chunk (`rich`: 3,7 → 10,9 KB, 4,2 KB
gzip), no 40–90.

## Lo que se hizo

### `studio/src/lib/markdown.ts` (nuevo, 470 líneas)

Un parser de bloques + inline. Devuelve `{ blocks, footnotes }`.

- **Bloques**: `heading` (ATX `#`–`######` y setext `===`), `para`, `code`
  (vallas ``` y `~~~`, con lenguaje), `list` (ordenada o no, anidada por
  indentación, suelta o apretada, con `start` propio y tareas `- [ ]` /
  `- [x]`), `quote` (recursivo, con continuación perezosa), `table`
  (cabecera + fila de alineación `:--` / `--:` / `:-:`, pipe escapado
  `\|`, filas cortas rellenadas nunca descartadas), `rule`.
- **Inline**: `code` (una y dos comillas), `strong`, `em` (`*` y `_`, con
  frontera de palabra para que `snake_case` no salga en cursiva), `del`,
  `link` (con título opcional), `image`, `note` (referencia a nota al
  pie), `break`, y texto. Autoenlaces `<url>` y URLs desnudas, a las que
  se les quita el punto final de la frase.
- **Escapes**: `\*`, `\|`, `\[`… se sacan del escaneo antes que nada, para
  que un asterisco escapado no abra una cursiva. Los trozos de texto
  contiguos se funden, así que un escape no parte una palabra en tres
  nodos.
- **Enlaces desactivados**: `safeHref` deja pasar `http(s):`, `mailto:` y
  las rutas relativas; cualquier otro esquema (`javascript:`, `data:` que
  no sea imagen) sale como `#`. Es texto que escribe un modelo.
- **Notas al pie**: las definiciones `[^id]: …` se extraen antes de
  parsear el cuerpo (dentro de una valla no cuentan), se numeran por orden
  de **referencia** —no de definición— y una definición que nadie llama se
  muestra igual al final: nada se pierde.
- La regla vieja sigue en pie: lo que no se reconoce cae como texto.

### `studio/src/screens/rich.tsx` (reescrito, 220 líneas)

Solo dibujo. Mantiene lo que solo la shell sabe: el censor (`findSensitive`
convierte un secreto en un botón de revelar) y el interruptor de emojis.
`useId()` da el ancla única de las notas al pie, así que dos turnos con
`[^1]` no chocan.

### CSS (`studio/src/screens/studio.css`)

- **Titulares**: escala comprimida pero con jerarquía real — h1 1,5em con
  filete, h2 1,26em con filete fino, h3 1,12em, h4 1em en `text-2`, h5
  0,95em en `text-3`, h6 en versalitas de etiqueta. Un `#` en una
  respuesta es estructura, no un cartel.
- **Tablas**: marco propio con scroll horizontal (región enfocable con
  teclado), cabecera con filete fuerte y fondo, filas separadas por
  filete, hover, alineación por `data-align` y `tabular-nums` en las
  columnas alineadas a la derecha. `word-break: normal` en las celdas: sin
  eso `qwen3.5` se partía después del punto.
- **Citas**: barra ember a la izquierda y texto en `text-2`; una caja
  competiría con los bloques de código.
- **Tareas**: casilla real deshabilitada, el texto hecho en `text-3`.
- **Notas al pie**: bloque separado por filete, marcador ember, y flecha
  de vuelta al punto de llamada.

### `studio/src/lib/mentions.ts` (nuevo) y `Transcript.tsx`

`MENTION_RE` (el mismo patrón que `src/file_mentions.py` y que usaba
`mentionChips.js`), `mentionPath` y `splitMentions`. El turno del usuario
ya no imprime `{turn.text}` crudo: lo parte y dibuja cada mención como un
botón `.fs-turn__mention` que llama al `onOpenFile` que ya bajaba desde
`Studio.tsx`. Ese prop solo existe con carpeta vinculada, que es
exactamente la puerta que ponía la anterior (sin workspace no hay contra
qué abrirlas). Sin `TreeWalker`, sin `MutationObserver`, sin memoizar
`localStorage`: es una función del texto.

De paso, `editado` estaba escrito a pelo en castellano dentro del turno;
ahora es `t('edited')`.

## Pruebas

- `studio/checks/markdown.check.mjs` (nuevo) + `tests/test_studio_markdown_js.py`:
  42 comprobaciones sobre las dos librerías puras —titulares, inline,
  listas anidadas y de tareas, tablas con alineación y pipe escapado,
  citas, vallas sin cerrar, notas al pie, escapes, `javascript:`
  desactivado, lo desconocido como texto, y las menciones (ruta, comillas,
  puntuación final, un correo que no es mención).
- `tests\test_studio*.py`: 20 en verde (eran 19).
- Compilación limpia; `rich` pasa de 3,7 a 10,9 KB (4,2 KB gzip).

## Verificado en el 7001

- **Vista previa del editor** (`/documents/{id}`, «MD torture»): h1–h6 con
  jerarquía visible, negrita / cursiva / cursiva con guion bajo / tachado /
  código en línea, enlace con título, autoenlace, dos notas al pie con su
  superíndice y su lista al final con flecha de vuelta, cita de dos líneas,
  tabla de tres columnas con alineación (izquierda / derecha / centro) y una
  celda larga que envuelve por palabras, regla, lista ordenada con viñetas
  anidadas y una ordenada más adentro, lista de tareas con las dos casillas,
  valla `python` con su insignia de lenguaje y su botón de copiar,
  `snake_case_word` sin cursiva.
- **Transcript** (`/studio`, qwen3.5:9b en modo chat, carpeta `odysseus`
  vinculada): el turno del usuario dibuja `@README.md` y `@app.py` como
  fichas; clic en `@README.md` abre la pestaña «Fichero» del panel lateral
  con el README real (421 líneas). La respuesta del modelo, una tabla
  markdown de dos filas, se dibuja como tabla.

## Lo que queda

- `PENDIENTES_UI.md` 124–126: sin HTML incrustado ni listas de definición
  (siguen cayendo como texto), el ancla de la nota al pie con el transcript
  en un contenedor con scroll propio no siempre queda arriba del todo, y una
  tabla más ancha que la columna hace scroll sin más indicador que el corte.
- La fila «Chat / agente» de PARIDAD §1 sigue **Parcial** hasta el lote AI
  (comandos `/`).
