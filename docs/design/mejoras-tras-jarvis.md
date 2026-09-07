# Mejoras posteriores a Jarvis: pendientes de revisión de Luis

Punto de separación: `4aad6e6`, publicado en `origin/master` como Luissalet.
Todo lo registrado aquí es posterior y debe permanecer **sin commit, sin push
y sin preparar** hasta que Luis lo revise. No publicar automáticamente.

## 1. Cierre de conexiones del índice de contexto — 07-09-2026

**Problema comprobado:** el contexto de una conexión SQLite confirma o revierte
transacciones, pero no cierra la conexión. Las consultas del índice dejaban ese
cierre al recolector; un error al inicializar el esquema tampoco lo garantizaba.
No se ha demostrado que esto fuese la causa de un bloqueo observado por Luis.

**Cambio:** `src/project_context/index.py` usa un contexto que mantiene la
transacción y cierra la conexión en `finally`, también si falla la creación del
esquema. No cambia el formato de datos, los permisos, las revisiones ni el ranking.

**Pruebas:** `tests/test_project_context_index_connections.py` cubre insertar,
leer revisión, buscar, borrar, fallo de esquema y rollback de una sustitución
fallida. Los seis casos fallaron antes del cambio y pasan después. El bloque
completo de contexto seleccionado dio **127 correctos**:

```powershell
venv/Scripts/python.exe -m pytest tests/test_project_context_index_connections.py tests/test_project_context_indexer.py tests/test_project_context_service.py tests/test_context_engine_sources.py tests/test_project_links_source.py -q --tb=short
```

**Alcance de validación:** pruebas con SQLite real en directorios temporales.
No se ha medido memoria durante horas. La suite completa ejecutada más adelante
se registra al final. El backend que sigue abierto ejecuta la versión publicada;
estas mejoras de Python requieren reiniciarlo después de la revisión. Los
archivos estáticos del frontend sí se han reconstruido durante esta pasada.

## 2. Recuperación del contexto de proyectos

`src/project_context/service.py` e `indexer.py`:

- Una extracción fallida ya no guarda `source_state="failed"`, valor inválido
  que hacía ilegible el propio enlace. Se separa fallo de índice y estado fuente.
- Actualizar un enlace fallido vuelve a ponerlo en cola aunque sus bytes no hayan
  cambiado. Desactivar/reactivar el enlace ajusta correctamente su indexación.
- Una desactivación observada tras extraer impide publicar ese resultado.
- Revisión vacía y fallo de publicación dejan un estado de error recuperable.
- Los enlaces deshabilitados no agotan el cupo del worker; un proyecto con error
  de listado no bloquea la revisión de los siguientes.

Ocho regresiones añadidas fallaron antes y pasaron después; bloque de contexto:
135 pruebas correctas. No se ha implementado una reparación automática de
registros antiguos ni un compare-and-swap que cierre toda la ventana entre
comprobar un enlace y publicar su índice. Esos límites siguen pendientes.

## 3. Actividad: conversaciones, recuperación y controles claros

Conversaciones en curso, en cola y esperando permiso aparecen junto a tareas,
renders y aprobaciones. Se muestran fase, modelo y último avance cuando el
servidor los proporciona. Abrir conversación usa su sesión; detener usa su
ejecución exacta, sin detener otra por accidente.

Ante caída parcial se conservan las filas de la fuente fallida, marcadas como
último estado conocido. No se oculta la conversación ni se afirma que no existe
actividad. Las mutaciones sobre ese estado quedan deshabilitadas hasta recuperar
una lectura válida. Recuperación manual, tiempo límite y un único ciclo de
actualización con pausa cuando la página está oculta.

La revisión Impeccable motivó correcciones de estado parcial, selección accesible,
foco lista/detalle y controles móviles de 44 px. La revisión final aceptó este
cambio acotado; no equivale a una auditoría aprobada de toda la aplicación.

Detalle y evidencia: `docs/ui/activity-live-work.md`. Pruebas de navegador con
el componente/CSS reales y datos sintéticos, en español e inglés, escritorio y
móvil. Se comprobó navegación a `/studio?s=working`, foco y recuperación parcial.
No se realizaron llamadas reales al modelo, grabaciones ni paradas de trabajos
del usuario. No se ha sustituido el sistema de ejecución de chats del backend.

## 4. PDF: limitar el trabajo que luego se descartaba

`src/document_processor.py::_process_pdf` tenía un límite final de 15.000
caracteres, pero extraía todas las páginas y podía consultar visión repetidamente
antes de aplicarlo. Incluso materializaba imágenes de páginas con texto suficiente.

Ahora termina al alcanzar el texto utilizable, no abre imágenes innecesarias,
limita la vista previa a 100 páginas y a 6 intentos de visión por documento
(también cuentan los fallidos), y recorre imágenes de forma perezosa. Explica
los límites alcanzados; conserva el PDF original para inspección específica.
Se corrigió el mensaje que prometía texto completo en el visor aunque también
recibía la extracción recortada.

`tests/test_pdf_extraction_budget.py`: siete casos fallaban antes, ocho pasan
después contando el caso de control. Con los tests de marcadores y Actividad:
24 correctos. Esto limita cantidad de trabajo, no garantiza un tiempo total de
OCR ni sustituye procesamiento completo; un solo PDF/página patológico todavía
requiere aislamiento y límites de recursos más fuertes.

