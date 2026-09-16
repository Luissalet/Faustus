# Recetas de producto y demostraciones objetivo

Las recetas siguientes son propuestas de experiencias completas. No son promesas de calidad de un modelo concreto. Cada una puede funcionar con assets importados y añadir generación sólo cuando las capacidades y recursos estén disponibles.

## R01 — Book trailer desde un proyecto de escritura

Entrada: sinopsis/fragmento propio, biblia de personajes, referencias visuales, voz autorizada y duración objetivo. Crear un brief y storyboard; producir un animatic antes de generar planos. Elegir tomas, narrar párrafos, diseñar música por cues, componer títulos y exportar versiones horizontal/vertical con subtítulos. Una corrección de nombre afecta voz/textos correspondientes, no toda la producción.

Dependencias principales: WP21, WP22, WP18, WP24, WP14. Generación de vídeo requiere WP23; no es necesaria para un animatic con imágenes.

## R02 — Localización de un vídeo educativo

Entrada: vídeo autorizado, glosario y uno o varios idiomas de destino. Transcribir y alinear, corregir hablantes, traducir/revisar/adaptar, asignar voces y mezclar preservando ambiente. Exportar original subtitulado, traducción sidecar y doblaje. Comparar cifras/nombres y mostrar cambios editoriales. Una frase rechazada regenera sólo su cue/toma.

Dependencias: WP15–WP19, WP13–WP14. La parte de voz es opcional: subtítulos sigue siendo un entregable completo.

## R03 — Canción original con letra editable

Entrada: letra propia o brief temático, estructura deseada, idioma y referencias autorizadas. Proponer metadatos, generar varias tomas dentro del budget, escuchar y aceptar secciones. Rehacer el puente o la intro con una operación compatible; preparar instrumental/stems cuando el motor lo admita, LRC revisado y lyric video. No entrenar un LoRA como parte implícita de esta receta.

Dependencias: WP24, después WP25/WP19 para edición avanzada y WP14 para vídeo.

## R04 — Campaña visual coherente de producto

Entrada: fotos originales, logotipo, paleta y brief. Segmentar el producto, explorar fondos en capas candidatas y componer texto/logotipo desde fuentes exactas. Producir formatos y encuadres separados con safe areas, comparar variantes y preparar piezas aprobables. La publicación es otra acción, no el paso automático posterior a generar.

Dependencias: WP20–WP22, WP14; publicación opcional WP40.

## R05 — Tutorial o lanzamiento de software

Entrada: proyecto de código/documentación, capturas elegidas y guion. Convertir instrucciones de usuario en escenas, narrar, añadir callouts y subtítulos, preparar clip corto y tutorial completo. Anclar capturas a versiones y fechas; no afirmar que una captura prueba el comportamiento actual de un programa tras cambiar su código.

Dependencias: WP22, WP18, WP16, WP14; captura asistida opcional WP33.

## R06 — Podcast o audiolibro por capítulos

Entrada: manuscrito y voces seleccionadas. Dividir por unidades editoriales, revisar pronunciación, sintetizar tomas, montar pausas/ambiente y normalizar al perfil elegido. Corregir un párrafo debe conservar capítulos intactos. Entregar texto fuente revisado, audio por capítulo y edición completa; el orden y los nombres se derivan del manifest.

Dependencias: WP18–WP19, WP26, WP34 para paquete editable.

## R07 — De metraje largo a clips revisables

Entrada: grabación autorizada. Indexar proxies y transcripción, sugerir highlights según criterios del brief, revisar encuadres verticales y captions, añadir apertura/cierre y exportar por lotes. No prometer viralidad ni que todos los cortes sugeridos son buenos; cada clip enlaza al rango fuente y se puede corregir.

Dependencias: WP04, WP13–WP16, WP22, WP26. Ampliar límites de duración sólo en rutas probadas.

## R08 — Diseño de personaje con revisión de canon

Entrada: descripción, boceto y referencias aceptadas. Producir variaciones de ropa, vistas o iluminación, comparar y seleccionar una ficha canónica. Generar nuevos planos reutilizando esa versión y señalar desviaciones como propuestas de revisión. Mantener las decisiones escritas y assets enlazados a Story Bible.

Dependencias: WP20–WP21 y WP03. No se promete consistencia geométrica o de identidad perfecta de un generador 2D.

## R09 — Presentación audiovisual narrada

Entrada: documento, fuentes y assets. Producir storyboard de diapositivas/escenas, componer gráficos/textos editables, narrar y exportar vídeo con capítulos. Las cifras proceden de los datos/figuras originales; no se regeneran mediante una imagen que pueda inventar etiquetas. Aprovechar exportadores de documentos existentes cuando corresponda.

Dependencias: WP22, WP14, WP18, WP03.

## R10 — Render de producto 3D y campaña mixta

Entrada: escena propia de Blender. Leer la escena, proponer cámaras/luces, generar turntable y mapas de profundidad/normales, producir derivados 2D y montar. Todas las mutaciones de escena se controlan y se puede conservar el .blend original. La disponibilidad requiere un backend probado, no sólo instalar un paquete MCP.

Dependencias: WP38, más WP21/WP22 para derivados. Expansión opcional posterior, no dependencia del Creator inicial.

## R11 — Producción por lotes a partir de una tabla

Entrada: tabla de briefs autorizados y plantilla de assets. Validar filas y presupuestos, generar sólo las operaciones permitidas, dejar en revisión cada variante y exportar un inventario de éxitos/fallos. Reanudar no repite filas ya confirmadas y ninguna pieza se publica sin su política de entrega.

Dependencias: WP22, WP26, WP09, WP34. La tabla es datos, no expresiones ejecutables.

## R12 — Reparación supervisada de una producción

Entrada: proyecto con un render fallido, traducción pendiente o asset faltante. Diagnosticar el estado externo, listar evidencias y proponer el subplan mínimo de reparación. Preservar partes aceptadas, reconciliar efectos inciertos, reanudar y verificar los criterios del Goal. Esta receta demuestra el harness, no sólo las capacidades del generador.

Dependencias: WP10, WP26–WP29, WP36. Un resultado incierto no se convierte artificialmente en fallo para facilitar un retry.

