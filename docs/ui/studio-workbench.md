# Studio: equipo y panel de trabajo

Extensión acotada del Studio existente, en modo **Operate**. Permite configurar
el equipo de una conversación y consultar sus resultados, fuentes y agentes sin
salir del chat. Conserva la identidad editorial, cálida y técnica de Faustus;
`DESIGN.md` sigue siendo la autoridad visual y no se modifica ni se regenera su
sidecar `.impeccable/design.json` en este trabajo.

## Experiencia implementada

- **Equipo junto al selector de modelo.** El modelo elegido en el chat actúa
  como coordinador. El editor permite activar la orquestación y configurar hasta
  ocho miembros: nombre, modelo/endpoint o herencia del coordinador, instrucciones,
  permiso de escritura dentro de los permisos heredados, herramientas y archivos
  asignados. Los límites son 1–8 agentes paralelos, 3–40 rondas y 60–7200 segundos
  por agente. Guardar aplica la configuración a turnos posteriores; la edición
  queda deshabilitada durante el turno actual.
- **Un panel de trabajo.** Resultados reúne documentos, archivos detectados en
  las salidas de herramientas e imágenes. Fuentes reúne referencias HTTP(S),
  adjuntos y contexto del proyecto. Agentes reutiliza el tablero de trabajadores
  de la conversación. Navegador muestra las capturas que entrega el agente,
  incluida la procedencia escritorio cuando corresponde.
- **Resultados abiertos.** Documentos y archivos tienen accesos propios encima
  del contenido. Cambiar de pestaña o cerrar el panel no elimina sus borradores.
  Un resultado con borrador no se puede retirar de esa lista hasta guardarlo o
  descartarlo. La llegada de resultados en segundo plano conserva la vista que
  la persona está consultando cuando el panel ya está abierto en otra pestaña.
- **Edición junto al chat.** Los documentos ofrecen lectura/edición, guardado,
  sugerencias, versiones y las acciones existentes de PDF, editor completo y
  archivo. Los archivos Markdown y de texto admitidos ofrecen vista previa,
  edición, recarga y descarte. Los demás archivos de texto se leen con números
  de línea; los binarios se identifican sin simular un editor.
- **Enlaces de archivos.** Los enlaces locales del contenido del chat pueden
  abrir el archivo en el panel cuando hay un manejador de workspace. Cada enlace
  conserva su semántica de enlace y su propio manejador; los modificadores de
  teclado mantienen el comportamiento normal del navegador. No se convierte el
  contenedor completo del texto en una zona clicable.

## Composición y accesibilidad

La extensión reutiliza las superficies, bordes, tipografía, espaciado, radios e
iconos existentes. Los recursos se presentan como filas separadas por divisores,
sin una nueva paleta ni tarjetas decorativas. El estado se expresa con palabras;
el color no es su único indicador. La copia usa el sistema de traducción de la
aplicación, con español e inglés.

En escritorio, a partir de 1280 px, el panel forma parte de la rejilla de Studio.
Su ancho inicial es 520 px; el control permite solicitar de 320 a 900 px, con un
límite visual del 50 % del viewport. Por debajo, usa el panel superpuesto
existente. A 600 px o menos ocupa todo el ancho y oculta el ajuste de anchura;
los límites del equipo pasan a una columna. Las pestañas y los resultados abiertos
tienen desplazamiento horizontal cuando lo necesitan.

Las pestañas usan `tablist`, `tab`, `tabpanel`, selección y foco itinerante; las
flechas, Inicio y Fin cambian la selección y enfocan la pestaña correspondiente.
El editor del equipo tiene cierre explícito y Escape devuelve el foco al botón
que lo abrió. Las reglas móviles de esta extensión establecen áreas de al menos
44 px en las pestañas, accesos/cierres de resultados y controles del equipo.
Esto documenta los controles revisados, no una certificación de accesibilidad de
todo Studio.

## Estado y guardado seguro

