# Estado del lote G — arnés, plan, checkpoints y selector nativo

Fecha: 04-09-2026. Rama `feat/studio-ui`. Verificado en el 7001
(`AUTH_ENABLED=false`) con `qwen3.5:9b` y el workspace
`D:\LocalAI\_claude_tmp\objtest_ws` (repo git con un commit inicial).

## Qué entra

- **Tarjeta del arnés** bajo cada respuesta de agente: veredicto
  (Verificado / Sin verificar / Terminó…), comprobaciones por ronda
  (`harness_check`: punto de control guardado, revisando el diff,
  verificado…), tests y análisis estático si vienen, «cambios frente a lo
  afirmado» con veredicto y confianza, avisos de afirmaciones sin cambio y
  cambios sin afirmar, lista de ficheros tocados con diff desplegable y
  revertir por fichero, «Volver a antes de este turno» (restaura el
  checkpoint del turno) y «Confirmar en git…» con el mensaje que propone el
  servidor (`/commit/proposal`) y el commit real (`/commit`).
- **Progreso y plan en vivo** (`progress_update`, `plan_update`) dentro del
  turno; porcentaje de contexto (`context_ledger`) en el pie.
- **`/versions`, `/restore <id>`, `/checkpoints`.**
- **Selector nativo de carpeta**: `POST /api/workspace/pick` (tkinter en un
  subproceso; loopback + admin + mismo origen; un diálogo a la vez; muere
  con la petición; exento del timeout duro de 45 s). Chip de Studio →
  Explorador de Windows; diálogo en página solo si el nativo no está
  disponible, y aun así con un botón «Explorador del sistema…». La pill de
  la anterior hace lo mismo y su modal gana dos botones (carpeta / fichero)
  para el selector de contexto de proyecto, que mezcla ficheros y carpetas.
- Los chunks `ModelPalette` y `CommandPalette` pasan a perezosos.

## Verificado en el navegador

- Chip de carpeta → se abre «Elegir carpeta de trabajo» (Explorador) →
  escribir `D:\LocalAI\_claude_tmp` → Select Folder → aviso «Carpeta:
  D:\LocalAI\_claude_tmp», chip `_claude_tmp`; vuelta a `objtest_ws` por el
  mismo camino. La anterior (`?shell=legacy`) enseña «Working in
  objtest_ws» y su pill abre también el Explorador; Cancel lo cierra sin
  dejar procesos.
- Agente: «Crea un fichero llamado saludo.txt…» → paso «Escribir ·
  saludo.txt» → tarjeta de permiso → Aprobar → respuesta → tarjeta
  **VERIFICADO · 1 herramienta · 1 cambio · terminó** con las tres
  comprobaciones, «partial (65 %)», `saludo.txt` con diff (`+hola desde
  studio`), «Confirmar en git…» → formulario con mensaje propuesto → Commit
  → «Commit hecho (821d984)»; «Volver a antes de este turno» → «0
  restaurados, 1 borrados» y el botón queda deshabilitado.
- Segundo turno (adios.txt): dos herramientas, dos cambios, sin volver a
  pedir permiso (concesión de la tarea), tarjeta correcta.
- Historial recargado: la pregunta de permiso ya no aparece como burbuja
  propia.

## Lo que se rompió y se arregló

- El primer `/pick` cayó en el `REQUEST_HARD_TIMEOUT` de 45 s con el diálogo
  aún abierto («Request exceeded 45s timeout»). Exento en `app.py`, y el
  subproceso ahora se vigila por sondeo para matarlo si el cliente se va.
- Fila duplicada en el carril tras aprobar y pregunta de permiso pegada al
  texto de la respuesta: PENDIENTES §31.

## Tamaño

`app` 328,1 KB / 103,4 KB gzip; perezosos: Projects, Project, Library,
Automations, Activity, WorkspaceDialog, SessionDialog, Harness,
ModelPalette, CommandPalette, Gallery.

## Siguiente

Tablero de sub-agentes (`subagent_event`), vista del navegador
(`browser_view`), documentos del editor (`doc_*`); luego el punto 5 de
PARIDAD §6 (ajustes, tema, atajos).
