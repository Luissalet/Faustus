# Pendientes de cierre

Actualizado: 08-09-2026. Sólo trabajo vigente; quitar cada entrada al cerrarla.

## Comprobaciones pendientes

- **Voz física:** conversación completa con micrófono en español e inglés. No activar grabación ni permisos para cerrar esta casilla sin intervención del usuario.

- **Un turno se declara correcto sin informe.** `rp-b737ffb10ef7` (08-09, 20:57) registró «IterResearch completed successfully» con 2 rondas y 14 URLs cuando la ronda 2 no había generado ninguna consulta y la síntesis no había producido nada: lo guardado son 8 hallazgos en bruto devueltos como respaldo. Un respaldo no es un informe y no debería salir por la puerta de «correcto». Revisar la rama de `Synthesis produced no report` en `src/deep_research.py`.

- **SearXNG contesta pero no busca.** El contenedor `odysseus-searxng-1` está sano y `/search?format=json` responde, pero con **0 resultados**. Mirar los motores de `SEARXNG_GENERAL_ENGINES` (`bing,mojeek,presearch`) antes de dar el proveedor por bueno.

- **Docker Desktop y el entorno del puente MCP.** Toda la noche del 08-09 se cayó al arrancar con `unable to get 'ProgramData'` cuando lo lanzaba una sesión a través del puente (cuatro vías probadas: directa, con el entorno repuesto, vía `explorer.exe` y como tarea programada interactiva). Lanzado por Luis a las 22:12 arrancó a la primera. El puente entrega un PowerShell **sin `ProgramData` ni `ALLUSERSPROFILE`**; anotado por si vuelve a aparecer con otro programa.

## Lista de Luis, 09-09 de madrugada (por orden de llegada)

1. **Research con Docker levantado → 0 fuentes, «no information».** Reproducir con SearXNG sano y mirar por qué el proveedor devuelve vacío (`SEARXNG_GENERAL_ENGINES=bing,mojeek,presearch`; el sondeo directo a `/search?format=json` dio 0 resultados a las 22:20).
2. **El Retry de una research no responde bien.**
3. **El texto del cuadro del chat se pierde al cambiar de chat.** Borrador por chat/proyecto que sobreviva a navegar y volver.
4. **«Waiting for the model» con el modelo ya cargado**, antes de empezar o a mitad del trabajo (captura: 01:28 esperando, con 35,3/44 GB y *PCIe spill*).
5. **Descargar modelos sólo llega a Qwen 3.5**; ya existe hasta 3.8. El catálogo de Discover está viejo.
6. **Cargar un modelo desde Modelos locales no enseña nada** hasta que termina: ni spinner ni estado.
7. **Un proyecto con el mismo nombre que su carpeta no se deja crear.**
8. **Personalización de modelos**: editar etiquetas individuales o un cuadro de *additional commands* para Ollama/llama.cpp (`-spec-type`, `draft-mtp`, `-spec-draft-n-max`, `-cache-type-k/-v`, `-np`, `-kv-cache-type-dtype`, `-jinja`…).
9. **Verificar todo por MCP y por pantalla.**

A comprobar de paso: la pantalla de Modelos locales lista **tres** GPUs (4070 Ti, 5060 Ti, 5060 Ti; 12 + 15,9 + 15,9 = 43,8 GB) y la GPU 1 sale sin lectura («— of 15.9 GB»). O hay tres tarjetas o una 5060 Ti aparece dos veces; en el segundo caso el presupuesto de 40,3 GB está inflado 15 GB.

## Lo que rompió la máquina el 08-09 (regla, no anécdota)

Dos 27B dentro a la vez —`q8_0` residente de una prueba (33 GB, con spill) y `q4_K_M` cargado por una research (17 GB)— más un build de Vite, dos tandas de pytest y siete procesos de Docker Desktop, superaron el **commit limit** de la máquina (147,7 GB = 128 de RAM + 20 de pagefile). La cascada, en orden: `cudaMalloc failed: out of memory` en la ronda 2, `MemoryError` en el servidor, `can't start new thread`, y después ni PowerShell arrancaba (`0xC000012D`, STATUS_COMMITMENT_LIMIT). El escritorio se quedó en negro con una sola ventana de error.