`panel.ts` conserva documentos, archivos y borradores por clave de recurso.
`useChatPanel.ts` mantiene el estado separado por conversación y vincula las
respuestas tardías a la conversación que inició la operación. En modo normal,
guarda ese estado en `sessionStorage`; las capturas y el indicador de actividad
no se restauran como si fueran una ejecución actual. En privado no escribe ese
estado en el almacenamiento del navegador. Un borrador pendiente activa el aviso
de salida de página del navegador.

La persistencia de borradores es local a esa sesión del navegador: no es
sincronización entre dispositivos ni un guardado en servidor. Si el navegador
deniega almacenamiento o se agota su espacio, permanecen sólo en memoria. Los
borradores de configuración del equipo se conservan en memoria al cambiar de
conversación; sólo el equipo guardado se persiste en el backend.

El guardado de archivos compara la revisión SHA-256 de los bytes leídos y el de
documentos compara el contenido base esperado. Una base distinta devuelve un
conflicto (409), sin escribir el borrador encima del contenido nuevo. En
documentos se compara contenido, no sólo el número de versión: dos guardados
cercanos pueden compartir versión por la coalescencia existente.

Una confirmación tardía de guardado tampoco borra texto escrito después: la
acción `draft-saved` retira únicamente el borrador que coincide con lo enviado;
si existe una edición posterior de la misma base, la conserva y actualiza su
base/revisión al resultado guardado. Si la respuesta pertenece a una base ya
superada, se ignora. No hay mezcla automática de cambios en conflicto: la persona
debe revisar el contenido y decidir qué conservar.

`panel-storage.ts` persiste también las respuestas que llegan a un chat no
visible, sin volver a serializar estados sin cambios. Renombrar actualiza sólo
el título, sin reponer una copia antigua del contenido. Restaurar envía la base
esperada para detectar cambios del agente; los controles de restauración y
archivado se bloquean mientras hay cambios sin guardar o una mutación en curso.
Sus respuestas tardías no cambian la pestaña que se está consultando.

## Contratos de implementación

| Área | Implementación y frontera |
| --- | --- |
| Equipo | `studio/src/screens/studio/ChatTeam.tsx`, adaptador `chat-team.ts` y `GET/PUT /api/session/{sid}/team`. El servidor verifica la propiedad de la conversación y compara la revisión antes de guardar. |
| Orquestación | `src/chat_team.py`, `routes/chat_routes.py` y `src/agent_tools/subagent_tools.py`. La configuración se carga como snapshot al comenzar el turno. Se validan miembros, herramientas y rutas exactas; una ruta no disponible falla sin sustitución silenciosa. Las tareas del modelo no pueden reemplazar el roster, el runner ni los permisos heredados. |
| Panel y recursos | `SidePanel.tsx`, `WorkbenchResources.tsx`, `panel.ts` y `useChatPanel.ts`, integrados desde `Studio.tsx`. La lista de resultados se deriva de eventos/salidas reconocidas, no de un inventario exhaustivo de todo el disco. |
| Archivos | `routes/workspace_routes.py`, `GET/PUT /api/workspace/file`. Acceso de administrador o usuario único, confinamiento al workspace, rechazo de escrituras de origen cruzado y publicación atómica. Sólo se editan archivos existentes `.md`, `.markdown` y `.txt`, UTF-8 no binarios y de hasta 400 000 bytes. Se conserva CRLF si el original lo utiliza. |
| Documentos | `routes/document/document_helpers.py` y `document_routes.py`; `PUT /api/document/{id}` recibe `expected_content` para el control de conflicto del panel, además de la comprobación de propietario existente. |
| Enlaces | `studio/src/screens/rich.tsx` y `studio/src/lib/markdown.ts`. Se distinguen rutas locales y enlaces externos; las rutas locales siguen sujetas al confinamiento del backend. |