## 5. Carga de autenticación: no convertir un error en primer arranque

Al revisar un aviso de lectura concurrente en Windows se observó una ruta de
riesgo distinta: `core/auth.py::_load` sustituía la configuración por `{}` ante
errores de lectura o JSON. Eso podía confundirse con falta de configuración.

Ahora se valida una copia temporal antes de sustituir el estado. Un archivo
inaccesible o inválido impide el arranque y no reinicia usuarios ni permisos;
una recarga fallida conserva el estado previo. Se reintenta brevemente un
`PermissionError` transitorio (hasta tres lecturas). Solo la ausencia real del
archivo, sin configuración previa en memoria, permite el primer arranque.
No se toca el archivo original ni se restauran copias automáticamente.

`tests/test_auth_load_fail_closed.py` reproduce errores de JSON, estructura,
recarga, desaparición de archivo, permisos y recuperación transitoria: ocho
fallos antes del cambio y un caso de control correcto. No se ha probado una
explotación del servidor ni se atribuye el aviso del test a pérdida real de datos.
Se añadieron después los casos de nombres vacíos y colisión al normalizar;
los once casos actuales pasan.

## 6. Reemplazos atómicos frente a bloqueos transitorios de Windows

Al mantener vivo el lector de la prueba de concurrencia apareció un fallo del
escritor: `PermissionError` durante operaciones concurrentes. `core/atomic_io.py`
reintenta exclusivamente `os.replace` para códigos Windows 5/32/33, hasta seis
intentos y 155 ms de esperas acumuladas. No borra el destino, no vuelve a generar
contenido y no cambia ACLs para forzar la escritura. Un bloqueo persistente sigue
siendo error y conserva el archivo anterior.

`tests/test_atomic_io_windows_retry.py` reproduce recuperación de texto/JSON,
fallo persistente conservando original y limpieza del temporal, y rechazo sin
reintento de otros errores de permisos. Tres regresiones fallaban antes y el
caso de control ya pasaba. La prueba concurrente también exige que el escritor
termine y que los 51 usuarios esperados estén realmente en disco.

## 7. Inspección multimedia para agentes

Nueva herramienta `inspect_media` en `src/agent_tools/media_tools.py`, con
implementación `src/media_inspection.py` y trabajador aislado
`src/media_probe_worker.py`. Obtiene dimensiones, orientación, formato, duración,
FPS y pistas cuando existen. Imagen/WAV básicos no necesitan FFprobe; audio/vídeo
adicional usa el FFprobe instalado, sin descargar nada. No interpreta visualmente
el contenido ni transcribe. Integra schemas, descubrimiento, permisos y ejecución.

Respeta raíces autorizadas y bloqueo de archivos sensibles. Rechaza URLs,
listas de reproducción y tipos no admitidos; límites de archivo, tiempo y salida;
termina el proceso al cancelar. No devuelve EXIF arbitrario ni etiquetas privadas.
Comprueba cambios del archivo durante la inspección. No es un sandbox de memoria
completo ni una garantía contra todas las carreras del sistema de archivos.
25 tests iniciales pasan, incluyendo PNG/WAV/MP4 real sintético y cancelación.

## 8. Capturas pegadas directamente en el chat

Ctrl+V admite imagen del portapapeles, miniatura local inmediata, estado de subida,
quitar y reintentar. Dos cargas simultáneas, plazo de 60 segundos, protección
contra adjuntos tardíos de otra conversación; Enter y Enviar esperan los adjuntos.
Texto normal conserva su pegado nativo. No lee el portapapeles en segundo plano.
La base ya permitía algunos pegados: se amplía y robustece, no se presenta como
ausencia absoluta previa. `docs/ui/composer-clipboard.md` documenta el contrato.
Revisión independiente: **ship**, sin hallazgos materiales en el alcance revisado.
Probado con portapapeles virtual del navegador y el Composer real en entorno
sintético; falta prueba física Win+Shift+S y consumo de la imagen por un LLM real.

## 9. Conexión API guiada y privacidad de proveedores

`settings/ProviderConnect.tsx`: OpenAI, Claude y Gemini sin pedir URL ni protocolo;
enlace oficial para clave, consulta del catálogo y elección explícita de modelos.
Se reutilizan rutas y almacenamiento existentes, con clave limpiada al cancelar,
plazo de petición y mensaje de guardado incierto sin reintento automático.
No se consumen generaciones como comprobación. API y suscripción se distinguen.

Correcciones: `refreshEndpointModels` ahora entiende el array que devuelve la ruta
real, en vez de mostrar cero modelos; crear conexión privada no reutiliza una
fila compartida ni se convierte en predeterminado global. Tres regresiones de
privacidad y contrato de adaptadores añadidos. **171 tests pasan** en el bloque
de endpoints y frontend. Sólo probado con credenciales sintéticas; no se han
conectado cuentas reales ni realizado llamadas de pago. Ruta continúa admin-only.

La petición posterior de usar Codex/Claude Code instalados con suscripción es
válida y queda en W12: los ejecutores básicos ya existen en `agent_runners.py`,
`external_worker.py` y `dispatch.py`; falta integrarlos de manera guiada en el
chat/orquestador y separar sus modos de autenticación. API se conserva por
petición explícita de Luis. No presentar los ejecutores existentes como capacidad
ausente ni como una integración en chat ya terminada.
También quedan el orquestador inline y panel lateral completo/Markdown editable,
registrados en PENDIENTES y W11/W13. No confundir estas peticiones con funciones
terminadas por existir el documento.