**La regla: nunca dos modelos grandes cargados a la vez, y nada pesado corriendo mientras hay uno dentro.** `ollama ps` antes de cargar, `ollama stop` del anterior. La puerta de admisión que automatiza esto es OBJ-1 en OBJETIVOS.md.

Las carencias de backend del índice anterior están implementadas; se ha eliminado ese índice vacío.
Las ampliaciones acordadas viven en OBJETIVOS.md; ahora mismo, OBJ-1 (puerta de admisión de VRAM). Eliminados los índices de UI resueltos; se pueden recuperar del historial Git.
No contar planes de inspiración o notas de implementación como otra cola de tareas.

## Última evidencia

- Cierre creativo/escritorio del 08-09: suite completa **13.236 correctas, 81 omitidas, cero fallos** (`logs/checkpoint-creative-full.xml`). Correcciones posteriores: **89 regresiones correctas**; transcripción y narración reales offline EN/ES: **4 correctas**. Adaptaciones de Diogenes: **471 regresiones correctas** (`logs/checkpoint-diogenes-regression.log`). Ventanas principal y secundaria, controles personalizados y propiedad del servidor comprobados en Electron real.

- 08-09: lanzadores web del repositorio probados con parada, arranque y reutilización sin duplicado. Ventana Electron real: minimizar, maximizar/restaurar, pantalla completa y cierre comprobados; cierra su backend propio y conserva el servidor web compartido. Barra integrada en temas y disponible también en el acceso. Regresiones de tareas/contexto: **68 correctas**; bloque creativo: **36 correctas**, incluido doblaje real local EN/ES; rutas de vídeo: **3 correctas**; controles de interfaz/escritorio/zonas horarias: **25 correctas**. TypeScript, scripts frontend, compilación y portfolio/CV correctos.

- Clientes oficiales en Brave (08-09, 02:07): Codex y Claude responden con sus sesiones de suscripción; Claude delega a un worker Codex, con resultado registrado, aprobación y cero cambios de archivos. Corregidos esquemas MCP incompatibles y descripciones ausentes de herramientas textuales. Bloque de **234 pruebas correctas**, controles de transporte de memoria y etiquetas de modelo, TypeScript y compilación correctos.

- Suite completa: **13.183 correctas, 81 omitidas y cero fallos** (`logs/checkpoint-context-client-full-20260908.xml`). Los cambios posteriores de edición/artefactos pasan **71 pruebas de regresión** (`logs/checkpoint-final-media.log`). Todos los scripts frontend, TypeScript y compilación correctos.
- Brave: Claude responde con skills automáticas desactivadas y presupuesto por turno; corregido el bloqueo de limpieza temporal de Windows. Editor→chat adjunta una copia sin enviar, conserva el borrador de capas/máscaras y abre adjuntos existentes. Compositor móvil corregido para mantener enviar/parar visible. Procedencia de artefactos comprobada con datos sintéticos, estados parciales y fallo recuperable de red.
- Navegador Brave y Qwen local: objetivo OBJ-1 creado en proyecto sin carpeta, conservado tras recarga; trabajo continuado fuera del chat. Respuesta inglesa tras herramienta y texto entre rondas completos tanto en vivo como tras recargar (08-09, 01:15).
- Objetivos y aislamiento: **225 correctas** (`logs/astra-objective-scope-focused.xml`).
- Integración: **203 correctas** (`logs/astra-objective-scope-integration.xml`).
- Servidor reiniciado el 08-09: HTTP 200.
- TypeScript y compilación de producción correctos. Separada la caché de React sin adelantar la carga del editor; ya no aparece el aviso de tamaño del bundle.