La opción de equipo no implica ejecutar todos los miembros en cada mensaje: el
coordinador decide qué tareas acotadas delegar. El modo incógnito no carga el
equipo persistido para la ejecución. Las asignaciones de archivos no amplían la
autoridad del usuario ni sustituyen los permisos heredados.

## Evidencia de cierre

Registro de la sesión de implementación del 7 de septiembre de 2026; esta
documentación no vuelve a ejecutar el ciclo de inspección visual de Impeccable.

- TypeScript y build de Studio: correctos, según la verificación de cierre.
- `studio/checks/workbench.check.mjs`: `ALL OK`. Comprueba conservación de
  borradores al navegar, bloqueo del cierre con cambios, guardados sin cambio de
  vista, límites de anchura, enlaces locales seguros y respuestas de guardado
  tardías para archivos y documentos. Incluye conservación de edición posterior,
  actualización de su base y rechazo de una respuesta ya obsoleta.
- Pruebas de backend pertinentes: `tests/test_chat_team.py`,
  `tests/test_workspace_file_viewer_routes.py` y
  `tests/test_document_editor_conflict.py` cubren contratos de propietario,
  revisiones, configuración inválida, rutas/permisos y conflictos de edición.
  La ejecución enfocada inicial reportó 35 pruebas correctas.
- La nueva suite global reportó **12 486 correctas, 82 omitidas y 125 avisos**,
  sin fallos (`logs/astra-workbench-tests.xml`). Tras cambios posteriores, la
  pasada focalizada final reportó **127 correctas, una omitida y cuatro avisos**
  (`logs/astra-workbench-final-focused.xml`); incluye los contratos de persistencia
  en segundo plano, restauración condicionada y metadatos sin pérdida de contenido.
- Detector de diseño ejecutado una vez: `[]`. La revisión de cierre dio **pass**
  después de cerrar el P1 de borrador borrado por guardado tardío y el P2 de
  controles móviles inferiores a 44 px. El veredicto está acotado a esta
  superficie; no certifica el backend completo.

Las cinco capturas están en `.impeccable/review/workbench/`:
`desktop-panel.png`, `desktop-team.png`, `mobile-panel.png`, `mobile-team.png` y
`desktop-light.png`. Proceden de componentes reales montados con estado y
respuestas sintéticas en el servidor local de QA (puerto 7005), descrito en
`studio/checks/workbench-preview.mjs`. No son capturas del flujo completo de
Studio con backend y proveedores reales. Son evidencia de QA, no imágenes
publicadas como parte del producto; no se añade ningún raster de interfaz.

Además se probó una delegación real con Qwen local en Studio: configuración y
recarga del equipo, aprobación, salida a Actividad y retorno al resultado en
español. Sin llamadas de pago ni cambios de archivos. Tras reconstruir la
interfaz se comprobaron las etiquetas inglesas de agentes en ese chat. Tras el
intento de reinicio bloqueado, se ejecutó `D:\LocalAI\Restart-Faustus.bat` a
petición de Luis: nuevo proceso servidor y HTTP 200 comprobados. Los ajustes más
recientes están activos y probados automáticamente; falta su prueba funcional en vivo.

## Límites explícitos

- No integra un chat de CLI ni convierte Codex/Claude Code en coordinadores del
  chat. La configuración de miembros usa las rutas de modelo/endpoint admitidas.
- Navegador es un visor de capturas con historial reciente de hasta ocho frames,
  no un navegador interactivo completo: no permite navegar, clicar la página
  remota ni controlar el escritorio desde esa imagen.
- No promete un catálogo exhaustivo de resultados ni un gestor de archivos;
  las carpetas del contexto no se abren como archivos editables.
- No promete guardado automático, sincronización de borradores entre dispositivos
  ni resolución automática de conflictos.
- La prueba sintética valida componentes y estados concretos. No sustituye una
  prueba de extremo a extremo de todo Studio, de proveedores reales, de todas las
  combinaciones de permisos ni de ejecución completa del backend.
