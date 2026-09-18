# Estado del lote H — sub-agentes, panel lateral, documentos, traza restaurada

Fecha: 04-09-2026. Rama `feat/studio-ui`. Verificado en el 7001 con
`qwen3.5:9b` y el workspace `D:\LocalAI\_claude_tmp\objtest_ws`.

## Qué entra

- **Tablero de sub-agentes** dentro del turno (`SubagentBoard`, chunk
  perezoso): una tarjeta por worker con nombre, rol, modelo, estado (en
  cola / en marcha / parado / hecho / fallido / parcial, con «sin señal» y
  «bucle» como en la anterior), actividad («Editando ficheros», «Pensando»…),
  tiempo, ronda, herramientas, tokens, ficheros que posee y que cambió,
  última línea, cola de salida, líneas de dirección y supervisor; botones
  Parar, Dirigir…, Abrir su chat y Repetir… (con modelo). Los eventos llegan
  como `tool_progress` con `subagent`, y el reductor `applyWorker` es el
  `_saApply` de la anterior campo por campo.
- **`/agents`** con la misma sintaxis (`|`, `[ficheros]`, `{modelo}`,
  `--review`, `--serial`) y el mismo campo `delegate_tasks`; fuerza agente.
- **Panel lateral** (`SidePanel`, chunk perezoso; tercera columna en ≥1280,
  capa por encima en pantallas más estrechas; botón en la cabecera):
  - **Navegador**: fotogramas de `browser_view` y de las capturas de
    `tool_output`, título y URL, tira de los últimos 8, «en vivo» mientras
    el turno corre, «abrir solo» con la clave `odysseus.browserView.auto`.
  - **Documento**: se abre cuando el agente escribe (`doc_stream_open`,
    `doc_stream_delta`, `doc_update`) y trae un editor real: guardar (PUT,
    versiona), renombrar (PATCH), vista previa, versiones y restaurar, PDF,
    archivar, y las sugerencias del agente (`doc_suggestions`) una a una.
    `/doc título` crea uno; la Biblioteca abre cualquier documento con
    `/studio?doc=<id>`.
  - **Fichero**: visor con números de línea (`/api/workspace/file`), desde
    «Ver el fichero» en el carril o `/open ruta`.
- **Traza y arnés restaurados del historial**: `tool_events` vuelve como
  carril (con diff coloreado por escritura, capturas, permisos ya
  respondidos), `harness` como tarjeta, `web_sources` como fuentes y un
  permiso pendiente como tarjeta de aprobación. Antes, al recargar, el turno
  era solo texto.
- `studio/checks/model.check.mjs` + `tests/test_studio_model_js.py`: el
  reductor con una delegación sintética, la aprobación repetida y un
  historial persistido.

## Verificado en el navegador

- Sesión de `saludo.txt` recargada: carril con «nuevo +1» por escritura,
  diff desplegable, «Ver el fichero» → pestaña Fichero con `adios.txt`;
  tarjeta «Resumen del turno» con los ficheros y los botones de checkpoint.
- «Crea un documento titulado 'Notas del mar'…» → paso «Crear documento ·
  Notas del mar», el panel se abre solo en Documento con el contenido;
  añadir una frase → Guardar → «Guardado (v2)»; Versiones → v2 tú · Manual
  edit, v1 agente · Created by qwen3.5:9b con Restaurar.
- `/open README.md` → visor con la línea numerada.
- Biblioteca → Documentos → «Notas del mar» → `/studio?doc=…` → panel con
  el documento cargado por id.
- `bench t3v2_q35b 20260831_144516` (sesión con `delegate_agents`
  persistido): «Sub-agentes 3/3», tres tarjetas hechas con tiempo,
  herramientas, ficheros cambiados, texto final y «Abrir su chat».
- `/agents` en vivo: el comando viaja bien pero `qwen3.5:9b` no llama a la
  herramienta (PENDIENTES §33); el tablero en vivo queda cubierto por el
  reductor.

## Tamaño

`app` 347,4 KB / 109,6 KB gzip (el reductor de workers y la restauración
del historial pesan); perezosos nuevos: SubagentBoard 9,3 KB, SidePanel
10,5 KB. Bruto cerca del aviso de 350; gzip bajo 120.

## Siguiente

PARIDAD §6 punto 3 (fork, incógnito, selección múltiple, presets, STT/TTS,
citar selección) y después las pantallas propias: Notas, Calendario, Correo,
Brain, Cookbook, Research, Compare, Tournament, Workers, Expertos,
Procedencia, Importación, Runners, Definiciones, Skills, Ajustes, Tema,
Fondos.
