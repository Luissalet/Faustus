# Especificación de producto Creator

## El resultado que se busca

La experiencia objetivo es poder decir: «Aquí están el guion, las fotos y la voz. Haz un tráiler de 45 segundos, prepara una versión vertical y otra horizontal, subtítulos en español e inglés y una música que suba en el último plano». Faustus debe devolver primero un plan inspeccionable y luego una producción editable, no una carpeta de resultados inconexos. Es una visión de producto propuesta: no se afirma que cualquiera de los motores analizados satisfaga por sí solo todos esos pasos.

La interacción alterna intención en lenguaje natural y edición precisa. «Cambia sólo esta frase», «prueba tres fondos sin tocar el personaje», «esta parte de la música está bien, rehace el puente» y «recorta del segundo 12 al 15 y reajusta los subtítulos» deben traducirse a cambios sobre objetos y revisiones identificados. El usuario puede operar sin agentes, con asistencia acotada o mediante una producción orquestada.

## Modelo de navegación

Creator es una vista especializada de un proyecto existente, con conversaciones normales asociadas. `workspace_kind=creator` es un nombre de campo propuesto; no debe añadirse hasta comprobar cómo el HEAD representa perfiles y vistas. `execution_mode=chat|agent` conserva su semántica. Abrir Creator no activa shell, permisos de red, downloads o autonomía adicional.[^F01][^F12]

La estructura visual propuesta tiene un área central de preview/editor, un media bin, un inspector contextual y un chat lateral. Un panel temporal inferior aparece para vídeo, subtítulos o música. Layouts guardados cambian la distribución, no las entidades. La pantalla se construye por secciones con carga diferida; no se descarga un vídeo completo para pintar una miniatura.

Las vistas principales son **Producción**, **Canvas**, **Montaje**, **Subtítulos**, **Audio/Música** y **Biblioteca**. Producción muestra brief, storyboard, dependencias y decisiones pendientes. La biblioteca debe ser la misma colección de occurrences que usan Activity y context links. Un resultado no deja de existir porque se cierre su conversación de origen.

## Objetos con los que trabaja el usuario

Un **brief** contiene objetivo, entregables, público, idiomas, estilo, duración, formato y restricciones. Una **escena** contiene intención narrativa y referencias; un **plano** añade duración, cámara y candidatas. Un **asset** es una occurrence autorizada, con revisiones y derivados. Una **toma** es un resultado concreto de una ejecución. Una **versión de montaje** compone tomas, voz, sonido y textos. Una **entrega** fija una versión de montaje y su perfil de exportación.

En música, letra y estructura son documentos editables. Las secciones no se representan como un único prompt gigantesco que se pierde después de generar. Una misma canción puede tener varias tomas, partes regeneradas y stems. Los intervalos regenerados guardan los handles usados para los empalmes y no destruyen el audio original.

En subtítulos, texto fuente, traducción, adaptación para doblaje y texto finalmente narrado son capas distintas. La interfaz debe mostrar qué se ha cambiado para encajar en tiempo. Una traducción más corta no se presenta como transcripción literal.

## Flujos de uso

### Imagen y diseño

Importar o generar una imagen, añadir una referencia, seleccionar una región y pedir alternativas. La propuesta llega a una capa candidata con preview antes de commit. Texto y logotipos importantes deben poder componerse determinísticamente desde assets aprobados. Los modelos generan elementos visuales; la aplicación no delega toda la precisión editorial al modelo.

Guardar una referencia como personaje o estilo ya tiene precedentes en Faustus. La mejora consiste en asociar ese rol con un input real de un backend compatible, conservar el recibo y mostrar la diferencia entre orientación textual y conditioning. Mantener esa distinción evita que una UI convincente prometa un mecanismo que no se ejecutó.[^F01][^F04]

### Vídeo y montaje

Importar vídeo con inspección acotada, construir proxies, editar la transcripción y proponer cortes. El preview usa referencias a rangos fuente, nunca vuelve a escribir el original. El render final compila la revisión aceptada, valida el archivo y lo añade a la biblioteca.

Para generación desde guion, comenzar con storyboard y animatic. Sólo después de aceptar referencias, encuadres y presupuesto se generan planos. Un plano fallido debe poder reintentarse de forma independiente cuando el efecto externo esté reconciliado. El montaje debe seguir siendo editable con recursos importados incluso sin un modelo de vídeo instalado.