## 10. Conversiones multimedia que ejecutan y verifican

Se añaden `plan_media_transform` (sólo lectura) y `transform_media` (lectura y
escritura de un archivo nuevo) al registro de herramientas, schemas, búsqueda,
permisos y despacho. Implementación: `src/media_transforms.py`,
`src/media_transform_worker.py`, `src/agent_tools/media_tools.py`.

- PNG/JPEG/WebP: orientación EXIF aplicada, tamaño máximo sin deformación ni
  ampliación, calidad JPEG/WebP, fondo JPEG explícito si hay transparencia.
  Animaciones y multipágina se rechazan, no se convierten silenciosamente al
  primer fotograma. Metadatos eliminados; no es un flujo de color para imprenta.
- Audio/vídeo → WAV PCM 16-bit o MP3 192 kbit/s: primera pista, estéreo a 48 kHz,
  duración conocida hasta 10 minutos. No codifica vídeo, transcribe ni subtitula.
- Preflight con motor disponible y pérdidas declaradas; comprobación de disco,
  entrada de hasta 256 MiB, salida hasta 128 MiB, imágenes hasta 16 MP y conversor
  limitado a 120 segundos. Fases de progreso; cancelación mata y recoge su proceso.
- Copia de trabajo y hash de la entrada usada; salida inspeccionada, formato o
  duración comprobados. Publicación por hard link exclusivo: un archivo existente
  o creado por otro agente se conserva. Un sistema de archivos sin hard links
  falla expresamente. Temporales propios retirados tras error/cancelación.
- Sin comandos del modelo, filtros libres, URLs ni playlists. FFmpeg limita
  protocolos y referencias externas de MOV. No instala motores ni descarga pesos.
  Las reglas de subagente se aplican tanto a la fuente como al destino, incluidos
  bloqueos de archivos. Se corrigió también la omisión de `inspect_media` en las
  reglas de lectura por ruta de los trabajadores.

La procedencia se devuelve al chat con hashes y opciones, **no** se ha implantado
el grafo duradero de artefactos ni una migración ART-1. Los procesos tienen límites
de tiempo/entrada/salida, no una sandbox dura de memoria o CPU. Las comprobaciones
de ruta no prometen inmunidad frente a un proceso hostil que cambie el filesystem.

