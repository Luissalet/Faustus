# Catálogo de 156 features y profundizaciones

Todas las filas son trabajo propuesto. «Profundizar» o «integrar» significa ampliar una base observada; «introducir» significa capacidad no localizada como experiencia completa en la revisión, no ausencia probada en todo el repositorio. «Auditar» requiere primero reproducir y trazar. «Opcional» no bloquea Creator inicial.

Prioridad: P0 cimientos; P1 primeras producciones completas; P2 ampliación avanzada; P3 extensiones posteriores. El presupuesto y la complejidad pertenecen al paquete de implementación; no sumar tamaños de features como estimación de calendario.

## Creator como espacio de trabajo
Existen proyectos, Chat/Agent, adjuntos y workbench; no se verificó un espacio Creator integral.
Fuentes e inspiración: [F01](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/README.md), [F12](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/services/projects.py), [F17](https://github.com/Luissalet/Faustus/tree/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/studio/src). La especificación que sigue es una propuesta de Faustus, no una atribución literal de cada función a cada fuente.

### UX01 — Perfil Creator independiente de Chat/Agent
**P0 · integrar · [WP02](07_ROADMAP_Y_PAQUETES.md#wp02) · fase 1.**
Añadir una especialización visual y de herramientas sin alterar las reglas del modo de ejecución ni cambiar el project_id.
**Aceptación:** Un mismo proyecto alterna Creator y conversación y conserva fuentes, permisos y mensajes.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### UX02 — Chat y selección del medio sincronizados
**P0 · profundizar · [WP05](07_ROADMAP_Y_PAQUETES.md#wp05) · fase 1.**
Enviar al compositor un chip de escena, región de imagen, cue o rango de audio con versión y contexto mínimo, no el archivo completo por defecto.
**Aceptación:** Una selección sobre una versión antigua queda obsoleta y no se aplica a la nueva por coincidencia textual.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### UX03 — Layouts orientados a tarea
**P0 · introducir · [WP05](07_ROADMAP_Y_PAQUETES.md#wp05) · fase 1.**
Layouts de imagen, montaje, localización y música que muestran lo pertinente; modo simple y avanzado sobre las mismas entidades.
**Aceptación:** Cambiar layout no duplica proyecto, sesión ni estado editorial.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### UX04 — Inspector de propiedades contextual
**P0 · introducir · [WP05](07_ROADMAP_Y_PAQUETES.md#wp05) · fase 1.**
Mostrar propiedades del objeto seleccionado, controles compatibles, procedencia y cambios pendientes frente a aceptados.
**Aceptación:** Modificar un campo no dispara generación; guardar un control inválido muestra motivo y conserva el borrador.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### UX05 — Revisión A/B y elección de variantes
**P1 · profundizar · [WP20](07_ROADMAP_Y_PAQUETES.md#wp20) · fase 2.**
Comparar originales y propuestas con deslizador, mosaico o reproducción sincronizada; elegir qué versión pasa al montaje.
**Aceptación:** Aceptar una variante fija su occurrence y revisión; nuevas generaciones no sustituyen la elegida.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### UX06 — Borradores y recuperación de sesión
**P0 · profundizar · [WP05](07_ROADMAP_Y_PAQUETES.md#wp05) · fase 1.**
Persistir por usuario y proyecto paneles, formularios, selección y edición aún no enviada; aviso de conflicto tras reconectar.
**Aceptación:** Cerrar la vista y volver restaura el borrador sin reenviar mensajes ni lanzar otro render.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### UX07 — Cola visible dentro del estudio
**P0 · integrar · [WP05](07_ROADMAP_Y_PAQUETES.md#wp05) · fase 1.**
Proyectar MediaRun, workflows y Attention en una vista de producción con causa de espera, motor, siguiente acción y outputs.
**Aceptación:** Esperando GPU, permiso y conexión perdida se distinguen; no hay una segunda fuente de estado.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### UX08 — Comentarios anclados a tiempo y región
**P2 · introducir · [WP35](07_ROADMAP_Y_PAQUETES.md#wp35) · fase 4.**
Comentarios revisables en un fotograma, capa, palabra o compás con snapshot de referencia y resolución humana.
**Aceptación:** Un retiming permite mapear o declarar obsoleto el comentario; nunca lo pega a otro fotograma silenciosamente.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### UX09 — Atajos y command palette creativa
**P0 · profundizar · [WP05](07_ROADMAP_Y_PAQUETES.md#wp05) · fase 1.**
Añadir comandos para split, trim, marcar, comparar, transcribir y pedir variantes; misma política que botones y tools.
**Aceptación:** Atajo y botón invocan la misma operación con las mismas validaciones y undo.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### UX10 — Revisión responsive y accesible
**P2 · profundizar · [WP35](07_ROADMAP_Y_PAQUETES.md#wp35) · fase 4.**
Priorizar reproducción proxy, comentarios y aprobaciones claras en móvil, además de navegación por teclado, foco y reduced motion.
**Aceptación:** Una aprobación incluye el medio y los cambios relevantes incluso en pantalla pequeña.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### UX11 — Onboarding por intención y capacidad real
**P0 · profundizar · [WP08](07_ROADMAP_Y_PAQUETES.md#wp08) · fase 1.**
Preguntar qué se quiere producir, detectar servicios ya configurados y mostrar rutas posibles, faltantes y costes sin instalar nada de oficio.
**Aceptación:** Un proyecto de subtítulos funciona sin un modelo generador de vídeo; cada bloqueo ofrece una acción concreta.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### UX12 — Presets de producción editables
**P1 · profundizar · [WP41](07_ROADMAP_Y_PAQUETES.md#wp41) · fase 3.**
Guardar configuraciones completas de un trabajo exitoso como receta con variables, versiones y alcance, no sólo un prompt textual.
**Aceptación:** Reutilizar una receta muestra qué inputs cambian y exige revalidar permisos y despliegue.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

## Biblioteca y conocimiento multimedia
Hay artifact store con bytes/occurrences y enlaces de contexto; ampliar metadatos y navegación en vez de crear otro almacén.
Fuentes e inspiración: [F01](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/README.md), [F05](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/media_edit_projects.py), [F12](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/services/projects.py), [F13](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/project_identity.py), [A06](https://github.com/invoke-ai/InvokeAI/blob/main/README.md), [W03](https://invoke.ai/development/front-end/canvas-projects/). La especificación que sigue es una propuesta de Faustus, no una atribución literal de cada función a cada fuente.

### AST01 — Asset graph con derivados y dependencias
**P0 · profundizar · [WP03](07_ROADMAP_Y_PAQUETES.md#wp03) · fase 1.**
Unir originales, máscaras, proxies, transcripciones, stems, versiones y exports mediante aristas tipadas, conservando el propietario de cada occurrence.
**Aceptación:** Cada export puede recorrer sus entradas exactas; el hash de bytes no se usa como credencial.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### AST02 — Media bin con filtros y colecciones
**P0 · profundizar · [WP03](07_ROADMAP_Y_PAQUETES.md#wp03) · fase 1.**
Filtros por tipo, proyecto, escena, persona autorizada, estado de revisión, licencia y motor; colecciones sin mover físicamente los archivos.
**Aceptación:** Añadir a dos colecciones no duplica bytes ni fusiona permisos.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### AST03 — Ingesta recuperable y por lotes
**P0 · profundizar · [WP04](07_ROADMAP_Y_PAQUETES.md#wp04) · fase 1.**
Subidas por chunks, importación acotada de carpetas elegidas, detección de duplicados y progreso por elemento con checksum.
**Aceptación:** Un lote interrumpido continúa los elementos pendientes y no importa dos veces los ya confirmados.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### AST04 — Proxies, filmstrips y waveforms
**P0 · introducir · [WP04](07_ROADMAP_Y_PAQUETES.md#wp04) · fase 1.**
Generar derivados ligeros para seek, waveform y galerías sin decodificar el medio completo en el navegador.
**Aceptación:** La UI indica cuándo reproduce proxy y la exportación usa el original fijado, no el proxy accidentalmente.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### AST05 — Búsqueda semántica por fragmento
**P1 · profundizar · [WP26](07_ROADMAP_Y_PAQUETES.md#wp26) · fase 3.**
Indexar captions, transcripción y etiquetas por escena y rango; embeddings opcionales y versionados con recibo de cobertura.
**Aceptación:** Buscar devuelve un rango reproducible y su fuente; un caption generado no se presenta como hecho verificado.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### AST06 — Biblias visuales y referencias canónicas
**P1 · integrar · [WP21](07_ROADMAP_Y_PAQUETES.md#wp21) · fase 3.**
Extender Story Bible con vistas de personaje, ropa, paletas, entornos y referencias regionales aprobadas para reutilizarlas entre medios.
**Aceptación:** La referencia canónica sólo cambia tras aceptación; la etiqueta de personaje no garantiza identidad visual.
**Verificación requerida:** `gpu_opt_in_and_integration`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### AST07 — Versiones y ramas creativas
**P1 · profundizar · [WP12](07_ROADMAP_Y_PAQUETES.md#wp12) · fase 2.**
Variantes de composición, montaje y música como revisiones o ramas de un documento, con merge explícito de operaciones compatibles.
**Aceptación:** Una edición humana intermedia no se pierde al adoptar una propuesta del agente.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### AST08 — Limpieza y cuotas de almacenamiento
**P0 · profundizar · [WP03](07_ROADMAP_Y_PAQUETES.md#wp03) · fase 1.**
Presupuesto de scratch y retención separada para temporales, previews y finales; mostrar dependientes antes de borrar.
**Aceptación:** Un original referenciado no se elimina automáticamente; los tombstones impiden que reaparezca por migración.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### AST09 — Paquete portable con inventario
**P2 · profundizar · [WP34](07_ROADMAP_Y_PAQUETES.md#wp34) · fase 4.**
Exportar proyecto, medios elegidos, manifest de hashes, recetas, versiones y permisos depurados; perfil ligero con referencias externas explícitas.
**Aceptación:** La opción completo falla si falta un asset declarado; una reimportación no ejecuta código incluido.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### AST10 — Intercambio editorial OTIO con pérdidas visibles
**P2 · introducir · [WP34](07_ROADMAP_Y_PAQUETES.md#wp34) · fase 4.**
Exportar/importar el subconjunto soportado de cortes, pistas y referencias; listar efectos o keyframes no representables.
**Aceptación:** El roundtrip del subconjunto mantiene tiempos y orden; los efectos perdidos aparecen en un informe, no se descartan en silencio.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### AST11 — Metadatos y privacidad de exports
**P2 · profundizar · [WP34](07_ROADMAP_Y_PAQUETES.md#wp34) · fase 4.**
Ofrecer conservar, reducir o retirar metadatos personales de distribución manteniendo los recibos internos autorizados.
**Aceptación:** Una exportación pública no incluye rutas de usuario, claves o archivos privados ajenos al inventario.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### AST12 — Enlaces desde texto, código y documentación
**P0 · integrar · [WP03](07_ROADMAP_Y_PAQUETES.md#wp03) · fase 1.**
Permitir que un proyecto de escritura o software use capturas, diagramas, locuciones y vídeos de Creator sin duplicar fuentes.
**Aceptación:** Cambiar de tipo de trabajo conserva el enlace de contexto y la versión citada del asset.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

## Imagen, diseño y canvas
Hay editor por capas/máscaras, transformaciones y recetas ComfyUI; el compositor y el conditioning requieren profundización.
Fuentes e inspiración: [F04](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/media_workflows.py), [F05](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/media_edit_projects.py), [F16](https://github.com/Luissalet/Faustus/tree/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/media_backends), [A06](https://github.com/invoke-ai/InvokeAI/blob/main/README.md), [W02](https://invoke.ai/features/canvas/layers-and-drops/), [W04](https://invoke.ai/releases/). La especificación que sigue es una propuesta de Faustus, no una atribución literal de cada función a cada fuente.

### IMG01 — Semántica ordenada de operaciones
**P1 · profundizar · [WP12](07_ROADMAP_Y_PAQUETES.md#wp12) · fase 2.**
Sustituir el historial parcialmente interpretado por operaciones explícitas con alcance capa/documento y orden reproducible.
**Aceptación:** Añadir capa, rotar y recortar produce el mismo resultado en preview y export.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### IMG02 — Capas raster, máscaras y grupos
**P1 · profundizar · [WP20](07_ROADMAP_Y_PAQUETES.md#wp20) · fase 2.**
Visibilidad, bloqueo, opacidad, transforms y agrupación con referencias a blobs; mantener la compatibilidad con borradores actuales.
**Aceptación:** Ocultar o bloquear una capa modifica su comportamiento real; no es un control sólo visual.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### IMG03 — Texto y vectores editables
**P1 · introducir · [WP20](07_ROADMAP_Y_PAQUETES.md#wp20) · fase 2.**
Añadir títulos, formas y overlays vectoriales con tipografías autorizadas; rasterizar sólo al exportar o al enviar al motor cuando sea necesario.
**Aceptación:** Cambiar una palabra conserva el resto de la imagen y su fuente editable.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### IMG04 — Inpainting con máscara trazable
**P1 · profundizar · [WP11](07_ROADMAP_Y_PAQUETES.md#wp11) · fase 2.**
Resolver máscara y original a inputs exactos del backend; controlar strength, seed y límites espaciales soportados.
**Aceptación:** El recibo contiene hashes de imagen y máscara; una máscara distinta invalida la aprobación anterior.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### IMG05 — Outpainting y expansión de formato
**P1 · introducir · [WP20](07_ROADMAP_Y_PAQUETES.md#wp20) · fase 2.**
Expandir el lienzo con zonas nuevas, preservar el interior y elegir dimensiones compatibles, útil para adaptar portadas y miniaturas.
**Aceptación:** La región preservada no se modifica por el compositor salvo operación aceptada.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### IMG06 — Referencias regionales y de estilo
**P1 · profundizar · [WP21](07_ROADMAP_Y_PAQUETES.md#wp21) · fase 3.**
Condicionar regiones con referencias reales cuando el modelo lo soporte; distinguirlo de una descripción textual de estilo.
**Aceptación:** La UI expone cómo se aplicó cada referencia: texto, imagen global o conditioning regional.
**Verificación requerida:** `gpu_opt_in_and_integration`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### IMG07 — Control por pose, depth y bordes
**P1 · introducir · [WP21](07_ROADMAP_Y_PAQUETES.md#wp21) · fase 3.**
Pistas de control importadas o calculadas por adaptadores opcionales con intensidad y cobertura versionadas.
**Aceptación:** Se valida compatibilidad de ControlNet o equivalente con la familia del checkpoint antes de ejecutar.
**Verificación requerida:** `gpu_opt_in_and_integration`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### IMG08 — Selección asistida y extracción de objetos
**P1 · introducir · [WP20](07_ROADMAP_Y_PAQUETES.md#wp20) · fase 2.**
Segmentación opcional tipo SAM, selección manual de respaldo, refinado de bordes y conservación de alfa.
**Aceptación:** Un fallo del segmentador no elimina la selección humana ni declara el recorte perfecto.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### IMG09 — Upscale, restauración y tiled processing
**P1 · profundizar · [WP11](07_ROADMAP_Y_PAQUETES.md#wp11) · fase 2.**
Recetas explícitas para escalado/restauración con mosaicos y solape, avisando de detalle inventado y resolución nativa frente a reconstruida.
**Aceptación:** El export conserva su resolución de origen y el escalador usado; no se etiqueta upscale como captura nativa.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### IMG10 — Matriz de variantes reproducible
**P1 · introducir · [WP21](07_ROADMAP_Y_PAQUETES.md#wp21) · fase 3.**
Explorar combinaciones acotadas de prompt, seed, controles y modelo con presupuesto previo y comparador.
**Aceptación:** Cada celda apunta a una ejecución y sus inputs; cancelar no inicia el resto del lote.
**Verificación requerida:** `gpu_opt_in_and_integration`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### IMG11 — Paletas y consistencia de marca
**P1 · integrar · [WP21](07_ROADMAP_Y_PAQUETES.md#wp21) · fase 3.**
Brand kit de colores, fuentes, logotipos y restricciones como datos editables del proyecto; composición determinista para texto y logos críticos.
**Aceptación:** El logotipo se compone desde el asset aprobado en vez de confiar en que un modelo lo redibuje fielmente.
**Verificación requerida:** `gpu_opt_in_and_integration`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### IMG12 — Mockups, portadas y packs sociales
**P1 · introducir · [WP22](07_ROADMAP_Y_PAQUETES.md#wp22) · fase 3.**
Plantillas de composición para libro, miniaturas, banners y producto; variantes por ratio que comparten contenido pero tienen encuadre revisable.
**Aceptación:** Cada formato tiene preview propio y márgenes seguros; no se estira una imagen para simular otro encuadre.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### IMG13 — Generación guiada por boceto y región
**P1 · profundizar · [WP20](07_ROADMAP_Y_PAQUETES.md#wp20) · fase 2.**
Arrastrar un boceto o pintar sobre una región para solicitar un cambio acotado, con before/after y commit separado.
**Aceptación:** Una propuesta fallida no modifica el lienzo aceptado; se puede descartar sin reconstruir el proyecto.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### IMG14 — Visual QA y revisión estética separadas
**P1 · profundizar · [WP36](07_ROADMAP_Y_PAQUETES.md#wp36) · fase 2.**
Comprobar dimensiones, alfa, archivos corruptos y texto compuesto de forma determinista; revisión estética humana o asistida etiquetada como tal.
**Aceptación:** Una puntuación del modelo no sustituye comprobar que el archivo existe ni demuestra calidad artística.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

## Vídeo, montaje y producción
Hay vídeo local acotado y renders ComfyUI; no se localizó un NLE integral. OpenCut orienta UX, no prueba que su reescritura esté lista.
Fuentes e inspiración: [F01](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/README.md), [F02](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/media_runs.py), [F03](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/media_scheduler.py), [A07](https://github.com/OpenCut-app/OpenCut/blob/main/README.md), [A08](https://github.com/OpenCut-app/opencut-classic/blob/main/README.md), [A09](https://github.com/Lightricks/LTX-2/blob/main/README.md), [A10](https://github.com/Wan-Video/Wan2.2/blob/main/README.md), [A11](https://github.com/AcademySoftwareFoundation/OpenTimelineIO/blob/main/README.md), [W05](https://docs.ltx.video/). La especificación que sigue es una propuesta de Faustus, no una atribución literal de cada función a cada fuente.

### VID01 — Timeline multipista no destructiva
**P1 · introducir · [WP13](07_ROADMAP_Y_PAQUETES.md#wp13) · fase 2.**
Clips de vídeo, imagen, voz, música, textos y captions con trims, splits, enlaces y locks; operaciones tipadas compartidas con agentes.
**Aceptación:** Mover un clip enlazado mantiene la relación audio/vídeo según el modo elegido.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### VID02 — Relojes exactos y VFR
**P1 · introducir · [WP13](07_ROADMAP_Y_PAQUETES.md#wp13) · fase 2.**
Tiempo racional para vídeo, índices de muestra para audio y mapa PTS para grabaciones variables; políticas explícitas al convertir a CFR.
**Aceptación:** Cien cortes consecutivos no acumulan desfase por floats y la sincronía se verifica con fixtures.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### VID03 — Montaje por instrucciones en lenguaje natural
**P1 · introducir · [WP22](07_ROADMAP_Y_PAQUETES.md#wp22) · fase 3.**
Interpretar peticiones de editar planos o rangos como una propuesta de operaciones sobre la revisión actual, no como shell generado.
**Aceptación:** La UI permite revisar los cortes propuestos antes de aplicarlos; un target ambiguo no se resuelve al azar.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### VID04 — Storyboard y animatic editables
**P1 · introducir · [WP22](07_ROADMAP_Y_PAQUETES.md#wp22) · fase 3.**
Del guion a planos con duración, cámara, referencias, diálogo, sonido y restricciones; animatic con imágenes aunque no haya vídeo generativo.
**Aceptación:** El animatic exporta con los tiempos del storyboard y conserva sus fuentes.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### VID05 — Generación por plano y continuidad
**P2 · introducir · [WP23](07_ROADMAP_Y_PAQUETES.md#wp23) · fase 4.**
Generar tomas independientes, mantener referencias canónicas y evaluar continuidad antes de unir; regenerar sólo un plano o transición.
**Aceptación:** Rehacer el plano 4 no sustituye los planos 1–3 aceptados.
**Verificación requerida:** `gpu_opt_in_and_integration`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### VID06 — Tareas de vídeo por variante de modelo
**P2 · profundizar · [WP23](07_ROADMAP_Y_PAQUETES.md#wp23) · fase 4.**
Distinguir T2V, I2V, speech-to-video, animación y reemplazo; no activar herramientas por compartir nombre de familia.
**Aceptación:** Un checkpoint de I2V no recibe una operación S2V a menos que su adaptador la declare y la valide.
**Verificación requerida:** `gpu_opt_in_and_integration`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### VID07 — Retake y extensión acotados
**P2 · introducir · [WP23](07_ROADMAP_Y_PAQUETES.md#wp23) · fase 4.**
Regenerar un intervalo, extender inicio/final o reencuadrar cuando el deployment lo soporte, preservando handles y empalmes.
**Aceptación:** Las capacidades de API y local se comprueban por separado y la operación no sustituye el resto sin avisar.
**Verificación requerida:** `gpu_opt_in_and_integration`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### VID08 — Reframing y safe areas por destino
**P1 · introducir · [WP22](07_ROADMAP_Y_PAQUETES.md#wp22) · fase 3.**
Reencuadre manual o asistido, seguimiento opcional del sujeto y overlays de zonas seguras para formatos verticales/horizontales.
**Aceptación:** La exportación muestra el encuadre real y permite corregir recortes antes de gastar en regeneración.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### VID09 — Transiciones, keyframes y velocidad
**P1 · introducir · [WP13](07_ROADMAP_Y_PAQUETES.md#wp13) · fase 2.**
Transiciones de un subconjunto documentado, keyframes de transforms/ganancia y curvas de velocidad con límites y retiming map.
**Aceptación:** Una rampa de velocidad actualiza o marca obsoletos captions, comentarios y voz vinculados.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### VID10 — Motion graphics y títulos
**P1 · introducir · [WP14](07_ROADMAP_Y_PAQUETES.md#wp14) · fase 2.**
Plantillas declarativas de títulos, lower thirds, visualizadores y lyric videos; código arbitrario requiere el sandbox y permisos de scripts existentes.
**Aceptación:** Un template de títulos produce los mismos tiempos de texto en preview y render final.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### VID11 — Detección de escenas y selección de highlights
**P1 · introducir · [WP22](07_ROADMAP_Y_PAQUETES.md#wp22) · fase 3.**
Detectar cortes y proponer segmentos a partir de transcripción y señales visuales; criterios de selección editables y sin promesa de viralidad.
**Aceptación:** Cada highlight enlaza al intervalo fuente y puede revisarse antes del montaje.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### VID12 — Exports por lotes y perfiles técnicos
**P1 · profundizar · [WP14](07_ROADMAP_Y_PAQUETES.md#wp14) · fase 2.**
Renderizar variantes con resolución, fps, bitrate, codec y audio explícitos; detectar soporte real de encoders de cada máquina.
**Aceptación:** Un encoder ausente provoca cambio aprobado o bloqueo, no otra calidad de salida silenciosa.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### VID13 — Renders recuperables y outputs verificables
**P0 · profundizar · [WP10](07_ROADMAP_Y_PAQUETES.md#wp10) · fase 1.**
Extender el lifecycle existente a ensamblado y workers externos; reconciliar envío, progreso, finalización y descarga por separado.
**Aceptación:** Reiniciar tras submit no genera otra toma ni declara final un archivo parcial.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### VID14 — Control de calidad temporal
**P1 · introducir · [WP37](07_ROADMAP_Y_PAQUETES.md#wp37) · fase 3.**
Fixtures de duración, PTS, sincronía, sonido, subtítulos visibles y decodificación; revisión perceptual del montaje como paso separado.
**Aceptación:** El gate técnico detecta frames perdidos o audio inexistente sin depender de que el agente afirme éxito.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

## Subtítulos, traducción y localización
Existen segmentos y SRT/VTT; alineación por palabra, glosarios y localización profunda son extensiones. Los límites de VideoLingo no se heredan como soluciones.
Fuentes e inspiración: [F06](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/media_subtitles.py), [F07](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/media_capabilities.py), [A03](https://github.com/Huanshere/VideoLingo/blob/main/README.md), [A04](https://github.com/m-bain/whisperX/blob/main/README.md). La especificación que sigue es una propuesta de Faustus, no una atribución literal de cada función a cada fuente.

### SUB01 — ASR con palabras y alineación
**P1 · profundizar · [WP15](07_ROADMAP_Y_PAQUETES.md#wp15) · fase 2.**
Transcripción por segmentos y palabras con tiempos, alineador, idioma y confianza opcional; guardar texto bruto junto a revisiones.
**Aceptación:** Una palabra no alineada queda sin precisión declarada; no se inventan timestamps para completar la tabla.
**Verificación requerida:** `gpu_opt_in_and_integration`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### SUB02 — Diarización revisable
**P1 · introducir · [WP15](07_ROADMAP_Y_PAQUETES.md#wp15) · fase 2.**
Identificar turnos de speaker_id, corregir agrupaciones manualmente y mantener separados los nombres reales o personajes asignados.
**Aceptación:** Un speaker_1 nunca se convierte en persona identificada sin un dato explícito.
**Verificación requerida:** `gpu_opt_in_and_integration`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### SUB03 — Cambio de idioma por fragmento
**P1 · introducir · [WP15](07_ROADMAP_Y_PAQUETES.md#wp15) · fase 2.**
Permitir alineación por idioma de segmento y texto no alineado como fallback visible; tratarlo como ampliación propia, no capacidad garantizada de VideoLingo.
**Aceptación:** Una frase secundaria no desaparece por usar un alineador del idioma principal.
**Verificación requerida:** `gpu_opt_in_and_integration`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### SUB04 — Segmentación editorial configurable
**P1 · profundizar · [WP16](07_ROADMAP_Y_PAQUETES.md#wp16) · fase 2.**
Aplicar perfiles de caracteres por línea, velocidad de lectura, pausas y número de líneas, adaptados al idioma y destino.
**Aceptación:** Se puede elegir dos líneas; no se fuerza una supuesta norma universal de línea única.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### SUB05 — Edición texto-waveform-timeline
**P1 · introducir · [WP16](07_ROADMAP_Y_PAQUETES.md#wp16) · fase 2.**
Editar cue y timing con búsqueda, split/join, desplazamientos colectivos y previsualización del subtítulo en vídeo.
**Aceptación:** Modificar un cue mantiene referencias fuente y registra quién cambió qué y cuándo.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### SUB06 — Glosario de proyecto y traducción consistente
**P1 · introducir · [WP17](07_ROADMAP_Y_PAQUETES.md#wp17) · fase 3.**
Nombres propios, terminología, tratamiento y registro como glosario versionado, compartido con escritura, voz y captions.
**Aceptación:** Al cambiar una traducción canónica se muestran las apariciones afectadas antes de sustituirlas.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### SUB07 — Traducción, crítica y adaptación medibles
**P1 · introducir · [WP17](07_ROADMAP_Y_PAQUETES.md#wp17) · fase 3.**
Separar traducción literal de revisión semántica y adaptación para lectura/doblaje; usar llamadas adicionales sólo si el perfil las autoriza.
**Aceptación:** La revisión no altera números o nombres sin una advertencia y conserva la fuente original.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### SUB08 — ASS, karaoke y tipografía
**P1 · profundizar · [WP16](07_ROADMAP_Y_PAQUETES.md#wp16) · fase 2.**
Añadir estilo/posición y resaltado por palabra mediante ASS o renderer equivalente, con escaping y fuentes declaradas.
**Aceptación:** Una etiqueta maliciosa no se interpreta como ruta o comando; el estilo exportado es visible en preview.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### SUB09 — Subtítulos dobles y accesibilidad
**P1 · introducir · [WP16](07_ROADMAP_Y_PAQUETES.md#wp16) · fase 2.**
Pistas separadas para original, traducción y captions descriptivos; versiones quemadas o sidecar sin mezclar silenciosamente idiomas.
**Aceptación:** Cambiar la pista de exportación no elimina las otras pistas del proyecto.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### SUB10 — Retiming tras cortes o cambio de velocidad
**P1 · introducir · [WP13](07_ROADMAP_Y_PAQUETES.md#wp13) · fase 2.**
Derivar posiciones destino desde los rangos fuente y el mapa de edición; revisión especial para discontinuidades y solapes.
**Aceptación:** Un corte dentro de un cue se divide o marca para revisión, nunca queda apuntando al audio eliminado.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### SUB11 — QA de nombres, cifras y omisiones
**P1 · introducir · [WP17](07_ROADMAP_Y_PAQUETES.md#wp17) · fase 3.**
Detectar diferencias sospechosas entre fuente y destino, cues vacíos, repeticiones y cobertura; los hallazgos son flags revisables.
**Aceptación:** El sistema muestra los fragmentos implicados y no corrige una cifra con una suposición del modelo.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### SUB12 — Reanudar por etapa y cue
**P1 · profundizar · [WP17](07_ROADMAP_Y_PAQUETES.md#wp17) · fase 3.**
Cachear ASR, alineación, traducción y TTS por revisiones; un JSON inválido no se convierte en caché válida y repetidamente reutilizada.
**Aceptación:** Reintentar una traducción fallida no vuelve a transcribir un audio sin cambios.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

## Voz, sonido y edición de audio
Jarvis y narración local existen; el doblaje actual no debe presentarse como clonación, lip-sync o mezcla multipista.
Fuentes e inspiración: [F01](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/README.md), [F07](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/media_capabilities.py), [A04](https://github.com/m-bain/whisperX/blob/main/README.md), [A05](https://github.com/resemble-ai/chatterbox/blob/master/README.md), [W08](https://www.ffmpeg.org/ffmpeg-filters.html). La especificación que sigue es una propuesta de Faustus, no una atribución literal de cada función a cada fuente.

### AUD01 — Voces de proyecto y casting
**P1 · profundizar · [WP18](07_ROADMAP_Y_PAQUETES.md#wp18) · fase 3.**
Vincular personaje/speaker_id a una voz concreta por idioma, modelo y permiso; preview por frase, no prueba de micrófono automática.
**Aceptación:** Cambiar el casting sólo invalida las frases que usan esa voz.
**Verificación requerida:** `gpu_opt_in_and_integration`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### AUD02 — Controles TTS según variante
**P1 · profundizar · [WP18](07_ROADMAP_Y_PAQUETES.md#wp18) · fase 3.**
Velocidad, emoción, pausas y etiquetas paralingüísticas sólo donde se admitan; distinción entre inglés y multilingüe en el picker.
**Aceptación:** Una etiqueta no soportada se escapa o bloquea y no se lee accidentalmente como diálogo final.
**Verificación requerida:** `gpu_opt_in_and_integration`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### AUD03 — Pronunciación y diccionarios
**P1 · introducir · [WP18](07_ROADMAP_Y_PAQUETES.md#wp18) · fase 3.**
Léxico revisable de nombres, números, siglas y pronunciaciones, con salida a las formas aceptadas por cada motor.
**Aceptación:** La misma entrada puede tener representación distinta por TTS sin cambiar el texto editorial.
**Verificación requerida:** `gpu_opt_in_and_integration`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### AUD04 — Doblaje por personaje y región
**P1 · introducir · [WP18](07_ROADMAP_Y_PAQUETES.md#wp18) · fase 3.**
Producir voz por turnos y recomponerla con revisión de duración; no atribuir a VideoLingo el soporte multihablante que su README limita.
**Aceptación:** Una sola frase regenerada conserva las demás; desajustes de duración se señalan antes de encajar.
**Verificación requerida:** `gpu_opt_in_and_integration`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### AUD05 — Mezcla preservando ambiente
**P1 · profundizar · [WP19](07_ROADMAP_Y_PAQUETES.md#wp19) · fase 3.**
Elegir reemplazar toda la pista, sustituir sólo diálogo o añadir narración; ducking y stems si un separador autorizado está disponible.
**Aceptación:** El modo predeterminado no destruye música o ambiente sin elección explícita.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### AUD06 — Edición no destructiva y limpieza
**P1 · introducir · [WP19](07_ROADMAP_Y_PAQUETES.md#wp19) · fase 3.**
Trim, silencio, fades, ganancia y reducción de ruido como operaciones con before/after; backups de toma original.
**Aceptación:** Deshacer restaura audio original, no un audio procesado otra vez.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### AUD07 — Loudness y true peak medidos
**P1 · introducir · [WP19](07_ROADMAP_Y_PAQUETES.md#wp19) · fase 3.**
Medir y normalizar con perfil elegido y valores reales del archivo; mostrar objetivo frente a obtenido y clipping.
**Aceptación:** La tarjeta no informa un LUFS calculado a partir de ajustes: lee el resultado del análisis.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### AUD08 — Sonido y foley condicionados al montaje
**P1 · introducir · [WP19](07_ROADMAP_Y_PAQUETES.md#wp19) · fase 3.**
Biblioteca y adaptadores de efectos sonoros acotados a eventos del storyboard, con duración y licencia por asset.
**Aceptación:** Un efecto generado queda anclado al evento y puede sustituirse sin rerenderizar voces.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### AUD09 — Audiolibro y podcast por capítulos
**P1 · introducir · [WP22](07_ROADMAP_Y_PAQUETES.md#wp22) · fase 3.**
Del documento a capítulos, voces, pausas, correcciones y entregas; mantener anclas a párrafos y versiones del texto.
**Aceptación:** Editar un párrafo propone regenerar sólo su toma y recalcula tiempos dependientes.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### AUD10 — Consentimiento verificable y revocable
**P1 · profundizar · [WP18](07_ROADMAP_Y_PAQUETES.md#wp18) · fase 3.**
Ampliar el gate actual con alcance, vigencia, hashes de referencias y autor humano; distinguir voz sintética predefinida y clonación de identidad.
**Aceptación:** Un permiso vencido se comprueba antes de ejecutar, aunque el preflight anterior fuera válido.
**Verificación requerida:** `gpu_opt_in_and_integration`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

## Music Studio y canciones
No se verificó Music Studio integrado; ACE-Step es investigación adicional independiente de los posts de X.
Fuentes e inspiración: [A01](https://github.com/ace-step/ACE-Step-1.5/blob/main/README.md), [A02](https://github.com/ace-step/ACE-Step-1.5/blob/main/LICENSE), [W09](https://github.com/ace-step/ACE-Step-1.5), [F02](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/media_runs.py), [F04](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/media_workflows.py). La especificación que sigue es una propuesta de Faustus, no una atribución literal de cada función a cada fuente.

### MUS01 — Canción desde brief o letra
**P1 · introducir · [WP24](07_ROADMAP_Y_PAQUETES.md#wp24) · fase 3.**
Pasar de género, atmósfera, intención y letra a una propuesta musical con inputs visibles y variante del modelo seleccionada.
**Aceptación:** La letra aceptada se conserva como documento y la canción como asset enlazado, no sólo un mensaje.
**Verificación requerida:** `gpu_opt_in_and_integration`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### MUS02 — Estructura musical editable
**P1 · introducir · [WP24](07_ROADMAP_Y_PAQUETES.md#wp24) · fase 3.**
Estrofa, estribillo, puente, intro y outro como secciones de letra/objetivo; distinguir lo que el motor puede condicionar de lo que sólo se solicita en texto.
**Aceptación:** No se etiqueta como estructura garantizada una instrucción que el motor sólo interpreta probabilísticamente.
**Verificación requerida:** `gpu_opt_in_and_integration`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### MUS03 — BPM, tonalidad, compás e instrumental
**P1 · introducir · [WP24](07_ROADMAP_Y_PAQUETES.md#wp24) · fase 3.**
Controles tipados de duración y metadatos musicales disponibles en ACE-Step, con modo automático y desconocido explícitos.
**Aceptación:** Los parámetros reales enviados aparecen en el recibo de la toma.
**Verificación requerida:** `gpu_opt_in_and_integration`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### MUS04 — Referencias de audio con derechos
**P1 · introducir · [WP24](07_ROADMAP_Y_PAQUETES.md#wp24) · fase 3.**
Usar una referencia para estilo o estructura dentro del alcance técnico del modelo, guardando origen, permiso y rango usado.
**Aceptación:** Una referencia externa no se envía al proveedor sin la política de salida y consentimiento pertinentes.
**Verificación requerida:** `gpu_opt_in_and_integration`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### MUS05 — Variantes y audición comparada
**P1 · introducir · [WP24](07_ROADMAP_Y_PAQUETES.md#wp24) · fase 3.**
Generar alternativas con presupuesto previo, etiquetas humanas y comparación sincronizada donde sea pertinente.
**Aceptación:** Elegir una variante no elimina el historial ni dispara nuevas generaciones automáticamente.
**Verificación requerida:** `gpu_opt_in_and_integration`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### MUS06 — Repaint de región y regeneración del puente
**P2 · introducir · [WP25](07_ROADMAP_Y_PAQUETES.md#wp25) · fase 4.**
Seleccionar el intervalo que se desea cambiar y preservar el resto, con crossfade y revisión del empalme.
**Aceptación:** El rango fuera de la operación conserva el audio aceptado salvo handles de mezcla declarados.
**Verificación requerida:** `gpu_opt_in_and_integration`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### MUS07 — Continuación, loops y finales
**P2 · introducir · [WP25](07_ROADMAP_Y_PAQUETES.md#wp25) · fase 4.**
Plantillas para loop, sting, cierre y ampliación, según operaciones disponibles; evaluación de continuidad como revisión, no garantía.
**Aceptación:** La duración final se mide y los empalmes tienen preview audible antes de exportar.
**Verificación requerida:** `gpu_opt_in_and_integration`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### MUS08 — Stems y pistas musicales
**P2 · introducir · [WP25](07_ROADMAP_Y_PAQUETES.md#wp25) · fase 4.**
Separar o generar capas por operación admitida; registrar si un stem procede de separación o generación independiente.
**Aceptación:** La opción no aparece habilitada sólo porque el motor sea ACE-Step; comprueba la variante.
**Verificación requerida:** `gpu_opt_in_and_integration`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### MUS09 — Acompañamiento de voz
**P2 · introducir · [WP25](07_ROADMAP_Y_PAQUETES.md#wp25) · fase 4.**
Proponer música de fondo o acompañamiento para una voz dada, con controles de mezcla y escucha A/B.
**Aceptación:** La toma vocal original se conserva y sigue exportable por separado.
**Verificación requerida:** `gpu_opt_in_and_integration`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### MUS10 — LRC y lyric video
**P2 · introducir · [WP25](07_ROADMAP_Y_PAQUETES.md#wp25) · fase 4.**
Generar timestamps de letra, corregirlos en el editor y renderizar visualizadores o títulos sincronizados reutilizando el timeline.
**Aceptación:** Una palabra sin alineación suficiente queda marcada para revisión antes del karaoke final.
**Verificación requerida:** `gpu_opt_in_and_integration`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### MUS11 — Música ajustada a escenas
**P1 · introducir · [WP22](07_ROADMAP_Y_PAQUETES.md#wp22) · fase 3.**
Definir cue sheet con entradas, salidas, intensidades y hit points; producir segmentos que acompañen un vídeo o tráiler.
**Aceptación:** Al cambiar el montaje se recalculan cues afectados sin reescribir la canción entera por defecto.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### MUS12 — Entrenamiento LoRA musical opcional
**P3 · opcional · [WP39](07_ROADMAP_Y_PAQUETES.md#wp39) · fase 5.**
Dataset curado y autorizado, plan de recursos, entrenamiento aislado y pruebas heldout para aprobar un adaptador personal.
**Aceptación:** Un LoRA no se promueve por haber terminado el proceso: requiere muestras comparadas y licencia resuelta.
**Verificación requerida:** `gpu_opt_in_and_integration`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

## Model intelligence y configuración operativa
Faustus ya tiene metadatos ricos, lectores, calibración y launch receipts. Extender y unificar sus autoridades; no reemplazarlas por un catálogo de nombres.
Fuentes e inspiración: [F08](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/model_capabilities.py), [F09](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/model_calibration.py), [F10](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/docs/api/model_router.md), [F07](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/media_capabilities.py), [A01](https://github.com/ace-step/ACE-Step-1.5/blob/main/README.md), [A05](https://github.com/resemble-ai/chatterbox/blob/master/README.md), [A06](https://github.com/invoke-ai/InvokeAI/blob/main/README.md), [A09](https://github.com/Lightricks/LTX-2/blob/main/README.md), [A10](https://github.com/Wan-Video/Wan2.2/blob/main/README.md), [W01](https://models.dev/), [W04](https://invoke.ai/releases/). La especificación que sigue es una propuesta de Faustus, no una atribución literal de cada función a cada fuente.

### MOD01 — ModelSpec inmutable y aliases
**P0 · profundizar · [WP06](07_ROADMAP_Y_PAQUETES.md#wp06) · fase 1.**
Separar identidad de pesos, versiones y componentes de los aliases comerciales o tags locales; deduplicar pesos sin confundir deployments.
**Aceptación:** Dos tags del mismo digest pueden compartir ModelSpec, pero no se heredan pruebas de otro servidor.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### MOD02 — DeploymentFingerprint reproducible
**P0 · profundizar · [WP06](07_ROADMAP_Y_PAQUETES.md#wp06) · fase 1.**
Identidad de ejecución con endpoint autorizado, motor/build, modelo/revisión, cuantización, opciones efectivas y recursos; secretos fuera del fingerprint público.
**Aceptación:** Cambiar el motor o template invalida pruebas incompatibles sin perder la ficha de pesos.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### MOD03 — Capacidades por tarea, no por modalidad
**P0 · profundizar · [WP07](07_ROADMAP_Y_PAQUETES.md#wp07) · fase 1.**
Matrices separadas para vision, image-edit, inpaint, T2V, I2V, S2V, ASR, align, diarize, TTS, music-repaint y separación.
**Aceptación:** Un modelo que lee imágenes no recibe automáticamente image-generation ni edición.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### MOD04 — Evidencia por campo y fecha
**P0 · profundizar · [WP07](07_ROADMAP_Y_PAQUETES.md#wp07) · fase 1.**
Cada dato registra fuente, alcance, revisión, fecha, estado declarado/probado/no soportado/desconocido y conflicto.
**Aceptación:** Un valor del registro externo no sobrescribe silenciosamente una medición local incompatible.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### MOD05 — Límites de entrada y salida
**P0 · profundizar · [WP07](07_ROADMAP_Y_PAQUETES.md#wp07) · fase 1.**
Contexto útil, salida máxima, número de referencias, píxeles, duración, fps, samples, idiomas y formatos; límites combinados de tarea.
**Aceptación:** La UI bloquea una duración incompatible aunque un límite genérico de audio parezca suficiente.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### MOD06 — Esquemas de parámetros con dependencias
**P0 · profundizar · [WP07](07_ROADMAP_Y_PAQUETES.md#wp07) · fase 1.**
Describir rango, enum, valor por defecto, exclusión, obligatoriedad y condiciones: strength necesita imagen; alignment necesita idioma compatible.
**Aceptación:** El mismo schema valida formulario, API y tool; no hay tres listas de defaults divergentes.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### MOD07 — Recibo solicitado/aplicado/confirmado
**P0 · profundizar · [WP08](07_ROADMAP_Y_PAQUETES.md#wp08) · fase 1.**
Mostrar qué se pidió, qué normalizó Faustus, qué envió el adapter y qué confirmó el motor, sin tratar un eco de request como prueba de aplicación.
**Aceptación:** Un flag ignorado se ve como no confirmado; no se afirma que cambió el comportamiento.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### MOD08 — Detalle de arquitectura y componentes
**P0 · profundizar · [WP07](07_ROADMAP_Y_PAQUETES.md#wp07) · fase 1.**
Mostrar dense/MoE, activos/totales si hay datos, encoder/decoder/VAE, precisión y compatibilidad de LoRA; fuente explícita para cada campo.
**Aceptación:** El nombre del fichero no basta para dar por conocido el número de parámetros ni la arquitectura.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### MOD09 — Memoria y rendimiento por configuración
**P0 · profundizar · [WP30](07_ROADMAP_Y_PAQUETES.md#wp30) · fase 1.**
Estimaciones de pesos, KV/latents, activaciones y scratch separadas de medidas; mostrar condiciones y variabilidad del benchmark.
**Aceptación:** Una medición a 512 píxeles no se reutiliza como prueba de cabida a 1080p vídeo.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### MOD10 — Estados de instalación honestos
**P0 · profundizar · [WP08](07_ROADMAP_Y_PAQUETES.md#wp08) · fase 1.**
Separar descubierto, descargado, importable, conectado, cargable, probado y apto para esta tarea; check que no cargue pesos de oficio.
**Aceptación:** Encontrar un paquete o binario no habilita el botón de render como compatible probado.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### MOD11 — Model Explorer comparativo
**P0 · profundizar · [WP08](07_ROADMAP_Y_PAQUETES.md#wp08) · fase 1.**
Buscar y comparar capacidades, controles, licencias, idiomas, costo y cabida; ejemplos por tarea y explicación de exclusiones.
**Aceptación:** Comparar dos rutas marca unknown en vez de convertir campos vacíos en cero o no.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### MOD12 — Defaults por modelo, tarea y proyecto
**P0 · profundizar · [WP08](07_ROADMAP_Y_PAQUETES.md#wp08) · fase 1.**
Presets de sampling/reasoning/resolución/voz con precedencia visible y override puntual, sobre configuración efectiva existente.
**Aceptación:** Restaurar defaults muestra la procedencia y no cambia la configuración de otros proyectos.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### MOD13 — Model Zoo musical por variante
**P1 · introducir · [WP24](07_ROADMAP_Y_PAQUETES.md#wp24) · fase 3.**
Distinguir base/sft/turbo/XL y componentes LM/DiT; exponer sólo operaciones y controles realmente compatibles en la instalación.
**Aceptación:** extract, lego o complete no se habilitan por herencia automática de la familia turbo.
**Verificación requerida:** `gpu_opt_in_and_integration`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### MOD14 — Model Zoo de voz e idiomas
**P1 · introducir · [WP18](07_ROADMAP_Y_PAQUETES.md#wp18) · fase 3.**
Diferenciar TTS multilingüe, inglés, CPU ligero, expresividad y cloning, incluyendo requisitos de referencia y consentimiento.
**Aceptación:** Elegir español filtra o explica por qué una variante inglesa no sirve, en lugar de fingir soporte.
**Verificación requerida:** `gpu_opt_in_and_integration`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### MOD15 — Model Zoo visual y de vídeo
**P2 · profundizar · [WP23](07_ROADMAP_Y_PAQUETES.md#wp23) · fase 4.**
Registrar checkpoint, task, controles, formatos, first/last-frame, audio y componentes por pipeline; separar API-only de local.
**Aceptación:** El selector de una receta no promete una operación publicada sólo en el servicio remoto.
**Verificación requerida:** `gpu_opt_in_and_integration`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### MOD16 — Router por requisitos con intención persistente
**P0 · profundizar · [WP08](07_ROADMAP_Y_PAQUETES.md#wp08) · fase 1.**
Conservar routing_intent auto separado de last_resolved_model; evaluar requisitos del turno, autoridad, recursos y capacidad de tarea.
**Aceptación:** El primer turno auto no convierte silenciosamente todos los futuros en manuales.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### MOD17 — Simulador de cambio de modelo
**P0 · profundizar · [WP08](07_ROADMAP_Y_PAQUETES.md#wp08) · fase 1.**
Antes de cambiar, mostrar capacidades perdidas, inputs excluidos, coste nuevo y qué evidencias quedan obsoletas.
**Aceptación:** Un modelo sin visión no recibe un resumen como sustitución invisible de la imagen original.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### MOD18 — Calibración por niveles y benchmark propio
**P1 · profundizar · [WP36](07_ROADMAP_Y_PAQUETES.md#wp36) · fase 2.**
Separar aceptación de transporte, corrección de schema, calidad funcional y rendimiento; ejecutar por presupuesto y con fixtures adecuados.
**Aceptación:** Aceptar un PNG de un píxel no se etiqueta como prueba de comprensión visual.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### MOD19 — Actualización de catálogo con diff y rollback
**P0 · profundizar · [WP06](07_ROADMAP_Y_PAQUETES.md#wp06) · fase 1.**
Snapshot de fuentes externas, diferencia de campos, revisión de conflictos y retorno a versión anterior; nunca cambiar pesos en uso por actualización de ficha.
**Aceptación:** Una actualización de catálogo no altera deployments fijados ni invalida el trabajo sin aviso.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### MOD20 — Licencias y reproducibilidad en ficha
**P0 · profundizar · [WP07](07_ROADMAP_Y_PAQUETES.md#wp07) · fase 1.**
Mostrar código/pesos/LoRA/voz por separado, aceptación pendiente y límites de reproducibilidad entre engines o hardware.
**Aceptación:** Misma seed se describe como intento reproducible bajo condiciones, no garantía bit a bit universal.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

## Recursos, colas y fiabilidad
Existen admission, topology, memory budget, media outbox y scheduler; su integración exacta debe reauditarse. Preservar modo conservador.
Fuentes e inspiración: [F02](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/media_runs.py), [F03](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/media_scheduler.py), [F11](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/resource_admission.py), [F14](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/PENDIENTES.md), [F07](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/media_capabilities.py). La especificación que sigue es una propuesta de Faustus, no una atribución literal de cada función a cada fuente.

### RES01 — Grafo de recursos físicos
**P0 · profundizar · [WP30](07_ROADMAP_Y_PAQUETES.md#wp30) · fase 1.**
Relacionar endpoint, host, GPU UUID, RAM commit, CPU y scratch; detectar alias del mismo motor y dos servicios sobre una tarjeta.
**Aceptación:** Dos URLs distintas que comparten GPU no se presentan como capacidad independiente.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### RES02 — Presupuesto vectorial por trabajo
**P0 · profundizar · [WP30](07_ROADMAP_Y_PAQUETES.md#wp30) · fase 1.**
Reservar VRAM por dispositivo, memoria comprometida y disco temporal además de tokens/coste; márgenes conservadores explícitos.
**Aceptación:** La suma de tarjetas no satisface una petición que requiere un único dispositivo mayor.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### RES03 — Plan de coexistencia LLM/media
**P0 · profundizar · [WP30](07_ROADMAP_Y_PAQUETES.md#wp30) · fase 1.**
Priorizar la interacción y evitar cargar simultáneamente modelos grandes y renders sin una prueba de coexistencia autorizada.
**Aceptación:** Con recursos inciertos el segundo trabajo espera y explica por qué, en vez de provocar un OOM.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### RES04 — Afinidad y warm reuse medidos
**P2 · introducir · [WP31](07_ROADMAP_Y_PAQUETES.md#wp31) · fase 4.**
Preferir un worker que ya tenga el modelo y sus componentes cuando compense coste de carga, con caducidad de observación.
**Aceptación:** Un dato antiguo de modelo residente se revalida antes de asignar el trabajo.
**Verificación requerida:** `gpu_opt_in_and_integration`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### RES05 — Cola justa con prioridad visible
**P2 · profundizar · [WP31](07_ROADMAP_Y_PAQUETES.md#wp31) · fase 4.**
Prioridad de chat y previews, aging para renders largos y límites por owner; pausa en puntos seguros cuando el motor lo permita.
**Aceptación:** Un render no se interrumpe a mitad mediante una supuesta pausa no soportada.
**Verificación requerida:** `gpu_opt_in_and_integration`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### RES06 — Backpressure y límites de descarga
**P0 · profundizar · [WP04](07_ROADMAP_Y_PAQUETES.md#wp04) · fase 1.**
Acotar streams, buffers, chunks, previews y descargas concurrentes; reserva de disco antes de capturar grandes outputs.
**Aceptación:** Un output gigantesco o stream infinito no consume memoria del servidor sin límite.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### RES07 — Crash recovery y estado externo incierto
**P0 · profundizar · [WP10](07_ROADMAP_Y_PAQUETES.md#wp10) · fase 1.**
Generalizar reconciliación sin reintentar a ciegas; separar renderer completo, output recogido y asset validado.
**Aceptación:** Una respuesta perdida no produce automáticamente otra operación facturable.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### RES08 — Cancelación con fencing por intento
**P0 · profundizar · [WP10](07_ROADMAP_Y_PAQUETES.md#wp10) · fase 1.**
ID de intento y token de fencing ligados al proceso/job; recolectar sólo resultados autorizados del intento vigente.
**Aceptación:** Un worker lento no sobreescribe un cancelado ni una revisión más nueva.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### RES09 — Rendimiento por fases comparable
**P1 · profundizar · [WP36](07_ROADMAP_Y_PAQUETES.md#wp36) · fase 2.**
Medir cola, carga, preparación, inferencia, encode, transferencia y validación con procedencia de cada medida.
**Aceptación:** Una fase no reportada es unknown y no cero; latencias del proxy no se atribuyen al modelo.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### RES10 — Pools remotos y varias GPUs opt-in
**P2 · profundizar · [WP31](07_ROADMAP_Y_PAQUETES.md#wp31) · fase 4.**
Distribuir tareas independientes a workers locales/remotos, no prometer dividir automáticamente un modelo entre GPUs o transportes heterogéneos.
**Aceptación:** La activación exige prueba de recursos disjuntos y conserva una vuelta inmediata al modo serial.
**Verificación requerida:** `gpu_opt_in_and_integration`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

## Orquestación, continuidad y equipos
Faustus ya dispone de workflows, completion, budgets, teams, Council y compacción; mejorar sus invariantes y UX sin añadir otro orquestador central.
Fuentes e inspiración: [F01](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/README.md), [F10](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/docs/api/model_router.md), [F12](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/services/projects.py), [D01](https://github.com/totec448-spec/chat-on-steroids/blob/99ec069cfbe7aae01e2ba9ab234c237ffbf609ff/README.md), [D02](https://github.com/totec448-spec/chat-on-steroids/blob/99ec069cfbe7aae01e2ba9ab234c237ffbf609ff/src/main/goal.ts), [D03](https://github.com/totec448-spec/chat-on-steroids/blob/99ec069cfbe7aae01e2ba9ab234c237ffbf609ff/src/main/session/handoff.ts), [W06](https://docs.langchain.com/oss/python/langgraph/interrupts), [W07](https://docs.langchain.com/oss/python/langgraph/use-subgraphs). La especificación que sigue es una propuesta de Faustus, no una atribución literal de cada función a cada fuente.

### ORC01 — Goal versionado y acotado
**P1 · profundizar · [WP27](07_ROADMAP_Y_PAQUETES.md#wp27) · fase 2.**
Definir entregables, scope, criterios, presupuesto y política de continuidad antes de ejecutar trabajos extensos.
**Aceptación:** El agente no añade mejoras ajenas al objetivo como condición para acabar.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### ORC02 — Complete exige evidencia
**P1 · profundizar · [WP27](07_ROADMAP_Y_PAQUETES.md#wp27) · fase 2.**
Comprobar archivos, revisiones y tests asociados al criterio; una autoafirmación o marcador del modelo sólo propone cierre.
**Aceptación:** Una respuesta convincente sin el MP4 pedido no marca la producción como completada.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### ORC03 — Loop con parada por falta de progreso
**P1 · profundizar · [WP27](07_ROADMAP_Y_PAQUETES.md#wp27) · fase 2.**
Detectar repeticiones de acción/estado y usar límites de intentos, tiempo y coste con causa de parada legible.
**Aceptación:** Dos vueltas sin nueva evidencia pueden pausar según el perfil y no disparan un bucle infinito.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### ORC04 — Handoff tipado y referencias verificables
**P1 · profundizar · [WP28](07_ROADMAP_Y_PAQUETES.md#wp28) · fase 3.**
Capsula de objetivo, decisiones, estado, pendientes, refs de artefactos y mapa de especialistas; conservar transcript original.
**Aceptación:** El receptor puede resolver cada referencia y ve cuáles han cambiado desde el snapshot.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### ORC05 — Publicación transaccional de continuación
**P1 · profundizar · [WP28](07_ROADMAP_Y_PAQUETES.md#wp28) · fase 3.**
Preparar la cápsula antes de publicarla, con event sequence, idempotency key y recuperación de ventanas de crash.
**Aceptación:** Reiniciar entre prepare y publish no crea dos continuaciones ni anuncia una cápsula inexistente.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### ORC06 — Cambio de modelo con revalidación
**P1 · integrar · [WP28](07_ROADMAP_Y_PAQUETES.md#wp28) · fase 3.**
Recalcular contexto, tools y pérdida de modalidades al cambiar de modelo durante una tarea; permisos nunca viajan por texto.
**Aceptación:** Reanudar en otro modelo no reutiliza una aprobación consumida para un contenido distinto.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### ORC07 — Especialistas por dominio creativo
**P2 · profundizar · [WP29](07_ROADMAP_Y_PAQUETES.md#wp29) · fase 4.**
Roles guionista, director visual, editor, traductor, compositor y QA con modelo/herramientas limitados; activarlos sólo cuando aporten.
**Aceptación:** Un pedido sencillo no invoca todo el equipo ni consume presupuesto de Council sin necesidad.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### ORC08 — Memoria por invocación o por proyecto
**P2 · profundizar · [WP29](07_ROADMAP_Y_PAQUETES.md#wp29) · fase 4.**
Declarar vida y alcance de estado de cada especialista; hechos canónicos y decisiones aceptadas separados de propuestas y borradores.
**Aceptación:** Un especialista no mezcla memoria de dos proyectos por reutilizar la misma sesión de motor.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### ORC09 — Propiedad de planos y documentos
**P2 · profundizar · [WP29](07_ROADMAP_Y_PAQUETES.md#wp29) · fase 4.**
Claims de edición, expected_revision y estrategias de integración para trabajo simultáneo; worktrees sólo para código.
**Aceptación:** Dos agentes modificando la misma escena reciben conflicto o ramas separadas, no último escritor gana.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### ORC10 — Steering durable y control humano
**P2 · profundizar · [WP29](07_ROADMAP_Y_PAQUETES.md#wp29) · fase 4.**
Ordenar correcciones como eventos con revisión de objetivo; nuevas instrucciones retiran acciones aún no autorizadas obsoletas.
**Aceptación:** Una corrección durante render se aplica a siguientes pasos y no finge deshacer el efecto ya enviado.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### ORC11 — DAG de producción inspeccionable
**P1 · integrar · [WP22](07_ROADMAP_Y_PAQUETES.md#wp22) · fase 3.**
Componer ASR, traducción, generación, revisión, mezcla y export con los workflows existentes y diferenciar simulación de ejecución.
**Aceptación:** La simulación no afirma calidad ni coste medido y no llama a motores generativos.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### ORC12 — Subplanes incrementales y caché semántica
**P1 · profundizar · [WP26](07_ROADMAP_Y_PAQUETES.md#wp26) · fase 3.**
Calcular el subgrafo afectado por una edición y enseñar el impacto antes de rerun; reglas de caché por tipo de tarea.
**Aceptación:** Cambiar captions no vuelve a generar una canción ni consumir crédito de vídeo sin dependencia real.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### ORC13 — Council y revisión con criterio
**P1 · profundizar · [WP27](07_ROADMAP_Y_PAQUETES.md#wp27) · fase 2.**
Usar revisión multmodelo sólo para decisiones caras o de alto riesgo; separar síntesis de permiso de ejecución y de verdad verificada.
**Aceptación:** Una votación no convierte en cumplido un criterio técnico fallido.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### ORC14 — Recetas desde éxitos y fallos reales
**P1 · profundizar · [WP41](07_ROADMAP_Y_PAQUETES.md#wp41) · fase 3.**
Promover procedimientos exitosos y registrar anti-patrones en sistemas de recetas/immune existentes con evidencia y ámbito.
**Aceptación:** Una regla aprendida en un motor no se impone globalmente sin revisión y pruebas.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

## MCP, extensiones y distribución
MCP, skills, presets y apps externas existen. Adoptar lifecycle y UX útiles sin reemplazar transportes ni copiar código sin licencia.
Fuentes e inspiración: [D04](https://github.com/totec448-spec/chat-on-steroids/blob/main/docs/plugins.md), [D06](https://github.com/buluma/steroid-chat/blob/master/README.md), [D07](https://github.com/buluma/steroid-chat/blob/master/steroid-chat-web/src/services/aiService.ts), [D08](https://github.com/calin-ciobanu/killer_chatgpt_prompts/blob/master/README.md), [D09](https://github.com/calin-ciobanu/killer_chatgpt_prompts/blob/master/user_custom.json), [F01](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/README.md), [F08](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/model_capabilities.py). La especificación que sigue es una propuesta de Faustus, no una atribución literal de cada función a cada fuente.

### EXT01 — Catálogo curado de adaptadores
**P1 · profundizar · [WP32](07_ROADMAP_Y_PAQUETES.md#wp32) · fase 2.**
Recetas instalables fijadas por versión/hash con runtime, permisos, modelos, licencia y prueba de salud descritos.
**Aceptación:** Pegar una URL GitHub desconocida no ejecuta automáticamente su script de instalación.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### EXT02 — Instalación aislada y actualización rollback
**P1 · profundizar · [WP32](07_ROADMAP_Y_PAQUETES.md#wp32) · fase 2.**
Entornos separados, staging de versión nueva, healthcheck y swap; conservar datos y rollback ante fallo.
**Aceptación:** Actualizar TTS no cambia torch/CUDA del servidor principal de Faustus.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### EXT03 — Schemas MCP íntegros y exposición clara
**P1 · profundizar · [WP32](07_ROADMAP_Y_PAQUETES.md#wp32) · fase 2.**
Preservar schemas completos y distinguir instalado, habilitado, descubierto y publicado; límites de tamaño y conflictos visibles.
**Aceptación:** No se renombra silenciosamente una herramienta conflictiva ni se pierde su schema de salida.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### EXT04 — Revocación y permisos en cada llamada
**P1 · profundizar · [WP32](07_ROADMAP_Y_PAQUETES.md#wp32) · fase 2.**
Revalidar enabled/scopes y owner aunque un cliente conserve schemas cacheados; no confiar en readonlyHint para asegurar aislamiento.
**Aceptación:** Una llamada stale a un plugin desactivado se rechaza antes de enviar datos.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### EXT05 — Creator tools para Claude/Codex y otros clientes
**P2 · introducir · [WP33](07_ROADMAP_Y_PAQUETES.md#wp33) · fase 4.**
Exponer descubrimiento, planes, operaciones y consultas de runs sobre los mismos servicios; sujeto a capacidades reales del cliente conectado.
**Aceptación:** El cliente externo produce el mismo recibo y autorización que Studio, no una ruta privilegiada.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### EXT06 — Captura web como material explícito
**P2 · profundizar · [WP33](07_ROADMAP_Y_PAQUETES.md#wp33) · fase 4.**
Capturar sólo la página, región o selección elegida con URL/fecha/snapshot; draft antes de envío y renovación de refs tras navegación.
**Aceptación:** No se exportan cookies, claves ni el perfil entero del navegador para generar contexto.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### EXT07 — Presets con contratos y evaluaciones
**P1 · profundizar · [WP41](07_ROADMAP_Y_PAQUETES.md#wp41) · fase 3.**
Roles propios derivados de necesidades de escritura/diseño/edición, con inputs/salidas y métricas; evitar instrucciones de ignorar todas las reglas.
**Aceptación:** Activar un preset cambia estilo/procedimiento y nunca el sistema de permisos.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### EXT08 — Robustez incremental de streaming
**P1 · profundizar · [WP36](07_ROADMAP_Y_PAQUETES.md#wp36) · fase 2.**
Añadir casos de SSE/NDJSON fragmentados, Unicode, escapes, JSON concatenado, terminal y cancelación inspirados en SteroidChat, sin copiar su transporte entero.
**Aceptación:** Partir el stream en cualquier frontera produce los mismos eventos y un único cierre.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### EXT09 — Conectores de entrega aprobada
**P3 · introducir · [WP40](07_ROADMAP_Y_PAQUETES.md#wp40) · fase 5.**
Plantillas y adaptadores por destino con revisión del contenido exacto, schedule opcional y recibos de entrega diferenciados.
**Aceptación:** Aprobar un MP4 no autoriza automáticamente publicarlo en todas las cuentas conectadas.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### EXT10 — Compatibilidad y documentación sin drift
**P0 · auditar · [WP00](07_ROADMAP_Y_PAQUETES.md#wp00) · fase 0.**
Generar tablas de soporte desde schemas/fixtures versionados y marcar fechas; detectar divergencia README frente al código.
**Aceptación:** Un cambio en el catálogo exige actualizar sus fixtures/documentación o declara cobertura pendiente.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

## Calidad, migración y seguridad
Existe infraestructura amplia de pruebas y permisos. Ampliar cobertura con observaciones reproducibles; no declarar todo probado por leer el README.
Fuentes e inspiración: [F01](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/README.md), [F02](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/media_runs.py), [F04](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/media_workflows.py), [F05](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/media_edit_projects.py), [F07](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/media_capabilities.py), [F15](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/tests/README.md), [D04](https://github.com/totec448-spec/chat-on-steroids/blob/main/docs/plugins.md), [A03](https://github.com/Huanshere/VideoLingo/blob/main/README.md). La especificación que sigue es una propuesta de Faustus, no una atribución literal de cada función a cada fuente.

### QA01 — Pruebas de observaciones estáticas
**P0 · auditar · [WP01](07_ROADMAP_Y_PAQUETES.md#wp01) · fase 0.**
Verificar fingerprint completo, returncode de probes y semántica de compositor antes de construir más automatismos sobre ellos.
**Aceptación:** Cada corrección tiene prueba roja previa y no altera un contrato histórico sin migración/versionado.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### QA02 — Suite de conformidad por adapter
**P1 · introducir · [WP36](07_ROADMAP_Y_PAQUETES.md#wp36) · fase 2.**
Harness común para validar schemas, planning sin efectos, submit incierto, collect acotado y cancelación con fencing.
**Aceptación:** Un adapter nuevo no se habilita sólo por responder health 200.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### QA03 — Matriz de pruebas owner/privacy
**P1 · profundizar · [WP36](07_ROADMAP_Y_PAQUETES.md#wp36) · fase 2.**
Casos cruzados de owners, projects, occurrences, plugins y nodos Comfy remotos; local-only también bloquea salidas secundarias.
**Aceptación:** Un custom node con salida de red no evita la política al ejecutarse detrás de un motor local.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### QA04 — Golden paths de producción real
**P1 · introducir · [WP37](07_ROADMAP_Y_PAQUETES.md#wp37) · fase 3.**
Fixtures end-to-end con entradas y outputs decodificables para imagen, vídeo y música; hardware real en lane opt-in distinta.
**Aceptación:** El informe distingue contrato simulado de render real y conserva comprobantes de cada nivel.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### QA05 — Migraciones aditivas y rollback de producto
**P0 · profundizar · [WP02](07_ROADMAP_Y_PAQUETES.md#wp02) · fase 1.**
Versionar documentos y extender metadatos sin reescribir toda la biblioteca; feature flags que permitan volver a UI anterior.
**Aceptación:** Un downgrade de interfaz no elimina archivos ni hace ilegibles proyectos legacy.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### QA06 — Licencias por cadena de producción
**P0 · profundizar · [WP07](07_ROADMAP_Y_PAQUETES.md#wp07) · fase 1.**
Registrar código, pesos, LoRA, fuentes, voz, datasets y muestras; unknown no se presenta como uso comercial permitido.
**Aceptación:** El export comercial muestra asuntos pendientes y no infiere autorización por el mero carácter público del repo.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### QA07 — Import seguro y sandbox de codecs
**P0 · profundizar · [WP04](07_ROADMAP_Y_PAQUETES.md#wp04) · fase 1.**
Límites contra decompression bombs, path traversal, formatos activos y protocolo de red en FFmpeg; procesos acotados y secretos mínimos.
**Aceptación:** Un bundle o SVG importado no ejecuta scripts ni lee rutas fuera de los roots autorizados.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### QA08 — Métricas de resultado, no vanity tests
**P1 · profundizar · [WP37](07_ROADMAP_Y_PAQUETES.md#wp37) · fase 3.**
Medir ratio de entregables válidos, reintentos, prompts manuales, coste y latencia por flujo; comparaciones con baseline y variabilidad.
**Aceptación:** Una mejora de velocidad que rompe fidelidad o sube fallos no se promociona como mejor perfil.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### QA09 — Sincronización de permisos con contenido
**P0 · profundizar · [WP09](07_ROADMAP_Y_PAQUETES.md#wp09) · fase 1.**
Vincular approval a owner, inputs, revisiones, destino, modelo, coste permitido y acción; revalidar al ejecutar.
**Aceptación:** Cambiar el guion o el destinatario tras aprobar invalida el permiso exacto afectado.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### QA10 — Ledger de enlaces no recuperados
**P0 · auditar · [WP42](07_ROADMAP_Y_PAQUETES.md#wp42) · fase 0.**
Mantener los 15 posts de X como pendientes con URL exacta y sin proyectos o features atribuidas por intuición.
**Aceptación:** El paquete permite completar el delta de fuentes sin fingir que ya se analizaron.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

## Expansiones ambiciosas posteriores
Opciones de producto propuestas, no funcionalidades verificadas en Faustus ni promesas de los posts de X.
Fuentes e inspiración: [A06](https://github.com/invoke-ai/InvokeAI/blob/main/README.md), [A09](https://github.com/Lightricks/LTX-2/blob/main/README.md), [D04](https://github.com/totec448-spec/chat-on-steroids/blob/main/docs/plugins.md), [A01](https://github.com/ace-step/ACE-Step-1.5/blob/main/README.md). La especificación que sigue es una propuesta de Faustus, no una atribución literal de cada función a cada fuente.

### ADV01 — Escenas Blender y turntables
**P3 · opcional · [WP38](07_ROADMAP_Y_PAQUETES.md#wp38) · fase 5.**
MCP o adapter tipado para escena, cámara, luz, materiales y render de producto con lectura inicial verificable.
**Aceptación:** La prueba de escena no modifica Blender y las mutaciones requieren autorización de operación.
**Verificación requerida:** `gpu_opt_in_and_integration`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### ADV02 — Puente 3D a 2D y vídeo
**P3 · opcional · [WP38](07_ROADMAP_Y_PAQUETES.md#wp38) · fase 5.**
Exportar depth, normals, pose o vistas de un activo 3D para conditioning de imágenes y planos con referencias geométricas.
**Aceptación:** Cada mapa referencia el render y cámara exactos; no se afirma geometría coherente sólo por usar una imagen.
**Verificación requerida:** `gpu_opt_in_and_integration`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### ADV03 — LoRA visual y biblioteca de datasets
**P3 · opcional · [WP39](07_ROADMAP_Y_PAQUETES.md#wp39) · fase 5.**
Curar datasets, captions y licencias, entrenar en entornos separados y validar en heldout antes de reutilizar un adaptador.
**Aceptación:** El entrenamiento nunca se inicia por la mera selección de referencias en un chat.
**Verificación requerida:** `gpu_opt_in_and_integration`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### ADV04 — Motion capture y animación dirigidas
**P3 · opcional · [WP38](07_ROADMAP_Y_PAQUETES.md#wp38) · fase 5.**
Integraciones futuras de animación condicionada, rigging o captura con interfaz de capacidades, revisión y límites explícitos.
**Aceptación:** Se mantiene como capability desconocida hasta probar un backend concreto, no un botón que sólo cambia el prompt.
**Verificación requerida:** `gpu_opt_in_and_integration`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### ADV05 — Campaign packs multiformato
**P3 · opcional · [WP40](07_ROADMAP_Y_PAQUETES.md#wp40) · fase 5.**
De un brief y kit de marca a imágenes, clips, música, captions y textos de publicación relacionados, con revisión por pieza.
**Aceptación:** Aprobar una pieza no aprueba el resto de los formatos ni su publicación.
**Verificación requerida:** `contract_and_e2e`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.

### ADV06 — Batch creative lab reproducible
**P3 · opcional · [WP39](07_ROADMAP_Y_PAQUETES.md#wp39) · fase 5.**
Laboratorio de comparación de recetas, modelos y LoRA con dataset autorizado, budgets fijos y revisiones humanas ciegas opcionales.
**Aceptación:** Los resultados mantienen tamaño de muestra y configuración; no se generaliza un ranking de una sola muestra.
**Verificación requerida:** `gpu_opt_in_and_integration`. Validar primero el contrato; los motores reales llevan ejecución opt-in identificada.
