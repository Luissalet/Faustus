# Estado del lote F — paridad del compositor y del transcript

Rama: `feat/studio-ui`. Fecha: 04-09-2026 (noche).

Regla que lo motiva (Luis): redistribuir y cambiar sí; **menos funciones,
nunca**. `PARIDAD_FUNCIONAL.md` es el libro; este lote cierra las filas de
uso diario del compositor, del transcript y de la lista de conversaciones.

## Lo que entra en Studio

`studio/src/screens/studio/` (Studio.tsx queda como orquestador):

| Pieza | Fichero | Endpoints |
|---|---|---|
| Compositor: adjuntos (botón, pegar, arrastrar), `@` ficheros con lista, `/` comandos con lista, chips Web/Docs/Terminal/Plan, carpeta, ajustes de generación | `Composer.tsx` | `POST /api/upload`, `GET /api/workspace/files` |
| Carpeta de trabajo: árbol navegable, ruta a mano, vetado | `WorkspaceDialog.tsx` | `GET /api/workspace/browse`, `GET /api/workspace/vet` |
| Transcript: editar (guardar / guardar y regenerar), regenerar, borrar, copiar mensaje, copiar bloque de código, adjuntos en la burbuja | `Transcript.tsx`, `rich.tsx` | `POST …/truncate`, `…/edit-message`, `…/delete-messages` |
| Conversaciones: renombrar, favorita, archivar, exportar en seis formatos, borrar con confirmación | `SessionsPane.tsx`, `SessionDialog.tsx` | `PATCH /api/session/{id}`, `…/important`, `…/archive`, `DELETE`, `GET …/export?fmt=` |
| Comandos | `commands.ts` | `/compact`, `/truncate`, `/rename`, `/export`, `/remember` → sus endpoints; `/temp` `/maxtokens` `/topp` `/think` `/gen` → `gen_overrides`; `/models` abre la paleta; el resto enruta |
| `#regla` | `Studio.tsx` | `POST /api/workspace/instructions/remember` |

Estado compartido con la interfaz anterior, en sus claves exactas:
`odysseus-workspace` (carpeta, en crudo) y `odysseus-rag-active`. Cambiar de
shell no pierde la carpeta.

## Verificado en el 7001

- `/he` → lista → Tab → `/help` → aviso con los comandos.
- Diálogo de carpeta: `D:\LocalAI\odysseus`, árbol, «Usar esta carpeta».
- `Resume @app.p` → lista difusa (`app.py`, `tests/test_app.py`…) → Tab →
  `@app.py ` → enviado en agente con la carpeta; el modelo lo leyó.
- Editar el último mensaje con «Guardar y regenerar» (Ctrl+Enter): el
  servidor trunca desde ese mensaje (queda versión), el nuevo texto se
  reenvía, la respuesta se regenera.
- Borrar un mensaje: desaparece en Studio y en `/api/history`.
- Adjunto por arrastre (`DataTransfer` simulado): sube, aparece como ficha,
  viaja en `attachments`. **El servidor del 7001 lo rechaza por dueño**
  (PENDIENTES §26): no es de Studio, pero queda por probar en el 7000.
- Renombrar desde el diálogo «…»: lista y cabecera se actualizan.

## Lo que este lote rompió y arregló

- **Dos React.** Ver PENDIENTES §24. Al abrir el diálogo de carpeta (chunk
  perezoso) el shell se desmontaba entero y aparecía la interfaz anterior
  con la URL de Studio. La causa era el `?v=` de cache-busting en la
  entrada. Ahora `studio.js` pesa 2,7 KB y todo lo real va en chunks con
  hash.
- **CSS por duplicado.** Vite volvía a enlazar `index.css` sin `?v=` y la
  segunda copia ganaba los empates de especificidad (el anillo de foco
  legacy reapareció). La hoja se enlaza una vez desde la entrada.
- **`odysseus-workspace` es crudo, no JSON.** El primer intento escribía
  con comillas y la anterior habría leído `"D:\…"`.

## Tamaño

`app` 372,9 KB / 118,7 KB gzip, más chunks perezosos (Proyectos, Proyecto,
Biblioteca, Automatizaciones, Actividad, WorkspaceDialog, SessionDialog,
Gallery). Bruto por encima de 350: PENDIENTES §25.