36 tests de conversiones: imágenes y vídeo con audio reales sintéticos, EXIF,
transparencia, animación, permisos, publicación concurrente, cancelación y errores.
La integración con modelos reales y la presentación de outputs en el nuevo panel
siguen pendientes. Referencias técnicas:
[FFmpeg](https://ffmpeg.org/ffmpeg.html),
[Pillow](https://pillow.readthedocs.io/en/stable/reference/Image.html).

## 11. Diagnóstico de sesión de clientes oficiales

`src/runner_connections.py` y `routes/agent_runner_routes.py` añaden
`GET /api/agent-runners/{codex|claude}/connection`, admin-only. Sólo se consulta
cuando se solicita; listar el catálogo no dispara comprobaciones de cuenta.
Ejecuta `codex login status` o `claude auth status --json`, con plazo y salida
acotados. Devuelve únicamente estados permitidos: instalación, sesión, vía
suscripción/API/desconocida, conflictos del entorno y enlaces de ayuda. Omite
email, organización, claves y salida cruda. Nunca inventa autenticación a partir
de la presencia del ejecutable; respuestas futuras desconocidas quedan sin verificar.

Comprobación local real: ambos instalados y ambos informan sesión de suscripción;
`agent_external_runners` permanece desactivado. No se enviaron prompts ni se
ejecutaron órdenes de login/logout/instalación. 24 pruebas de parser, ruta protegida,
errores, cancelación y límites. **Diagnóstico, no selector ni política de ejecución**:
falta elegir/persistir la vía desde el chat y comprobarla durante el trabajo.
La configuración del CLI puede cambiar; este snapshot no garantiza gasto futuro.

OpenAI Docs orientó esta separación: `forced_login_method` puede cerrar la sesión
si no coincide, por lo que no se usó como una comprobación inocua sobre la cuenta
del usuario. La CLI dispone de un comando de estado específico.
[Autenticación de Codex](https://learn.chatgpt.com/docs/auth),
[referencia del CLI](https://learn.chatgpt.com/docs/cli/reference),
[precedencia de credenciales de Claude Code](https://code.claude.com/docs/en/authentication).

## 12. Contexto: publicar sólo si el enlace sigue vigente

La indexación usa `ProjectStore.patch_link_if_current`, que comprueba estado,
revisiones y políticas bajo el bloqueo del registro. El callback de publicación
y el marcado ready no admiten una desactivación/desvinculación intercalada dentro
del mismo store. Se protegen también el inicio, la reencolación y los fallos tardíos.
Una carrera perdida devuelve disabled/detached/superseded sin pisar el cambio nuevo.

43 pruebas de indexador/store pasan, incluidas una carrera inmediatamente anterior
a publicar, error después de desactivar, contrato con store real y concurrencia
de hilos. Alcance: una instancia de ProjectStore, no escritura concurrente desde
varios procesos. No hay transacción única JSON/SQLite: si SQLite publica y falla
después el JSON, queda recuperación del índice derivado. Tampoco bloquea editores
externos de la fuente. No se migraron registros históricos.

## 13. Persistencia de proyectos sin reinicios silenciosos

`ProjectStore._load` antes trataba un error de lectura como corrupción, intentaba
mover el archivo a `.corrupt` y cacheaba una lista vacía; una escritura posterior
podía sustituir los proyectos. Ahora preserva ambos archivos y comunica el error,
reintenta PermissionError tres veces y valida la estructura antes de cachear.
Un store que ya vio datos persistidos no reinicializa una lista si desaparece el
archivo al invalidar la caché. La primera creación real sigue funcionando.

`_save` reutiliza `atomic_write_text`: temporal único, fsync y reemplazo con los
reintentos acotados de Windows. Si falla, no anuncia los nuevos datos en la caché.
81 pruebas del bloque proyectos/contexto pasan, incluidas 11 regresiones nuevas.
No se repararon ni renombraron datos reales del usuario.

## 14. Clientes oficiales visibles en Agentes

Agentes → Runners incorpora dos comprobaciones independientes de Codex y Claude
Code. Explica suscripción frente a API, clientes no instalados, falta de sesión,
conflictos, servidor antiguo y permisos. Cancelación y espera acotada; no realiza
login ni ejecuta tareas. Impeccable guio filas ligeras integradas con el sistema
existente, estados textuales y acciones móviles de 44 px. Español e inglés.

TypeScript, contrato JavaScript y build pasan. Prueba aislada de navegador con el
componente real en escritorio oscuro y móvil claro, sin llamadas a cuentas reales.
Contrato y límites: `docs/ui/runner-connections.md`. No sustituye la integración
pendiente de selección/orquestación dentro de la conversación.

## Investigación convertida en backlog (referencias)

Documento: `D:/LocalAI/inspiration/AUDITORIA_WORKSTATION_APPS_Y_COMMANDERTURTLE.md`.
Compara documentación oficial de ChatGPT, Claude, Gemini y Grok y una selección
de repositorios, con commits, archivos y criterios de aceptación. Distingue
capacidad ya existente, cambio local y propuesta. No se han instalado servicios,
descargado pesos ni copiado código externo.

Prioridad pendiente: identidad lógica de artefactos B-017 antes de ampliar su
distribución, resultados multimedia reutilizables, descubrimiento progresivo de
skills, evidencia trazable y preparación del equipo por capacidad. ART-1 exige
aprobación antes de migrar datos existentes; no se ha realizado esa migración.

## 15. Equipo del chat y panel de trabajo

`src/chat_team.py` guarda configuraciones por propietario/conversación con revisión
condicional. `ChatTeam.tsx` permite elegir miembros, rutas, roles, herramientas,
escritura y límites junto al modelo. El backend fija la configuración para cada
turno y aplica sus restricciones después de interpretar las tareas del modelo.
No permite inventar miembros ni usar otro destino si desaparece el elegido.

El panel reúne resultados, fuentes, agentes y capturas. Archivos Markdown/texto y
documentos tienen guardado explícito con detección de conflictos. Los borradores
pertenecen al recurso y chat, no al componente montado; una respuesta tardía de
guardado conserva las ediciones posteriores. Los archivos mantienen sus finales
de línea Windows y los enlaces locales se abren como enlaces accesibles.

Impeccable: revisión independiente pass tras corregir la carrera de guardado y
las áreas táctiles. Cinco capturas en entorno de componentes reales; no equivalen
a certificar toda la aplicación. Contrato: `docs/ui/studio-workbench.md`.

Prueba adicional en Studio real tras reiniciar el servidor: conversación QA
`0360bc4f-de9a-4fdd-be93-1731ca24aaa6`, Qwen 3.5 9B. Crear/guardar equipo, recargar,
aprobar una delegación aritmética, salir a Actividad y volver. Trabajador
`06d220ca` terminó sin cambios de archivos y el coordinador respondió en español.
No hubo llamadas a proveedores de pago ni se activaron runners externos.

Límites: faltan CLI como rutas del chat y plantillas de equipo por proyecto;
Navegador muestra capturas, no un navegador interactivo completo; borradores
persisten localmente en la sesión del navegador, no entre dispositivos.

## 16. Correcciones encontradas durante la integración

- Equipo cargado después de resolver la sesión y verificar propietario. JSON
  respeta modo, contexto de carpeta e incógnito, además del envío multipart.
- Cada agente usa las credenciales del endpoint al que se dirige. Corrige también
  los perfiles antiguos, que podían conservar las del coordinador al cambiar URL.
- La cola del equipo emite estado visible aunque haya cupo global disponible.
- Esperar aprobación no se contabiliza como fallo de ejecución en nuevos resúmenes;
  tampoco se considera ejecución exitosa. Etiquetas del tablero corregidas ES/EN,
  y detener un agente ya no se etiqueta como atasco.
- Codex pasa el contexto por stdin, según su ayuda instalada y documentación
  oficial. Los ejecutores leen salida y supervisan cancelación/timeout mientras
  envían el contexto; un hijo que no lee stdin ya no bloquea esa vigilancia.
  Entrada/salida UTF-8 conserva español y otros caracteres en Windows.
- Codex usa eventos JSON del cliente oficial: progreso de herramientas, respuesta,
  error terminal, consumo y sesión reanudable por ID exacto (nunca `--last`). No se
  muestra el payload de razonamiento privado. Un error terminal del cliente cuenta
  como error aunque el proceso termine con código cero; un aviso recuperable no.
  Referencia: [modo no interactivo oficial](https://learn.chatgpt.com/docs/non-interactive-mode).
  Pruebas de protocolo con procesos simulados, sin consumo de cuota; esto no añade
  todavía el cliente oficial al selector de modelos del chat.
- Persistencia del panel: incluye confirmaciones tardías en conversaciones no
  visibles, evita reescribir estados sin cambios y excluye incógnito/capturas/stream
  transitorio. Si el almacenamiento está lleno, mantiene borradores en memoria.
  Renombrar sólo actualiza metadatos; restaurar compara contenido base en servidor;
  controles de archivado/restauración bloqueados con borrador o mutación en curso.
  Las respuestas tardías no navegan a un recurso distinto del seleccionado.

## 17. Informes y exportación

Los informes Markdown y visuales muestran las métricas de citas calculadas, sin
inventarlas cuando faltan. El informe HTML las escapa. Las propiedades DOCX/PDF
distinguen documentos de chats, conservando la exportación anterior de chats.
La traducción completa de las etiquetas del informe permanece fuera de este lote.

## 18. Clientes oficiales desde el chat y supervisión

`docs/design/official-client-chat.md` detalla la nueva ruta privada de Claude Code:
suscripción/API explícita, autenticación comprobada antes de enviar cada tarea,
sin fallback de cobro y con herramientas nativas desactivadas. Faustus mantiene
su propio bucle, contexto, herramientas y permisos. Alta desde el selector del
chat reutilizando la conexión API existente; no cambia el modelo seleccionado.
Reintentar el alta no duplica ni reactiva una conexión revocada. Carrera real de
dos conexiones SQLite cubierta en pruebas, sin prompts a cuentas reales.

Impeccable guio la revisión acotada de los formularios: foco/teclado, controles
de 44 px, texto legible, estados de error y anchura móvil. La prueba visual final
se hizo con los componentes reales a 1920/390 px. No es sólo una captura estática.

El instalador, los diagnósticos y las consultas Cookbook conservan su proceso si
la cancelación llega durante el arranque y acotan limpieza/salida. Cookbook usa
Bash también en Windows. Regresiones con procesos Python inertes, no comandos
remotos. Los ejecutores siguen apagados globalmente. Codex como modelo del chat,
generación Claude real y equipo mixto real siguen pendientes de verificación.

## 19. Confianza SSH, correo y contexto verificable

`docs/ui/ssh-trust.md` documenta el emparejado explícito desde servidores guardados:
inspeccionar huellas, comparar una huella SHA256 por canal independiente y confirmar.
Una clave cambiada no tiene aceptación automática. Desvincular exige una acción
separada; las mutaciones son humanas y de mismo origen. Pruebas visuales con el
componente real y transporte simulado EN/ES, escritorio/móvil; ninguna huella real
de `known_hosts` fue cambiada para probarlo. El servidor auxiliar de QA se detuvo.

Correo programado pasa el dueño tanto al adjuntar como al limpiar el staging.
No se ha enviado correo real. Contratos de proyectos añadidos: permisos, orden de
fuentes, contadores y negativas con HTTP 200. Un estado desconocido no se presenta
como sano y una respuesta sin vínculo no se anuncia como guardada. No se cambia
la autoridad de los permisos de archivo, que sigue en el servidor.

## 20. Citas y cancelación de workflows

Siete regresiones reproducían la división de `Fig. 3`, títulos como `Dr.`/`Dra.` y
ejemplos `p. ej.`/`e.g.`, además de comillas de cierre perdidas en la afirmación.
Corregidas sin quitar la regla de cita pospuesta a un porcentaje. La segmentación
sigue siendo heurística: no se promete reconocimiento universal de abreviaturas.

Cinco regresiones reproducían cancelaciones de workflows que se convertían en
completado/pausado/fallido y permitían ejecutar el siguiente paso. `set_run_status`
usa escritura condicional y no sobrescribe estados terminales. El motor comprueba
la parada antes del handler y entre pasos, conserva el resultado de uno que ya
había empezado y no inicia sus sucesores. El avance HTTP trabaja fuera del bucle
principal, de modo que cancelar se atiende mientras sigue activo un handler.
Una prueba HTTP concurrente verifica ese caso con un handler inerte. Esto no
cancela retroactivamente una entrega externa ni añade aún el scheduler duradero.

## 21. Horas del correo sin reinterpretar la zona del remitente

La activación histórica de la respuesta de ausencia estaba guardada en UTC sin
offset. Al comparar con un correo, el código le asignaba la zona del remitente,
cambiando el instante: podía contestar correo antiguo o saltarse uno nuevo. Se
reprodujeron ambos casos con `+0200` y `-0500`; ahora se normalizan como UTC los
valores históricos sin zona. También se corrige el cooldown: `timestamp()` nunca
recibe un UTC sin zona para reinterpretarlo como hora local del sistema.

Los usos obsoletos de `utcnow()` en rutas/pollers se sustituyen por el helper
existente `core.database.utcnow_naive` cuando el almacenamiento espera ese formato.
No se cambian fechas guardadas ni se añade un sufijo que rompa comparaciones SQLite.
Validación: **35 correctos**, convirtiendo avisos de deprecación en errores,
`logs/astra-mail-utc.xml`. No se habilitó auto-respuesta ni se envió correo real.

## Validación y estado de ejecución

- Lote clientes: suite completa **12.537 correctos, 82 omitidos, 125 avisos**, sin
  fallos, en 21 min (`logs/astra-cli-full-suite.xml`). Cambios posteriores a su
  colección verificados aparte: **239 correctos, 13 avisos** de clientes, contexto,
  citas y correo (`logs/astra-client-project-final-focused.xml`); **130 correctos**
  de exportaciones/assets (`logs/astra-export-brand-final-focused.xml`);
  **59 correctos** de cancelaciones/workflows (`logs/astra-workflow-cancellation-final.xml`).
  La última prueba de alta concurrente pasa en un lote de **11 correctos**.
  Los lotes se solapan: no sumar como pruebas únicas. TypeScript, traducciones
  generadas (5468 cadenas) y build correctos. Avisos de fuentes/chunk grande
  permanecen. El reinicio de 16:18 no carga los arreglos backend posteriores.

- Lote panel/equipo: segunda suite completa **12.480 correctos, 2 fallos, 82
  omitidos, 125 avisos** (20 min 17 s). Fallos reparados: lectura prematura de equipo
  para JSON y contenedor clicable del Markdown. Repetición focalizada: **35 correctos**.
  Nueva suite completa terminada: **12.486 correctos, 82 omitidos y 125 avisos**,
  sin fallos, en 18 min 37 s (`logs/astra-workbench-tests.xml`).
  Cambios posteriores a su colección comprobados en la pasada final focalizada:
  **127 correctos, uno omitido y cuatro avisos**, 11,34 s
  (`logs/astra-workbench-final-focused.xml`). Incluye conflictos al restaurar,
  persistencia en segundo plano, equipos, eventos Codex y exportaciones.
  TypeScript y build final correctos. Tras el intento de reinicio bloqueado,
  Luis pidió usar sus lanzadores: `D:\LocalAI\Restart-Faustus.bat` ejecutado el
  07-09 a las 15:47; PID nuevo 84044, puerto 7000 y HTTP 200 comprobados. Backend
  actualizado activo; el arranque correcto no sustituye la prueba funcional en vivo.
- Pruebas adicionales: **91 correctos, 1 omitido** de ejecutores; **101 correctos**
  de equipos/perfiles/exportación/historial; **39 correctos** de permisos/tablero/
  contratos frontend; **85 correctos, 1 omitido** de informes/exportación. Los lotes
  se solapan: no sumar sus recuentos como pruebas únicas. TypeScript y build pasan.

- Tanda conversiones/conexiones/contexto (07-09-2026): suite completa con
  **12.446 correctos, 20 fallos, 82 omitidos y 126 avisos**, 17 min 44 s.
  Registro `logs/astra-workstation-next.xml`. Los 20 fallos se reprodujeron
  recogiendo toda la suite y seleccionando únicamente multimedia: eran ContextVars
  antiguas retenidas por las fixtures tras sustituciones del módulo durante la
  colección. Las fixtures ahora resuelven el contexto actual; no se cambiaron ni
  relajaron permisos de producción. Repetición con toda la suite recogida:
  **59 correctos, 12.490 no seleccionados**, `logs/astra-media-full-collection.xml`.
  No se afirma una segunda pasada completa limpia. Además, **27 correctos** de
  conexiones/contratos JavaScript; TypeScript y build pasan. El test JavaScript de
  conexiones se añadió después de comenzar la pasada completa, por eso se repitió
  aparte. Siguen pendientes los avisos de la suite y la validación real con LLM.
- Suite completa: **12.334 correctos, 82 omitidos, 126 avisos**, 12 min 37 s.
  Registro `logs/astra-workstation-tests.log`, JUnit del mismo nombre.
  Se ejecutó antes de la última corrección de Actividad, PDF y autenticación.
- Tras Actividad/PDF: **181 pruebas correctas** del bloque de contexto, documentos,
  actividad y guardas. TypeScript, ambas comprobaciones de Actividad y build pasan.
- Tras autenticación y escritura atómica: **166 pruebas correctas**, incluyendo
  usuarios, sesiones, permisos, primer arranque y persistencia de configuraciones.
  Los avisos de excepciones de hilo se trataron como errores. Registro:
  `logs/astra-auth-tests.log`. No se ha repetido toda la suite tras estos cambios.
- Última integración multimedia/portapapeles/proveedores: **286 tests correctos**,
  TypeScript y build pasan. Después del guardado condicionado a backend actualizado,
  se repiten los **171 tests** de endpoints/adaptadores, TypeScript y build.
  La conexión guiada exige que el backend anuncie la corrección de privacidad;
  como el servidor abierto no se ha reiniciado, mostrará que hace falta reiniciarlo
  antes de permitir crear una conexión privada. No se reinicia por iniciativa propia.
- El aviso de hilo del lector concurrente de `auth.json` se reprodujo como fallo
  real al tratar esos avisos como errores. Se corrigió **la prueba** para que
  reintente bloqueos transitorios de Windows, detecte otros errores, exija lecturas
  válidas y compruebe todos los usuarios al finalizar. No se silencia globalmente
  el aviso. El fallo del escritor que afloró después y su corrección se describen
  por separado en el apartado 6.
- Los avisos del build sobre fuentes/chunk grande siguen presentes.
- Ningún cambio posterior a `4aad6e6` preparado, commiteado o publicado. Backend
  reiniciado para el lote panel/equipo; frontend estático reconstruido. Las pruebas
  anteriores que indicaban servidor sin reiniciar describen su estado de entonces.

## Continuación

### Continuidad multimedia sin pestaña abierta (07-09-2026)

`media_scheduler.py` recoge únicamente renders previamente enviados. Recorre lotes,
reconcilia envíos pendientes con una ventana de gracia y no repite prompts. Un fallo
al descargar conserva el run y su motivo para reintentarlo; no declara éxito sin
archivos. Las escrituras condicionales y los bloqueos acotados evitan revertir una
cancelación o descargar cuatro veces por cuatro consultas simultáneas.

ComfyUI descarga por bloques a un temporal, verifica tamaño y finalización, y publica
sin sobrescribir. Las pruebas cubren respuestas HTTP truncadas tanto con longitud
declarada como con transferencia chunked. Cancelar después de que el motor termine
no elimina el resultado aún no recogido. Los workflows recargan resultados duraderos
cuando el worker ya terminó antes de su siguiente despertar.

Pruebas: `astra-media-truncated-cancel.xml`, 60 correctas;
`astra-media-workflow-recovery-final.xml`, 33 correctas. Lotes solapados.
Reinicio real 18:27, PID 51660. El servidor recogió automáticamente el run
`mrun_51870e80569c420383ee` en ocho segundos y guardó la ocurrencia
`occ_978d699ad1915ef3b39c7a26f8228208`. Es una imagen sintética válida de 8×8 servida
por un fixture HTTP local: prueba transporte/persistencia/continuidad, no calidad
de generación ni disponibilidad de GPU. Actividad muestra estado completado y su
descarga. El helper de QA se detiene al terminar; no quedó otro motor instalado.

### Resultados duraderos y recuperables (07-09-2026)

ART-1 conectado: copia aditiva en segundo plano, ocurrencias por evento, alias
heredados, previews, eliminación sin resurrección y publicación atómica. El
catálogo compartido alimenta contexto/State Mirror; HTTP verifica propietario.
Los workflows guardan informes reales y Actividad ofrece enlaces a sus archivos
y los de renders. Impeccable guio esta extensión localizada de Actividad, sin
cambiar el tema ni crear otra pantalla. Comprobadas etiquetas EN/ES, nombres
largos y enlaces de 44 px en escritorio; el override móvil del controlador no
se aplicó a la pestaña medida y no se cuenta como validación móvil nueva.

Pruebas: 126 correctas en consumidores, 49 en migración/identidad/workflows y
112 en rutas/Actividad. No se suman lotes solapados. Reinicio 18:03:52, PID 74440,
HTTP 200. Copia SQLite en `logs/artifact-cutover-backup-20260907.db`; había cero
artefactos históricos. Workflow real `wfr_121604eaaf5d49c2a5ea` finalizó por el
worker y guardó `prueba-workflow.md`, sin modelo ni efectos externos.

### Workflows visibles y continuados por el servidor (07-09-2026)

`src/workflows/scheduler.py` continúa ejecuciones ya iniciadas aunque no haya
navegador. No lanza borradores ni decide aprobaciones. El motor usa identidades
por intento y heartbeat; recuperación y publicación compiten mediante escrituras
condicionales. Un proceso antiguo no publica ni inicia un sucesor tras perder
su intento. Las esperas aceptan zonas horarias y no comparan fechas como texto.

Actividad incorpora workflows con pasos, nombres de dependencias, horas de
reanudación, acceso a aprobaciones e inicio/cancelación. Mantiene lecturas antiguas
como tales si falla la conexión. Listado limitado sin inputs ni prompts; acciones
con respuesta comprobada, estados de carga y timeout. Revisión sintética ES/EN
de escritorio y móvil: sin desbordamientos y controles de 44 px en móvil.

Pruebas específicas: 48 correctas de scheduler/carreras/handlers/media antes del
ajuste de reloj; 41 correctas de reloj/scheduler/handlers; 44 correctas de rutas,
scheduler y adaptadores de Actividad. Los lotes se solapan y no se suman.

El resumen de turno también distingue «permiso respondido» de «esperando permiso»
y una herramienta sin completar por aprobación de una que ha fallado. Comprobado
en el chat real de delegación ya existente, sin generar otra respuesta de pago.

README inglés renovado y README español completo, con los once sistemas y las
capacidades de chat, equipos, multimedia, proveedores y voz implementadas. Ficha,
destacados y contenido fuente del CV de Faustus actualizados en `portfolio-react`
en ambos idiomas. Lint, build y `check:cv` correctos; los botones reales abrieron
CV español e inglés con los nuevos textos, conservando 17 proyectos, 8 entradas
y 150 habilidades. No se ha generado un PDF ni publicado o hecho commit.

Doctor incorpora catálogo Ollama, memoria JSON, heartbeat Chroma y estado del
navegador. Las comprobaciones no generan, abren páginas ni modifican memorias;
respuestas de red y lectura de archivos acotadas. Los errores no incluyen URLs
con credenciales y el filtro de áreas evita sondas ajenas. Corregidos mensajes
obsoletos de continuación manual y falta de ayuda ante disco escaso. 30 pruebas
correctas en `logs/astra-doctor-services.xml`, posteriores a la colección de la
regresión general en curso.

La regresión general posterior a artefactos terminó con 12.646 pruebas correctas,
3 fallos y 82 omitidas (19m22s). Corregidos: aceptación Docker que todavía leía
ArtifactRow en vez del catálogo de ocurrencias; referencias de capturas en README;
advertencias explícitas AUTH_ENABLED y LOCALHOST_BYPASS. Eliminados los 16 avisos
UTC observados. Repetición del bloque afectado: 179 correctas, 1 omitida, tratando
deprecaciones como errores (`astra-regression-fixes-final.xml`).

Persistencia de numeración de citas: snapshot versionado, independiente del orden
o filtrado de hallazgos. Restauración antes de nuevas fuentes; rechazo de estados
dañados antes de llamar al proveedor, sin reiniciar silenciosamente una investigación.
Guardado privado atómico y lectura de continuaciones por dueño. 445 pruebas del
bloque research/citas correctas (`astra-research-regression.xml`); no inferencia
pagada para estas pruebas.

Exportaciones de informes EN/ES: etiquetas, fuentes, pies, idioma HTML y asunto
DOCX/PDF coherentes; contexto local por exportación, no un diccionario global
mutable compartido por usuarios. 153 correctas y 1 omitida en la regresión de
exportaciones (`astra-export-locales.xml`). TXT conserva sangría de listas
profundas y código; lector DOCX nativo conserva encabezados y limita a 16 MiB el
XML descomprimido antes de leerlo. Instalado el extra Office MarkItDown 0.1.6
después de terminar la suite, conservando ONNX 1.29.0 y NumPy 2.5.2; `pip check`
correcto y conversión DOCX comprobada con el convertidor real.

State Mirror recoge datos reales de uso antes de un barrido periódico con dueño,
habilitado y sin conversación activa. La página de uso ya no tiene que estar
abierta. La recogida vive en el bucle del servidor; los adaptadores siguen en
hilos, con timestamp original y sin afirmar frescura si falla la sonda.
33 pruebas correctas (`astra-state-runtime-refresh-final.xml`). Estos cambios
de exportación/lector/State Mirror son posteriores a la colección de la segunda
suite general. Su bloque posterior junto con Office y permisos Codex pasa con
199 correctas y 1 omitida (`astra-office-scopes-runtime.xml`). Reinicio 19:39,
PID 34696 y HTTP 200. La segunda suite general terminó con 12.687 correctas,
82 omitidas y cero avisos (`astra-post-citations-regression.xml`, 19m35s).

La autorización de tokens Codex ya no admite toda la familia por una regla
global. Métodos/rutas explícitos por recurso, rechazo de rutas nuevas y permisos
conjuntos para correo convertido a documento; se conservan dueño y admin.

Investigación comparte ahora la ejecución de `src/research_handler.py`: el adaptador
de servicios sólo cambia la presentación enriquecida. Conserva fuentes, URLs inspeccionadas,
continuaciones, dueño, guardado atómico, timeout y recuperación parcial, sin mantener
otro motor antiguo. El sexto argumento posicional de headers sigue siendo compatible.
Confirmaciones españolas («sí», «dale», «continúa») conservan el tema original si falla
la síntesis. 462 pruebas correctas (`astra-research-shared-final.xml`).

State Mirror observa HEAD mediante el ejecutor Git confinado por argumentos, con
caché y timestamp originales; verificado en Git temporal, detached HEAD y worktree.
101 pruebas correctas (`astra-workspace-head.xml`). Sus conflictos se cierran al
reconfirmarse muestras frescas y concordantes de las fuentes, no por repetición de
una sola ni por antigüedad. Un tercero discrepante bloquea el cierre. La escritura
comprueba que no entraron observaciones concurrentes y publica una sola resolución.
75 pruebas correctas de persistencia/servicio/consultas/HTTP (`astra-state-convergence.xml`).
Este bloque de conflictos es posterior a la colección de la tercera suite general.

Últimas correcciones: reranking opcional y privado de herramientas (86 pruebas),
esperas SMTP/configuración y búsqueda de emergencia fuera del bucle principal
(14 pruebas), y consumo de autorizaciones con escritura condicional para impedir
gastar dos veces el mismo permiso (33 pruebas). No se ha enviado correo real.

La tercera suite terminó con 12.749 correctas, 81 omitidas y dos fallos de
inspección de fuente: se modificó el programador mientras el proceso conservaba
los números de línea anteriores. El bloque afectado, repetido sin cambios de
fuente en un proceso nuevo, pasa sus tres pruebas. No equivale a una nueva suite
completa limpia; queda repetirla congelando el código durante la ejecución.

Luis ha autorizado un commit y push de recuperación de Faustus si la versión
actual compila y arranca. No es una declaración de que todos los planes estén
terminados. El portfolio permanece separado y sin publicar. Las siguientes
mejoras continúan localmente después del punto de recuperación; no hay una
programación recurrente ni autorización para capturar el micrófono por iniciativa
propia. Se conserva aquí el alcance comprobado de cada cambio.