### Subtítulos y doblaje

Transcribir, alinear, corregir hablantes e idioma, aplicar glosario y revisar traducciones. Antes de narrar, asignar voces por hablante/personaje y decidir si se reemplaza diálogo, se conserva ambiente o se añade voice-over. La mezcla debe hacer visible esta decisión, porque la narración local descrita en la baseline reemplaza el audio original y no equivale a un doblaje avanzado.[^F01][^F06]

### Música y canciones

Crear letra o usar una propia; elegir estructura, idioma y metadatos musicales. El modo sencillo deja que el motor proponga valores, pero los registra; el experto permite ajustar sólo controles soportados. Elegir tomas mediante escucha, marcar partes aceptadas, regenerar una región y montar. Stems, acompañamiento o edición avanzada aparecen sólo cuando la variante configurada los admite.[^A01][^W09]

### Proyectos mixtos

Un proyecto de novela puede producir una portada, voces de personajes y un book trailer. Un proyecto de software puede producir capturas, narración y vídeos de lanzamiento. No es necesario mover esos trabajos a otro registro de proyectos ni perder sus fuentes. Creator añade objetos de producción al conocimiento existente.

## Reglas de interacción

Una selección se envía como `{document_id, revision, object_id, range_or_region}`. El agente puede consultar el contexto relacionado y proponer una operación, pero no inferir que unos píxeles corresponden a una línea de código o que una palabra seleccionada existe igual en otra revisión.

El flujo por defecto para cambios materiales es **proponer → previsualizar → aceptar → ejecutar cuando proceda**. No todas las ediciones requieren una aprobación modal: los cambios manuales dentro de una autorización vigente pueden ser directos. Las acciones facturables, públicas o sensibles conservan sus gates específicos. Evitar tanto la automatización invisible como una tormenta de confirmaciones para cada pequeño gesto.

El progreso no es una barra ficticia. Mostrar etapa real, espera de recursos, bytes transferidos o iteración sólo si el motor lo reporta. En otros casos, mostrar actividad y última señal recibida. No convertir estimaciones de tiempo en una promesa.

## Criterios de experiencia

El primer éxito de Creator debe alcanzarse con piezas existentes: un vídeo corto local, subtítulos corregidos y export comprobado; o una imagen con máscara y variantes que se pueden comparar. No exigir instalar todos los motores para abrir el estudio. El avance hacia música, vídeo generativo y 3D ocurre mediante capacidades opcionales.

Un flujo sólo está cerrado cuando produce un entregable accesible, editable según lo prometido y vinculado a su evidencia. «El agente escribió una explicación del resultado» no es equivalente. La UI debe permitir responder en todo momento: qué cambió, sobre qué versión, con qué motor, qué falta y cuánto se conoce realmente del resultado.


[^F01]: Faustus · README.md. [Fuente](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/README.md). Consultado el 13 de septiembre de 2026. Capacidades generales, arquitectura y licencia declarada AGPL-3.0-or-later; resultados de pruebas reportados por el repositorio, no reproducidos aquí.

[^F12]: Faustus · services/projects.py. [Fuente](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/services/projects.py). Consultado el 13 de septiembre de 2026. Lectura 1–170: ProjectStore, context_items, ProjectExecutionContext e identidad estable; evitar segundo registro de proyectos.

[^F04]: Faustus · src/media_workflows.py. [Fuente](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/media_workflows.py). Consultado el 13 de septiembre de 2026. Lectura 1–190: inputs tipados, requisitos y fingerprint de MediaWorkflow.

[^F06]: Faustus · src/media_subtitles.py. [Fuente](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/media_subtitles.py). Consultado el 13 de septiembre de 2026. Exportadores SRT/VTT; reutilizan el contrato de segmentos del vídeo local.

[^A01]: ace-step/ACE-Step-1.5 · README.md. [Fuente](https://github.com/ace-step/ACE-Step-1.5/blob/main/README.md). Consultado el 13 de septiembre de 2026. Catálogo de operaciones musicales, variantes, API y recursos declarados; no benchmark propio.

[^W09]: ACE-Step · documentación de variantes. [Fuente](https://github.com/ace-step/ACE-Step-1.5). Consultado el 13 de septiembre de 2026. Tabla Model Zoo consultada: extract/lego/complete no equivalentes entre base, sft y turbo.
