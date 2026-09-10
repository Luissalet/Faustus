# FAUSTUS — Especificación integral de producto y arquitectura

Versión 2.0 · 10 de septiembre de 2026 · Documento para Fable y Codex

**Alcance:** 187 requisitos, 192 contratos lógicos de herramientas, 48 escenarios de aceptación y 32 referencias.

**Estado:** diseño propuesto basado en inspección documental y código puntual. No se ejecutó ni modificó Faustus.


## 00. Mandato y forma de utilizar esta especificación

**Objetivo:** convertir Faustus en una estación de trabajo local de propósito general que saque el máximo rendimiento útil de cada modelo compatible: conversación, programación, investigación, escritura, documentos, datos, multimedia y automatización. La calidad se mide por trabajo terminado y comprobable, no por número de agentes, botones o herramientas.

Esta versión sustituye y amplía el blueprint anterior. Mantiene su núcleo —estado, contexto, herramientas, verificación, recuperación, memoria y evaluación—, pero deja de tratar Faustus como una aplicación por construir: parte del repositorio real y añade contratos, integración, experiencia de usuario, pruebas y una secuencia de implementación.

**Lectura para Fable y Codex:** comenzar por 01–04; auditar después los puntos de integración de 01; implementar los requisitos P0 con pruebas y migraciones; usar el catálogo de herramientas como contratos lógicos, no como orden de crear cientos de funciones nuevas. El paquete complementario contiene este documento en Markdown, el backlog estructurado y contratos de ejemplo.

**Tres límites que no se negocian:** no se promete que todos los modelos tengan las mismas capacidades; no se presenta una función documentada como verificada en esta sesión; no se declara completado un trabajo porque el modelo lo afirme. Un endpoint compatible, un JSON válido y un test que termina en verde son tres evidencias distintas, ninguna equivalente por sí sola a que la petición completa esté resuelta.

| Concepto | Definición de producto |
| --- | --- |
| Universalidad | Conectar cualquier modelo soportado por un adaptador y degradar de forma explícita cuando no tenga herramientas, visión, contexto o velocidad suficientes. No fingir capacidades ausentes. |
| Independencia | Faustus conserva proyectos, memoria, reglas, evidencias y procedimientos al cambiar de modelo o proveedor. Los pesos, motores externos y servicios opcionales no se confunden con funciones incluidas. |
| Mejor experiencia | Menos pérdidas de trabajo, bucles, esperas opacas, configuraciones a ciegas y acciones no deseadas; más control, resultados verificables y continuidad. |
| Local-first | La ruta local no depende obligatoriamente de APIs de pago. Cualquier salida de contenido a un servicio remoto se declara y se autoriza, incluidos embeddings, resúmenes y telemetría. |
| Alcance principal | Windows, interfaz gráfica y carpetas locales, también sin Git. Docker, terminal, nube, clusters y colaboración son capacidades, no barreras obligatorias para empezar. |

**Prioridades:** P0 = integridad, compatibilidad o flujo esencial; P1 = ventaja práctica de uso frecuente; P2 = ampliación especializada; LAB = experimento que no entra por defecto. Son prioridades propuestas, no estimaciones de calendario ni afirmaciones de que hoy falte cada elemento.


## 01. Punto de partida real: conservar, comprobar y ampliar

**Referencia inspeccionada:** Luissalet/Faustus, rama master, commit `b824057edf40a6fa20caec9c1f77b9e6a9d75bad`, registrado el 10 de septiembre de 2026 a las 01:00:48 UTC. Se consultaron el README, los objetivos, el registro de pendientes, el sistema de diseño, documentación de evidencias, el directorio Studio y código de interacción. No se clonó ni ejecutó la aplicación, no se corrieron sus tests y no se modificó el repositorio. [F01–F08]

**Leyenda de certeza:** CÓDIGO = comportamiento visible en el archivo leído; DOCUMENTADO = afirmación del propio repositorio que debe comprobarse en el checkout de implementación; PROPUESTA = requisito de este documento; LAB = hipótesis que exige mediciones. Una prueba reportada en PENDIENTES.md pertenece a su autor y a su ejecución histórica, no a una validación nuestra.

| Área y evidencia | Qué conservar / siguiente comprobación |
| --- | --- |
| Python/FastAPI + React/TypeScript; escritorio Electron. DOCUMENTADO [F02]. | Mantener el stack y el sistema visual existente. No reescribir en otro framework para implementar este plan. |
| Context Engine, Project Context Links, perfiles, Enséñame, Immune System, Branching Futures, Consejo, State Mirror, Delta Engine, Completion Engine y voz. DOCUMENTADO [F02]. | Auditar contratos y conexión extremo a extremo. La existencia de estos módulos impide presentar memoria, contexto o multiagentes como ideas completamente nuevas. |
| Admisión VRAM en chat, research y carga manual; opciones por modelo; fases de carga/lectura/spill. DOCUMENTADO [F03–F04]. | Reutilizar src/vram_admission.py, src/vram_fit.py y las rutas de modelos. Ampliar coordinación, reservas y cobertura sin crear otro diálogo paralelo. |
| AskUserTool y UpdatePlanTool. CÓDIGO [F05]. | ask_user emite una decisión de UI y termina el turno; la selección llega como nuevo mensaje. update_plan recibe Markdown y lo corta a 8.192 caracteres. Migrar con compatibilidad: pregunta durable y plan estructurado sin truncamiento silencioso. |
| Recibos de cambios persistentes. DOCUMENTADO [F06]. | src/changeset_store.py ya separa guardar evidencia de verificar éxito. Reusar su aislamiento por propietario y versiones; no instalar un segundo ledger incompatible. |
| Investigación tras reinicio. DOCUMENTADO [F04]. | Se registra interrupted y se ofrece Reintentar desde cero. Añadir reanudación desde unidades confirmadas; no llamar a lo actual recuperación completa del progreso. |
| Borradores por chat, preguntas con opciones, salud de búsqueda y avisos persistentes. DOCUMENTADO [F04]. | Convertir correcciones recientes en pruebas de regresión. No reimplementar los síntomas sin verificar que el fallo siga existiendo. |
| Fuente de diseño editorial, cálida y técnica. DOCUMENTADO [F08]. | Reutilizar DESIGN.md, tokens, temas, tipografía local, densidades y estados; no sustituir la identidad por un clon genérico de otra aplicación. |
| studio/ inspeccionado; no se recuperó studio/package.json en este snapshot [F07]. | Comprobar cómo se obtienen o generan manifiestos y lockfiles y demostrar un build desde checkout limpio. No deducir solo de esa ausencia que el proyecto no compile. |


### 01.1 Hallazgos que deben convertirse en requisitos

El registro reciente muestra fallos de integración especialmente útiles para diseñar regresiones: opciones convertidas a «[object Object]»; clasificación de peticiones españolas que omitía herramientas de escritura; dos definiciones de reglas efectivas según la documentación; consultas devueltas como objeto en lugar de lista; informes que omitían secciones al aplanar el brief; y research registrada como éxito sin rondas ni fuentes. Son casos documentados como corregidos, no bugs abiertos que este documento vuelva a declarar. [F04]

La próxima mejora debe preguntarse «¿qué contrato evita esta clase completa de fallo?» antes que añadir otro parche al prompt. Una UI bonita no compensa una proyección incorrecta; un resumen persistido no equivale a estado reanudable; una cifra de VRAM libre no representa memoria reservable mientras otro proceso carga un modelo.

**BASE-01 · Mapa de reutilización antes de tocar código · P0.** Backend: Inventariar rutas, stores, componentes, workers y tests por cada requisito; marcar existente, parcial, ausente o no verificable. Interfaz: Mostrar la versión de aplicación y build realmente servidos en Diagnóstico. Aceptación: Entregar mapa con rutas verificadas y una ejecución base reproducible; ningún requisito se cierra solo por nombre de módulo.

**BASE-02 · Una autoridad por estado · P0.** Backend: Definir qué store manda sobre tareas, permisos, memoria, cambios y modelos; las demás vistas son proyecciones versionadas. Interfaz: Recargar o cambiar de cliente reconstruye exactamente el estado confirmado. Aceptación: Producir el mismo resultado desde eventos, API e historial; detectar y reparar proyecciones divergentes.

**BASE-03 · Build limpio y diagnóstico de instalación · P0.** Backend: Verificar manifiestos, lockfiles, assets, versiones de Python/Node y launcher; no depender de archivos locales no registrados sin un paso explícito de generación. Interfaz: Asistente de arranque muestra el componente ausente y una reparación acotada. Aceptación: Una máquina de prueba prepara y abre Faustus siguiendo documentación sin copiar archivos de la instalación personal.


## 02. Comparativa: qué tomar y qué no asumir

Comparación documental consultada el 10-09-2026. No es un benchmark de rendimiento ni una prueba exhaustiva de todas las versiones, planes y sistemas operativos. «Carencia» se usa solo cuando está documentada; en los demás casos se distingue una limitación de enfoque o un riesgo que debe probarse. No se asignan estrellas inventadas ni se confunde el producto de escritorio con su API o CLI.

| Aplicación / capacidad documentada | Límite o riesgo y decisión para Faustus |
| --- | --- |
| ChatGPT / Work / Codex. Documentación de proyectos, herramientas, trabajo prolongado, terminal, revisión y worktrees. [S01] | REFERENCIA de integración y continuidad. No asumir portabilidad equivalente de esas funciones a cualquier modelo local. Faustus debe tener un núcleo propio, estado exportable y control de inferencia independiente. |
| Claude Cowork. Trabajo sobre archivos y flujos guiados de tareas. [S02] | REFERENCIA de delegación accesible sin vivir en una CLI. La portabilidad completa de ese comportamiento a modelos ajenos no se establece aquí; Faustus no debe depender de un cliente comercial para el camino local básico. |
| Claude Code. Subagentes con contexto y herramientas delimitados; checkpoints. [S03–S04] | CARENCIA DOCUMENTADA: rewind no cubre todos los cambios por shell, subagentes o actores externos. Faustus debe declarar la cobertura exacta del rollback y detectar mutaciones fuera de ella, sin prometer deshacer efectos remotos. |
| Open WebUI. Chat multimodelo, RAG híbrido, multimedia, herramientas, MCP/OpenAPI y controles de acceso. [S05] | No tratarlo como un chat simple. RIESGO DE DISEÑO: copiar su amplitud sin ligar cada resultado a un contrato de tarea. Faustus debe probar el flujo completo, no solamente la conexión y la presencia de botones. |
| LibreChat. Agentes configurables, herramientas diferidas y streams reanudables. [S06–S07] | DISTINCIÓN: reconectar una transmisión no demuestra recuperación del cómputo tras caída del servidor. Faustus debe ofrecer ambas garantías por separado y conservar borradores, eventos y decisiones pendientes. |
| LM Studio. Gestión local e interfaz de servidor con tool use nativo o adaptado. [S08–S09] | LIMITACIÓN DOCUMENTADA: llamadas mal formadas pueden terminar como texto y la calidad depende del modelo. Faustus necesita pruebas de capacidad y adaptación segura; no ejecutar automáticamente el texto que parezca una herramienta. |
| AnythingLLM. Agentes, documentos y flujos de trabajo. [S10] | REFERENCIA de onboarding y conocimiento por workspace. No se infiere que le falten edición, herramientas o memoria. Para Faustus, exigir evidencia por página/rango y calidad de recuperación medida, además de que «se indexó». |
| OpenHands. SDK, agentes y entorno de ejecución para trabajo de desarrollo. [S11] | REFERENCIA de separación entre agente y ejecución. Su foco documental de desarrollo no demuestra una experiencia completa de escritura o multimedia: Faustus debe medir también esos recorridos y no quedar reducido a coding. |
| Aider. Mapa de repositorio basado en estructura del código. [S12] | REFERENCIA de contexto selectivo. Un mapa ayuda a localizar; no sustituye evidencia actual, documentos no-code ni estado de tarea. Reutilizar la idea, sin imponer Git ni una interfaz de terminal al usuario. |
| Cline. Checkpoints con repositorio sombra. [S13] | COSTE DOCUMENTADO: almacenamiento y ralentización en proyectos grandes. Faustus ya documenta Git sombra: mejorar cobertura, exclusiones, cuotas y restauración sin perderlo ni duplicarlo. |
| Continue. Integración de modelos locales mediante Ollama. [S14] | REFERENCIA de adaptadores y configuración. No confundir elegir modelo con validar tool calling, modalidades y ajuste al hardware. Faustus debe calibrar la combinación exacta y explicar sus límites. |
| LangGraph. Persistencia y ejecución por checkpoints. [S15] | Es infraestructura, no una aplicación rival completa. Adoptar principios de durabilidad sin imponer una migración del runtime actual. La reejecución debe controlar efectos laterales e idempotencia. |


### 02.1 Huecos transversales que Faustus debe cerrar

La oportunidad no es «tener todo lo que tengan los demás» indiscriminadamente. Es unir cinco garantías: cualquier resultado puede remontarse a su evidencia; cualquier capacidad se declara según el modelo y backend reales; cualquier acción puede inspeccionarse y tiene una política de recuperación; cualquier tarea larga conserva progreso utilizable; y cualquier pantalla conserva el trabajo del usuario aunque haya fallos debajo.

Tampoco conviene heredar puntos ciegos frecuentes de diseño: un botón Stop que solo detiene la animación, memorias no corregibles, costes de modelos auxiliares ocultos, recuperación que duplica envíos, citas cuyo destino no respalda la frase, plugins con permisos irrestrictos o un «modo local» que manda resúmenes a la nube. Se plantean aquí como amenazas a probar, no como acusaciones no verificadas a todas las aplicaciones comparadas.


## 03. Arquitectura objetivo sin reescritura

Separar cuatro planos lógicos sobre los componentes existentes. **Control:** tareas, presupuesto, permisos, planes y decisiones. **Ejecución:** modelos, herramientas, procesos, conectores y subagentes. **Datos:** proyectos, evidencias, artefactos, historial, índices y memoria. **Presentación:** Studio, escritorio y clientes remotos. La separación es contractual; no requiere convertir una aplicación personal en decenas de microservicios.


```text
Usuario / Studio / Electron / cliente remoto
                    |
          API de control autenticada
                    |
  TaskState + eventos + permisos + presupuesto
                    |
        Orquestador existente de Faustus
          /         |          \
   Context Engine  Tool Broker  Model Gateway
     |              |           |
   memoria       ejecutores    Ollama / llama.cpp /
   proyectos     aislados      vLLM / endpoint compatible
   fuentes       MCP / APIs    runners opcionales
          \         |          /
        evidencias + ChangeSets + artefactos
                    |
       verificación + revisión + recuperación
                    |
         proyecciones coherentes de Studio
```

La persistencia no debe depender del navegador. Para una instalación personal, mantener stores locales existentes y una cola durable sencilla suele ser preferible a añadir Redis, un cluster y otra base de datos. Escalar a servicios compartidos solo cuando exista un requisito y un benchmark que lo justifiquen. SQLite y almacenamiento de objetos local pueden coexistir; evitar transacciones que pretendan cubrir mágicamente filesystem, GPU y APIs externas.

**ARCH-01 · API y eventos versionados · P0.** Backend: Contratos tipados para turno, evento, herramienta y artefacto; negociación de versión y errores de incompatibilidad explícitos. Interfaz: Avisar cuando frontend y backend no coinciden; no mostrar payloads desconocidos como éxito. Aceptación: Un cliente antiguo falla de forma comprensible ante un evento nuevo obligatorio; compatibilidad probada para versiones soportadas.

**ARCH-02 · Separar cliente y proceso propietario · P0.** Backend: La desconexión del cliente no mata una tarea durable; registrar quién arrancó y quién puede detener cada proceso. Interfaz: Cerrar pestaña, cerrar ventana y detener servidor son acciones distintas y describen sus consecuencias. Aceptación: Cerrar un cliente no detiene Ollama ni procesos ajenos; reabrir recupera el trabajo permitido.

**ARCH-03 · Configuración efectiva explicable · P0.** Backend: Resolver precedencia global/proyecto/rol/modelo/turno en un único compilador; producir hash y origen de cada valor. Interfaz: Inspector «qué instrucciones y opciones se están aplicando» con secretos ocultos. Aceptación: Una regla duplicada o incompatible aparece como conflicto; no gana silenciosamente por orden accidental de definición.


## 04. Contrato de tarea y ejecución durable

Una conversación contiene mensajes; una tarea contiene objetivo, restricciones, decisiones, criterios de aceptación y progreso. Una ejecución es un intento concreto; una llamada es una operación de modelo o herramienta. Deben tener identificadores diferentes. Bifurcar una conversación no duplica los permisos ni reinicia acciones externas ya realizadas.


```text
Task: draft -> ready -> running
                    -> waiting_user / waiting_approval / waiting_resource
                    -> paused / interrupted / blocked
                    -> verifying -> succeeded / partial / failed / cancelled
Run: created -> admitted -> executing -> settling -> terminal
Call: planned -> validated -> authorized -> started
      -> succeeded / failed / cancelled / outcome_unknown
```

**Invariantes:** solo un propietario válido avanza una ejecución a la vez; cada transición tiene versión y causa; los cambios del usuario nunca desaparecen; cancelled no significa que no hubiera efectos; partial no se maquilla como succeeded; guardar un recibo no modifica el veredicto de verificación. Una operación remota cuyo resultado se desconoce entra en conciliación, no en reintento automático.

**TASK-01 · Estado estructurado y plan no truncable · P0.** Backend: Pasos con IDs, dependencias, estado y evidence_refs; historial de revisiones y compatibilidad con update_plan actual. Interfaz: Plan editable y plegable con criterios sin resolver, sin confundir una casilla marcada por el modelo con verificación. Aceptación: Un plan de más de 8.192 caracteres conserva todos sus pasos; el último requisito sigue pendiente hasta obtener evidencia.

**TASK-02 · Reanudación por unidades confirmadas · P0.** Backend: Persistir rondas, fuentes, pasos y efectos confirmados; reconstruir desde checkpoint y conciliar operaciones en vuelo. Interfaz: Diferenciar Reanudar, Reintentar desde cero y Crear alternativa; explicar qué se conserva. Aceptación: Forzar reinicio entre dos rondas de research y continuar sin repetir las completadas ni perder citas.

**TASK-03 · Diario transaccional e idempotencia · P0.** Backend: Registrar intención antes de ejecutar, resultado después y claves de operación; usar patrón outbox donde aplique. Interfaz: Mostrar «resultado incierto, comprobando» en vez de un falso error terminal. Aceptación: Reenviar la misma petición tras una pérdida de respuesta no crea dos tareas ni duplica un envío.

**TASK-04 · Pausa, cancelación y decisiones durables · P0.** Backend: Cancelación propagada a subagentes y procesos con política de limpieza; preguntas identificadas y respuestas asociadas por versión. Interfaz: Stop generación, Pausar tarea y Cancelar trabajo tienen etiquetas y efectos distintos. Aceptación: Una respuesta tardía a una pregunta cancelada no despierta el turno; un hijo no continúa escribiendo después de revocar su tarea.

**TASK-05 · Steering y cambios del usuario · P1.** Backend: Encolar instrucciones durante ejecución y aplicarlas en puntos seguros; invalidar pasos afectados por una nueva restricción. Interfaz: Permitir «cambia de enfoque» sin destruir el borrador; mostrar cuándo la instrucción entró en vigor. Aceptación: La corrección del usuario durante un test queda en el estado antes del siguiente cambio de archivos.

**TASK-06 · Presupuesto de autonomía · P0.** Backend: Límites separados de llamadas, tokens, tiempo activo, memoria, gasto remoto y subagentes; pausas humanas no consumen timeout de inferencia. Interfaz: Presets supervisado, autónomo acotado y solo lectura con presupuesto visible. Aceptación: Al agotar un presupuesto se guarda un checkpoint útil; no se borra el trabajo ni se amplían permisos para terminar.


## 05. Model Gateway: compatibilidad real, no una URL y un nombre

La unidad de compatibilidad es **modelo + revisión de pesos + cuantización + tokenizer + plantilla de chat + backend + versión + parámetros efectivos**. Cambiar cualquiera de esos elementos puede invalidar una calibración. El mismo nombre de modelo servido por dos motores no garantiza llamadas, modalidades ni límites idénticos. Ollama, llama.cpp, vLLM y LM Studio documentan caminos distintos de tool calling; deben aislarse en adaptadores. [S08, S16–S18]

**MOD-01 · Manifiesto de capacidades comprobadas · P0.** Backend: Registrar texto, visión, audio, herramientas, JSON/schema, streaming, cancelación, ventana y controles de razonamiento como soportado, no soportado o desconocido. Interfaz: Badges explican si la capacidad fue anunciada o probada y cuándo; no deducir visión por el nombre. Aceptación: Un modelo de texto no recibe imágenes perdidas silenciosamente: se ofrece adaptador de visión autorizado o se informa del límite.

**MOD-02 · Calibración breve y suite ampliada · P0.** Backend: Fixtures sin efectos externos para JSON, selección de herramienta, resultados múltiples, argumentos españoles, recuperación y contexto; guardar resultados por combinación exacta. Interfaz: Botón Probar capacidades con coste previsto; informe por dimensión, no nota de inteligencia única. Aceptación: Una plantilla incorrecta reduce la calificación y desactiva autonomía riesgosa hasta resolverla.

**MOD-03 · Normalización de mensajes y stream · P0.** Backend: Separar texto, llamadas, resultados, rechazos, finalización y bloques opacos del proveedor; preservar IDs y orden causal. Interfaz: Nunca mostrar JSON interno como respuesta normal ni animar bloques que no son texto del usuario. Aceptación: Reproducir streams fragmentados, finish_reason ausente, múltiples llamadas y reconexiones sin duplicar contenido.

**MOD-04 · Opciones con ámbito y precedencia · P0.** Backend: Distinguir flags de proceso, opciones de carga y parámetros por petición; validar cada uno en su adaptador. Interfaz: Campos incompatibles deshabilitados con motivo; ver los valores efectivos y restaurar un preset. Aceptación: Un flag de llama-server no se envía como opción Ollama; un cambio que exige recarga pide permiso y muestra impacto.

**MOD-05 · Router medido y privacidad transitiva · P1.** Backend: Routing por capacidad, calidad observada, latencia, residencia y política; todos los auxiliares heredan restricciones de datos. Interfaz: Fijar modelo por tarea/rol y ver cada escalado; nunca pasar a una API de pago silenciosamente. Aceptación: Un fallo de autenticación del runner no dispara gasto API; local-only bloquea también resumidores remotos.

**MOD-06 · Degradación y compatibilidad portable · P0.** Backend: Fallback de herramientas nativas a formato restringido solo tras calibración; conversión explícita de contexto al cambiar de familia. Interfaz: Mantener el hilo y explicar funciones temporalmente no disponibles. Aceptación: Cambiar modelo conserva evidencias y tareas; no interpreta bloques opacos antiguos como texto ejecutable ni borra adjuntos.

El modo de razonamiento de un modelo se respeta cuando el proveedor lo expone. No se inventan controles equivalentes entre familias ni se exige revelar razonamiento privado para depurar. La observabilidad se apoya en decisiones resumidas, fuentes, llamadas y resultados. Un modelo no entrenado para herramientas puede quedar restringido a chat o a pasos supervisados: eso es compatibilidad honesta, no fracaso del producto.


## 06. Tool calling: protocolo, validación y recuperación

Una llamada válida tiene identidad, herramienta disponible, versión, argumentos completos y autorización. **JSON válido no implica una acción válida**: una ruta puede escapar del workspace, una URL puede apuntar a la red interna y un identificador puede pertenecer a otro usuario. El modelo propone; el broker valida y ejecuta. Los ejemplos de LM Studio muestran por qué el parseo puede fallar incluso existiendo una herramienta anunciada. [S08]

**CALL-01 · Assembler incremental seguro · P0.** Backend: Acumular deltas por call_id/index; decodificar UTF-8 y escapes; exigir mensaje completo, límites de bytes/profundidad y JSON sin claves duplicadas. Interfaz: Mostrar «preparando llamada» sin ejecutar fragmentos; permitir cancelar antes de autorizar. Aceptación: Cortar un stream a mitad de una ruta o un número produce cero efectos; chunks reordenados o duplicados se detectan.

**CALL-02 · Validación sintáctica, semántica y contextual · P0.** Backend: Schema, tipos estrictos, enums, rangos, referencias, propietario y precondiciones; no coercionar «false» a verdadero ni inventar parámetros faltantes. Interfaz: Error junto al argumento problemático con opción de corregir o volver al modelo. Aceptación: Una ruta inválida, una cuenta ajena o un archivo obsoleto se rechazan antes de iniciar procesos.

**CALL-03 · Reparación limitada sin cambiar intención · P0.** Backend: Permitir reparar formato con la misma herramienta y sin ampliar destinos/permisos; máximo acotado, luego pedir una llamada nueva. Interfaz: Distinguir llamada original, corrección y acción finalmente autorizada. Aceptación: Texto citado que contiene tool_call no se ejecuta; una reparación no sustituye read_file por shell.

**CALL-04 · Paralelismo según efectos · P0.** Backend: Agrupar lecturas independientes; serializar escrituras sobre recursos compartidos; bloquear por archivo/entidad con versión esperada. Interfaz: Agrupar actividad concurrente sin ocultar una aprobación pendiente. Aceptación: Dos herramientas que modifican el mismo archivo no pisan cambios; lecturas sin dependencia sí pueden solaparse.

**CALL-05 · Resultado tipado y continuación correcta · P0.** Backend: Retornar status, datos, evidence_refs, side_effects y error; preservar asociación con la llamada aunque termine fuera de orden. Interfaz: Tarjeta compacta con resultado, duración y enlace a evidencia completa. Aceptación: Un éxito de transporte con error funcional no marca el paso hecho; toda llamada tiene exactamente un resultado lógico terminal.

**CALL-06 · Reintentos por clase, no por optimismo · P0.** Backend: Backoff con jitter y Retry-After; solo repetir lecturas o escrituras idempotentes; conciliar outcome_unknown antes de reintentar. Interfaz: Explicar «servidor saturado» frente a «acción quizá completada» y ofrecer comprobación. Aceptación: Simular envío completado seguido de timeout: no se envía otra vez sin reconciliar.

**CALL-07 · Interacción humana tipada · P0.** Backend: Generalizar ask_user con question_id, opciones estables, multiselección, respuesta libre y estado persistido, manteniendo aliases. Interfaz: Opciones con consecuencia, recomendación y caja libre; nunca [object Object]; una elección no exige reconstruir la pregunta. Aceptación: Recarga, timeout, doble clic y respuesta desde otro cliente no generan decisiones contradictorias.

**CALL-08 · Garantía de no actuar por texto · P0.** Backend: Solo ejecutar salidas del canal/protocolo autorizado; desactivar recuperación heurística de comandos desde prosa, documentos o razonamiento. Interfaz: Marcar sugerencias como sugerencias; Ejecutar requiere convertirlas a una acción revisable. Aceptación: Un README malicioso con un bloque de herramienta no dispara ninguna operación.

El sistema puede reconocer un formato nativo defectuoso para ayudar a generar una llamada nueva, pero no debe «adivinar lo que quería hacer» y actuar. Los ejemplos few-shot se seleccionan por familia y tarea; se versionan y se evalúan. Aumentar temperatura, añadir cinco verificadores o repetir el mismo prompt no es una política de recuperación universal.


## 07. Tool Broker, MCP, plugins y skills

El objetivo es **un catálogo amplio y una exposición pequeña**. La sesión dispone de herramientas de búsqueda y descubrimiento, más un conjunto básico; carga esquemas completos solo cuando son pertinentes. MCP distingue herramientas, recursos y prompts, y sus anotaciones son información declarada por el servidor, no autorización confiable. El broker de Faustus conserva la última palabra. [S19–S20]

**TOOL-01 · Registro único de herramientas · P0.** Backend: Un ToolDescriptor versionado define schema, ámbitos, riesgo, idempotencia, timeout, salida y compatibilidad; aliases preservan nombres actuales. Interfaz: Catálogo con búsqueda, favoritos, estado, permisos y prueba segura. Aceptación: La misma herramienta tiene igual contrato en chat, workflow, subagente y MCP; no existe un atajo sin validación.

**TOOL-02 · Descubrimiento bajo demanda · P1.** Backend: Ranking híbrido de herramientas por intención/capacidad y un mínimo determinista por tarea; límites al número de esquemas visibles. Interfaz: El usuario puede fijar o excluir herramientas por proyecto y ver por qué se eligieron. Aceptación: Una petición española de editar .txt mantiene acceso a lectura/escritura aunque falle el ranking semántico.

**TOOL-03 · Ciclo de vida MCP · P0.** Backend: Negociación de capacidades y versión, stdio/HTTP según soporte, autenticación, reconexión, cancelación y paginación; cambios de catálogo invalidan cachés. Interfaz: Conexión Probando/Lista/Degradada/Auth requerida con registro sanitizado. Aceptación: Una caída o revocación no deja un turno colgado; las herramientas revocadas desaparecen antes de la siguiente acción.

**TOOL-04 · Permisos de extensión y cuarentena · P0.** Backend: Manifiesto con versión, hash, dependencias, acceso a red/archivos/secretos y proceso aislado; actualización no amplía permisos automáticamente. Interfaz: Instalar muestra procedencia y diferencias de permisos; desactivar o revertir en un clic. Aceptación: Un plugin intenta leer credenciales fuera de su ámbito y el broker lo bloquea aunque el modelo lo solicite.

**TOOL-05 · Elicitación, sampling y recursos con límites · P1.** Backend: Mapear funciones MCP avanzadas solo si se negocian; sampling consume presupuesto; roots orienta al servidor pero no reemplaza el sandbox. Interfaz: Preguntas del conector muestran qué servicio pregunta y qué datos recibirá. Aceptación: Un servidor no puede recursar indefinidamente pidiendo modelos ni reutilizar una autorización de otro destino.

**TOOL-06 · Skills evaluables, no instrucciones privilegiadas · P1.** Backend: Procedimientos con entradas, evidencias de éxito, versiones, dependencias y pruebas; promoción desde Enséñame requiere revisión. Interfaz: Editor con historial, simulación, permisos y activar por proyecto; ninguna skill se convierte en regla global por sorpresa. Aceptación: Una skill obsoleta falla antes de mutar datos y ofrece la versión previa; su contenido no eleva privilegios.

Los hooks antes/después de herramientas usan el mismo aislamiento y presupuesto. Los conectores OpenAPI se importan tras validar el schema, la autenticación y los efectos de cada operación; GET no se presume inofensivo solo por el verbo HTTP. Las instrucciones procedentes del repositorio, de una skill y de un servidor MCP tienen procedencias diferenciadas y nunca reemplazan políticas de seguridad.


## 08. Context Engine: conservar precisión sin llenar la ventana

Ampliar `src/context_engine/` en lugar de escribir un segundo gestor. El contexto enviado es un manifiesto reproducible de requisitos, evidencia exacta, resúmenes con procedencia y herramientas habilitadas. Mantener fuera del prompt los grandes logs y archivos completos que no se necesitan. Los subagentes y los mapas de repositorio son mecanismos útiles de aislamiento y selección, no garantías de corrección. [F02, S03, S12]

**CTX-01 · Presupuesto real por modelo y modalidad · P0.** Backend: Usar tokenizer correcto cuando esté disponible; reservar salida y overhead; marcar estimaciones para imágenes/audio o tokenizer desconocido. Interfaz: Medidor separa instrucciones, historial, memoria, archivos, herramientas y margen. Aceptación: Un adjunto grande no desplaza la petición ni los criterios; no se comunica una cifra estimada como conteo exacto.

**CTX-02 · Compresión con integridad semántica · P0.** Backend: Preservar requisitos, negativos, decisiones y referencias; comprobar esquema/resumen contra el estado canónico y registrar lo omitido. Interfaz: Ver resumen, recuperar fuente y fijar fragmentos que no deben comprimirse. Aceptación: Un resumen nunca transforma «no cambiar API» en «cambiar API»; al no poder conservarlo, retiene el texto original.

**CTX-03 · Ventana de trabajo y evidencia exacta · P0.** Backend: Rangos y versiones de fuentes; lectura paginada, búsqueda dentro de output y carga incremental; deduplicar tokens sin perder procedencia. Interfaz: Inspección de qué se incluyó, por qué y qué quedó fuera. Aceptación: El modelo puede volver al fragmento exacto que fundamenta un resumen aunque cambie la ventana activa.

**CTX-04 · Cache correcto y reindexación · P1.** Backend: Claves por modelo/tokenizer/plantilla/política/usuario/versión de fuente; invalidación cuando cambia cualquiera. Interfaz: Mostrar índices desactualizados y permitir reconstruir sin borrar documentos. Aceptación: Editar un archivo o revocar su acceso invalida recuperaciones y prefijos dependientes; no hay fuga entre proyectos.

**CTX-05 · Recuperación selectiva con control del usuario · P1.** Backend: Ámbitos de sesión, proyecto y memoria global separados; exclusiones por archivo/carpeta y fuentes explícitas prioritarias. Interfaz: Acciones «usar este fragmento», «no usar esta fuente» y «sin memoria automática» con alcance visible. Aceptación: Excluir una memoria no borra el archivo fuente ni se aplica por error al historial completo.

**CTX-06 · Salud del contexto sin otro LLM obligatorio · P1.** Backend: Medir repetición, contradicciones con restricciones y lecturas sin progreso mediante reglas; usar evaluación adicional solo cuando aporte valor. Interfaz: Aviso accionable: contexto saturado, evidencia obsoleta o plan perdido; botón reconstruir tarea. Aceptación: Recuperar una tarea tras compactación manteniendo objetivo, restricciones, cambios y siguiente acción correctos.


## 09. Proyecto, búsqueda local e ingestión documental

Un workspace es una carpeta o conjunto autorizado de carpetas, no necesariamente un repositorio. Su identidad es estable aunque cambie su nombre visible. El índice combina árbol, búsqueda literal, símbolos, relaciones y embeddings opcionales. Para documentos no-code importa la página, sección, tabla, fecha y versión; para código, definición, llamadas, tests y configuración.

**IDX-01 · Identidad de carpetas y fuentes · P0.** Backend: Workspace IDs estables, rutas canonizadas, tratamiento de mayúsculas, unidades y enlaces; separar proyecto de carpeta de chats. Interfaz: Selector nativo, recientes y reubicar carpeta; indicar inexistente/desconectada sin perder el proyecto. Aceptación: Renombrar una carpeta no duplica memorias ni convierte un path de Windows en una ruta remota inválida.

**IDX-02 · Índice incremental y análisis de código · P1.** Backend: Símbolos y referencias con parsers/LSP donde proceda; watch con debounce, ignorados, cancelación y límites de archivos. Interfaz: Progreso por fases y exclusiones editables; mostrar último índice confirmado. Aceptación: Cambiar una función actualiza referencias y tests candidatos sin reembebir todo el workspace.

**IDX-03 · Recuperación híbrida medible · P1.** Backend: Combinar literal, BM25/semántica, símbolos y vecindad; reranking opcional, top-k adaptativo y recuperación de evidencia negativa. Interfaz: Resultados con fragmento, origen y razón de relevancia; abrir definición y llamadores. Aceptación: Suite con nombres raros, español y código encuentra el archivo correcto aunque la similitud semántica falle.

**IDX-04 · Ingestión robusta de archivos · P0.** Backend: Detectar MIME, encoding, archivos dañados, cifrados y grandes; extraer texto nativo antes de OCR; conservar estructura y errores por archivo. Interfaz: Adjunto Subiendo/Extrayendo/Listo/Parcial/Error con reintento individual y preview. Aceptación: Un PDF escaneado no figura como leído si solo se extrajo una portada; un archivo fallido no bloquea los demás.

**IDX-05 · Referencias multimodales exactas · P1.** Backend: EvidenceRef admite página, celda, símbolo, bbox y rango temporal con hash de versión; capturas derivadas conservan origen. Interfaz: Saltar de una cita al párrafo, celda o segundo correspondiente. Aceptación: Un gráfico o tabla no se resume desde texto ausente sin visualizarlo o declarar limitación.

**IDX-06 · Grandes colecciones sin secuestrar el PC · P1.** Backend: Colas con prioridad baja, límites RAM/CPU, pausar al inferir, deduplicación por contenido y limpieza de índices huérfanos. Interfaz: Panel de almacenamiento con tamaño de originales, derivados e índices separados. Aceptación: Indexar una carpeta extensa no bloquea escribir en el chat ni provoca carga involuntaria de otro LLM grande.


## 10. Memoria, State Mirror y aprendizaje verificable

La memoria debe diferenciar hechos explícitos, observaciones, inferencias, decisiones, preferencias y procedimientos. Un hecho sin vigencia puede convertirse en una trampa. El State Mirror y el Immune System ya documentados son puntos de integración para estado observado e incidentes; no deben alimentar una única bolsa de texto donde hipótesis antiguas reaparecen como certezas. [F02]

**MEM-01 · Tipos, alcance, vigencia y procedencia · P0.** Backend: Cada entrada tiene autor/origen, owner, proyecto, fecha, validez, sensibilidad y estado confirmado/inferido/obsoleto. Interfaz: Memoria consultable y editable con «por qué lo recuerda»; distinguir recuerdo explícito de inferencia. Aceptación: Una hipótesis refutada no vuelve como hecho en un chat nuevo ni contamina otro proyecto.

**MEM-02 · Corrección, olvido y borrado propagado · P0.** Backend: Tombstones y eliminación de índices/caches derivados según política; evitar resurrección desde consolidaciones o imports. Interfaz: Olvidar explica qué borra y qué permanece en archivos originales o copias de seguridad. Aceptación: Borrar un recuerdo y reconstruir el índice no lo restaura; copias retenidas tienen política explícita.

**MEM-03 · Memoria de fallos con promoción controlada · P1.** Backend: Registrar clase de incidente y evidencia; proponer regla correctiva tras validación, con caducidad y prueba de no regresión. Interfaz: Panel «problema recurrente → regla propuesta → pruebas → activación». Aceptación: Un fallo aislado no produce una prohibición global que empeore otras tareas.

**MEM-04 · Continuidad de proyecto y decisiones · P1.** Backend: Registrar decisión, alternativas descartadas y alcance; invalidarla cuando cambia su premisa sin borrar su historia. Interfaz: Timeline de decisiones vinculada a artefactos y cambios. Aceptación: Cambiar de modelo conserva el motivo de una elección técnica sin arrastrar todo el transcript.

**MEM-05 · Incógnito de extremo a extremo · P0.** Backend: Excluir persistencia automática de mensajes, memorias, evidencias y telemetría de contenido; aislar temporales y su limpieza. Interfaz: Explicar qué sí puede persistir por acción explícita, como un archivo creado por el usuario. Aceptación: Terminar una sesión incógnita no deja contenido en títulos, búsqueda, embeddings ni logs de depuración.

El aprendizaje propuesto es procedimental y externo a los pesos. No afirmar que guardar memorias entrena el modelo. Exportar recuerdos y procedimientos con su procedencia permite reemplazar embeddings, motores y modelos sin perder conocimiento. El ajuste fino de pesos, si se añade, pertenece a LAB y necesita datasets autorizados y evaluaciones separadas.


## 11. Planificación, subagentes, Consejo y ramificación

Conservar la orquestación y los especialistas existentes. Usar varios agentes cuando haya independencia real, aislamiento de contexto o revisión útil; no para representar una reunión. En hardware local, dos agentes concurrentes pueden ser más lentos que uno y provocar presión de memoria. Un router pequeño puede clasificar, pero no debe ser autoridad para conceder permisos ni para resolver por sí solo decisiones difíciles.

**PLAN-01 · Plan progresivo y cobertura de requisitos · P0.** Backend: Extraer requisitos sin aplanar encabezados; mapear pasos a criterios; revisar tras reconocimiento y evidencia nueva. Interfaz: Plan corto por defecto, expandible hasta cada obligación original. Aceptación: Un brief de muchas secciones conserva todas, incluidas las últimas; ninguna desaparece por top-k o límite fijo.

**PLAN-02 · Delegación con contrato y presupuesto · P1.** Backend: Objetivo acotado, entradas, salidas, herramientas y evidencias requeridas; profundidad y número de hijos limitados. Interfaz: Árbol de subtareas con estado, modelo, permisos, consumo y cancelar rama. Aceptación: Un hijo devuelve partial cuando no cumple su encargo; no se declara éxito por haber producido texto.

**PLAN-03 · Propiedad de recursos y reconciliación · P0.** Backend: Asignar dueño de archivos o copias aisladas; integrar cambios con verificación de versiones y conflictos. Interfaz: Ver qué agente modifica cada archivo; resolver conflictos antes de aplicar. Aceptación: Dos especialistas no pisan el mismo componente y un worker muerto libera sus leases de forma segura.

**PLAN-04 · Consejo con evidencia, no voto ciego · P1.** Backend: Opiniones iniciales independientes; comparar hipótesis y pruebas; único ejecutor autorizado para efectos. Interfaz: Mostrar desacuerdos y evidencias decisivas, no unanimidad teatral. Aceptación: Tres modelos que repiten la misma afirmación sin fuente no sustituyen una comprobación.

**PLAN-05 · Explorar alternativas sin contaminar el mundo real · P1.** Backend: Namespaces de simulación separados de estado real; ramas aisladas y presupuesto; fusión solo tras revisión. Interfaz: Etiquetas claras Simulado/Aplicado; comparar cambios antes de aceptar una rama. Aceptación: State Mirror y recuerdos confirmados no reciben resultados de una rama descartada.

**PLAN-06 · Finalización ambiciosa pero acotada · P1.** Backend: Completion Engine propone trabajo adicional con coste y relación al objetivo; no altera automáticamente el alcance. Interfaz: Separar solicitado, necesario para completarlo y mejora opcional. Aceptación: Arreglar un bug no deriva en refactorizar todo el proyecto ni añadir dependencias no aprobadas.


## 12. Verificación, evidencias y confianza

La verificación debe apoyarse primero en observaciones ejecutadas: pruebas, build, salidas, diferencias y criterios. Una revisión de otro modelo añade una perspectiva, pero comparte posibles errores y no certifica verdad. El store de recibos actual ya distingue evidencia guardada de resultado verificado; esa semántica debe mantenerse en cada nueva pantalla. [F06]

**VER-01 · Criterios con estados explícitos · P0.** Backend: Cada criterio queda demostrado, incumplido, no comprobado o no aplicable con razón; conservar evidencia y versión del resultado. Interfaz: Resumen final con alcance comprobado, limitaciones y enlaces; prohibir el check verde total si quedan criterios críticos sin probar. Aceptación: Una tarea con herramientas exitosas pero tests no ejecutados se presenta como no verificada.

**VER-02 · Verificadores deterministas y baseline · P0.** Backend: Descubrir comandos desde el proyecto; registrar fallos previos; ejecutar pruebas específicas y ampliar cuando cambia el riesgo. Interfaz: Panel separa regresión nueva, fallo preexistente y comprobación inconclusa. Aceptación: Un proyecto ya roto no atribuye todo al último cambio ni usa fallos antiguos para ignorar una regresión nueva.

**VER-03 · Revisión independiente estructurada · P1.** Backend: Revisor recibe objetivo, diff y evidencia, no la conclusión persuasiva del autor; hallazgos con severidad y reproducción. Interfaz: Aceptar/rechazar hallazgos y volver al fragmento; límite visible a rondas de reparación. Aceptación: Un revisor sin pruebas no bloquea indefinidamente por preferencias de estilo ni altera requisitos originales.

**VER-04 · Prueba de resultado material · P0.** Backend: Leer después de escribir, comprobar artefacto y snapshot exacto; no asumir que HTTP 200 o exit_code 0 demuestra toda la tarea. Interfaz: Distinguir Generado, Guardado, Abre correctamente, Revisado y Verificado. Aceptación: Un DOCX corrupto, descarga incompleta o export vacío falla antes de mostrar un enlace de éxito.

**VER-05 · Provenance y citas comprobables · P0.** Backend: Verificar que cada cita resuelve a una fuente obtenida; evaluar apoyo semántico aparte de la mera existencia del marcador. Interfaz: Abrir extracto original y mostrar fuente inaccesible o respaldo dudoso. Aceptación: Un informe con muchos marcadores pero referencias que no apoyan la frase no obtiene etiqueta de investigación verificada.

**VER-06 · Aprobación humana no es prueba automática · P0.** Backend: Mantener estado de revisión del usuario separado de tests/veredictos automáticos; firma temporal del diff aprobado. Interfaz: Aceptar cambios y Verificado son acciones/etiquetas distintas. Aceptación: Aceptar manualmente no convierte tests fallidos en exitosos; cambiar el diff invalida aprobación de la versión anterior.


## 13. Edición transaccional, checkpoints y recuperación

Conservar los checkpoints y el Git sombra existentes. Extender su cobertura de forma explícita, sin presentarlos como una máquina del tiempo universal. Un commit o snapshot no puede deshacer un correo enviado, una publicación remota ni cualquier efecto de un comando arbitrario. La documentación de Claude Code reconoce límites de cobertura; Cline también documenta costes de almacenamiento y rendimiento en proyectos grandes. Faustus debe hacer visible qué protege realmente cada punto de recuperación. [F02, S04, S13]

**EDIT-01 · Edición sobre versión conocida · P0.** Backend: Cada patch lleva hash/base_revision; validar anclas únicas y diff antes de aplicar; escrituras atómicas donde el sistema de archivos lo permita. Interfaz: Mostrar conflicto con edición del usuario y comparar base, propuesta y actual, sin sobrescribir en silencio. Aceptación: Modificar el archivo desde un editor externo entre lectura y escritura provoca conflicto recuperable, no pérdida de trabajo.

**EDIT-02 · Lotes multiarquivo recuperables · P0.** Backend: Preparar y validar todo el lote; journal de aplicación y compensación si no hay atomicidad real entre archivos; checkpoints antes de cambios de riesgo. Interfaz: Previsualizar el lote completo, excluir cambios opcionales y explicar dependencias entre archivos. Aceptación: Un fallo en el tercer archivo conserva evidencia y permite restaurar lo aplicado; nunca anuncia atomicidad que el filesystem no ofrece.

**EDIT-03 · Preservación del contenido · P0.** Backend: Mantener encoding, BOM, finales de línea, permisos y formatos; evitar reescrituras completas y formateos ajenos al alcance. Interfaz: Diff legible que distingue cambios funcionales de formato; vista de binarios y archivos grandes. Aceptación: Editar una línea en un fichero CRLF o UTF-8 con BOM no reescribe miles de líneas ni destruye caracteres.

**EDIT-04 · Cobertura real del checkpoint · P0.** Backend: Registrar raíces, exclusiones, binarios, tamaño, versiones y efectos no restaurables; observar cambios por shell y subagentes cuando sea viable. Interfaz: Panel Qué se restaurará / Qué no; preview obligatorio si hay cambios posteriores del usuario. Aceptación: Restaurar no borra cambios ajenos sin confirmación ni afirma revertir operaciones externas.

**EDIT-05 · Historial y limpieza segura · P1.** Backend: Retención por tamaño, edad y puntos fijados; cuotas para snapshots y deduplicación; limpieza transaccional y referencias protegidas. Interfaz: Línea temporal con etiquetas, ramas, comparación y espacio recuperable antes de purgar. Aceptación: GC no elimina objetos referidos por un checkpoint fijado, un informe guardado o una tarea pausada.

**EDIT-06 · Refactorización asistida por estructura · P1.** Backend: Preferir operaciones AST/LSP para renombrados soportados; verificar referencias y tests, con fallback a patch revisable. Interfaz: Vista de impacto con símbolos, llamadas y pruebas relevantes. Aceptación: Renombrar una función no cambia cadenas y documentación no relacionadas por sustitución global accidental.


## 14. Shell, procesos, entornos y ejecución local

El ejecutor debe reconocer la diferencia entre anfitrión, contenedor, WSL y nodo remoto. El shell es una herramienta poderosa, pero no debe convertirse en un bypass de todas las capacidades tipadas. El incidente documentado de ProgramData/ALLUSERSPROFILE ausentes al arrancar Docker mediante el puente MCP se convierte en una prueba de entorno, no en otra instrucción de prompt. [F04]

**EXEC-01 · Entorno explícito y reproducible · P0.** Backend: Cada ejecución fija cwd, shell, host, usuario efectivo y lista segura de variables; diagnóstico de dependencias y rutas. Interfaz: Mostrar dónde se ejecutará: Windows, WSL, contenedor o remoto; abrir terminal equivalente para inspección. Aceptación: Una ruta D:\Proyecto no se entrega literalmente a un shell Linux; faltas de entorno se reportan antes de acciones dependientes.

**EXEC-02 · Comandos y argumentos seguros · P0.** Backend: Usar argv en herramientas tipadas, escapar solo al entrar en un shell real; no confiar en listas negras de palabras para contener código arbitrario. Interfaz: Preview del comando efectivo y su ámbito; secretos enmascarados. Aceptación: Espacios, comillas, Unicode, globbing y rutas con & no cambian el significado de un argumento ni habilitan otro comando.

**EXEC-03 · Procesos controlables · P0.** Backend: Identidad de proceso con tiempo de inicio y árbol de hijos; límites, captura incremental de salida, manejo de prompts y terminación escalonada. Interfaz: Terminal acoplable, buscar/copiar logs, enviar entrada explícita, detener un proceso o toda la tarea. Aceptación: Cancelar mata solo el árbol propiedad de Faustus; no todos los Python, Node, Docker u Ollama de la máquina.

**EXEC-04 · Construcción y pruebas con presupuesto · P0.** Backend: Cola de jobs pesados, límites de memoria/concurrencia y prioridades; no lanzar builds, tests y modelos grandes sin admisión conjunta. Interfaz: Avisar de conflictos entre tareas y ofrecer ejecutar secuencialmente. Aceptación: Una research local no compite con varias suites de tests y otro modelo hasta agotar el commit de Windows.

**EXEC-05 · Instalar dependencias con control · P1.** Backend: Detectar gestores/lockfiles; planificar cambios y descarga; aprobación para red, postinstall y elevación; preferir entorno aislado. Interfaz: Mostrar paquete, origen, versión, motivo y archivos que cambiarán; no pedir aprobación repetida para el mismo plan inalterado. Aceptación: Un paquete sugerido por texto no confiable no se instala sin validar origen y permiso; no modificar globalmente el sistema por defecto.

**EXEC-06 · Scripts reutilizables y ejecución remota · P1.** Backend: Recetas versionadas con entradas tipadas y salida verificable; SSH con fingerprint validado, secretos fuera del prompt y límites por destino. Interfaz: Perfil de ejecución reutilizable y prueba de conexión no destructiva; advertencia al cambiar host. Aceptación: Un hostname similar no hereda confianza de otro; una receta no puede cambiar silenciosamente de equipo.


## 15. Web, navegador y control del escritorio

Separar tres capacidades: obtener información web, interactuar con páginas y operar una aplicación de escritorio. Usar APIs autorizadas cuando existan, DOM/accesibilidad para acciones web y visión/coordenadas solo cuando haga falta. Un navegador puede ayudar a un modelo sin visión; no convierte un modelo exclusivamente textual en un modelo visual. La captura, la página y el elemento deben tener versiones y procedencia.

**WEB-01 · Búsqueda con salud y diversidad · P0.** Backend: Consultar motores configurados con cuotas, deduplicación y diagnósticos; preservar errores, fuente y fecha; no mezclar cero resultados con timeout. Interfaz: Estado de motores, filtro de fecha/dominio y aviso de cobertura parcial; reusar Salud de la búsqueda. Aceptación: Si solo responde un motor, el informe lo dice; si todos fallan no inventa resultados ni acaba como éxito.

**WEB-02 · Lectura web acotada y segura · P0.** Backend: Fetch con límites de bytes/tiempo, MIME y redirecciones; bloquear SSRF incluyendo IP privada tras resolver DNS; extracción conserva URL y fragmentos. Interfaz: Vista original/extracto, advertencias de acceso, páginas duplicadas y contenido desactualizado. Aceptación: Una URL pública que redirige al metadata endpoint o localhost se bloquea salvo conector local explícitamente autorizado.

**WEB-03 · Sesiones de navegador aisladas · P0.** Backend: Perfiles por propietario/proyecto; cookies cifradas o gestionadas por navegador; sesiones efímeras opcionales y permisos por origen. Interfaz: Ver navegador activo y tomar el control; login, CAPTCHA y segundo factor a cargo del usuario. Aceptación: Un proyecto no ve la sesión autenticada de otro; cerrar una tarea no borra la sesión del navegador personal del usuario.

**WEB-04 · Acciones con precondición y readback · P0.** Backend: Resolver elemento sobre snapshot vigente; comprobar URL, accesibilidad y estado antes de clic/type/submit; validar resultado posterior. Interfaz: Resaltar elemento objetivo y estado antes/después; aprobaciones contextualizadas para envíos o cambios relevantes. Aceptación: Un layout desplazado no produce un clic ciego en Eliminar; una navegación invalida coordenadas y selectores obsoletos.

**WEB-05 · Capturas como evidencia accionable · P1.** Backend: Guardar resolución, escala, viewport, timestamp y página; punto de edición es una referencia visual, no un enlace inventado a una línea de código. Interfaz: Anotar recuadro/punto, adjuntarlo sin enviar y regresar al proyecto o elemento observado. Aceptación: Una captura anterior nunca se usa como mapa exacto de una página nueva sin volver a inspeccionarla.

**WEB-06 · Navegación y extracción complejas · P1.** Backend: Paginación, descargas y formularios con límites; adjuntos a sandbox, comprobar hash/tamaño; no evadir accesos restringidos. Interfaz: Progreso por página/descarga y posibilidad de seleccionar campos; revisar destino y archivo antes de subirlo. Aceptación: Un resultado descargado a medias no aparece como completo ni una selección de archivo expone toda la carpeta.

**DESK-01 · Control visible del escritorio · P2.** Backend: Puente por OS con permisos específicos para captura, accesibilidad, teclado y ratón; lista de apps/ventanas permitidas, parada inmediata. Interfaz: Indicador permanente de control, vista de acción próxima y atajo de emergencia no dependiente del modelo. Aceptación: Mover el foco a una app no autorizada pausa la automatización; no escribir contraseñas ni activar cámara/micrófono por inferencia.

**DESK-02 · Verificación de interfaces · P1.** Backend: Ejecutar journeys en navegador de pruebas: DOM, consola, red, screenshots y accesibilidad; fixtures controladas antes de la instancia real. Interfaz: Resultado reproducible con paso fallido y captura; no dar por comprobada una UI solo por compilar TypeScript. Aceptación: El caso de botones [object Object] falla en test de render/journey aunque el backend devuelva HTTP 200.


## 16. Investigación con cobertura, no solo generación larga

Reutilizar el motor de research y sus correcciones. El requisito no es escribir más palabras, sino contestar el encargo completo con fuentes rastreables, diferencias de evidencia y limitaciones claras. Una investigación interrumpida debe conservar los resultados confirmados; generar un informe desde cero otra vez es Reintentar, no Reanudar. [F04]

**RES-01 · Brief jerárquico sin recorte silencioso · P0.** Backend: Parsear encabezados, subpreguntas, tablas solicitadas y restricciones en un árbol; asignar cobertura y evidencias por nodo sin cortar por número arbitrario. Interfaz: Esquema editable con pendientes, cubiertos e insuficientemente documentados. Aceptación: Un encargo con 44 viñetas conserva todas y produce un mapa de cobertura, aunque el informe se redacte en partes.

**RES-02 · Búsqueda iterativa con parada razonada · P1.** Backend: Planificar consultas variadas; medir novedad, duplicados y cobertura; parar por presupuesto, saturación o falta de acceso, no solo por vueltas. Interfaz: Mostrar qué falta investigar y por qué se detiene; permitir añadir fuentes o acotar un apartado. Aceptación: Diez URLs repetidas no cuentan como diez fuentes nuevas; repetir una consulta sin aportar evidencia dispara cambio de estrategia.

**RES-03 · Registro de fuentes y conflictos · P0.** Backend: Guardar autor/organismo, título, URL/DOI cuando exista, fecha de publicación y consulta, extractos, versión y estado de acceso; distinguir primarias y secundarias. Interfaz: Biblioteca de fuentes agrupable; marcar fuentes débiles, desactualizadas o contradictorias sin ocultarlas. Aceptación: Una opinión no se etiqueta como ensayo o consenso; dos fuentes que discrepan aparecen en el análisis.

**RES-04 · Redacción segmentada consistente · P0.** Backend: Redactar por secciones con IDs estables de evidencia, glosario y esquema compartido; ensamblar y comprobar huecos, repeticiones y referencias rotas. Interfaz: Vista del informe vivo con secciones completas/pendientes, sin perder la última versión válida. Aceptación: Agotar salida en una sección no elimina el resto del encargo ni sobrescribe un informe útil con texto vacío.

**RES-05 · Recuperación real por etapas · P0.** Backend: Checkpoint de consultas, páginas obtenidas, extractos, síntesis y secciones confirmadas; revalidar fuentes cuando cambian. Interfaz: Reanudar desde última etapa o empezar una nueva ejecución; elegir sin confundir ambas acciones. Aceptación: Matar y reiniciar el backend tras tres rondas conserva sus evidencias y no vuelve a gastar toda la inferencia en ellas.

**RES-06 · Calidad de evidencia configurable · P1.** Backend: Perfiles general, técnico, académico y clínico con fuentes y verificaciones apropiadas; separar grado de respaldo de simple número de citas. Interfaz: Etiquetas de incertidumbre y tabla de afirmaciones con su apoyo; export de bibliografía cuando hay metadatos. Aceptación: El porcentaje de frases citadas nunca se usa por sí solo para declarar correcto un informe clínico o técnico.


## 17. Documentos, datos y artefactos de primera clase

La app debe producir entregables utilizables, no solo texto que el usuario tenga que transformar manualmente. Cada artefacto tiene ID, propietario, tipo, versiones, entradas, generador, modelo, receta y estado de validación. Los motores especializados pueden ser opcionales, pero la UI debe saber si están instalados y qué pueden hacer.

**ART-01 · Pipeline de artefactos durable · P0.** Backend: Estados preparando/generando/validando/listo/parcial/fallido; escritura temporal, commit y checksum; manifest con procedencia y límites. Interfaz: Previsualización, abrir en app externa, revelar en carpeta, copiar referencia y descarga; no mostrar listo antes de finalizar. Aceptación: Cerrar el chat no pierde una exportación; una descarga interrumpida se reintenta sin crear archivos corruptos o duplicados.

**ART-02 · Edición de documentos real · P1.** Backend: Leer y modificar DOCX/ODT/Markdown/HTML con estructura, estilos, tablas, referencias y preservación de contenido no tocado; capacidades por formato. Interfaz: Editor documento y modo cambios; advertir elementos no preservables antes de convertir. Aceptación: Una edición de un párrafo no elimina comentarios, imágenes o numeración sin mostrarlo y permitir cancelación.

**ART-03 · Exportar y revisar visualmente · P1.** Backend: Export DOCX/PDF/HTML/Markdown con plantillas versionadas, TOC, fuentes locales y validación de apertura; render para inspección de páginas. Interfaz: Vista de páginas con cortes, tablas y alertas de desbordamiento; descargar original editable y versión de lectura. Aceptación: Un documento que abre pero tiene texto fuera de página o tablas cortadas no pasa control visual automáticamente.

**ART-04 · Hojas de cálculo fiables · P1.** Backend: Importar CSV/TSV/XLSX preservando tipos, fechas, fórmulas y formatos; operaciones sobre rangos, validación y motor de cálculo declarado. Interfaz: Tabla editable con preview de cambios, fórmulas visibles y unidades; aviso cuando resultados cacheados no se han recalculado. Aceptación: No convertir IDs a notación científica ni fechas ambiguas sin revisión; bloquear inyección de fórmulas al exportar texto no confiable.

**ART-05 · Datos y análisis reproducibles · P1.** Backend: Python/SQL aislado con dataset versionado y scripts guardados; conexiones DB solo lectura por defecto, timeout y límite de filas. Interfaz: Notebook ligero o panel de consultas con muestra, esquema y gráfico; ver cómo se obtuvo cada resultado. Aceptación: Una afirmación numérica enlaza a datos y cálculo; una consulta no autorizada de escritura se rechaza aunque el modelo la llame análisis.

**ART-06 · Presentaciones y gráficos · P2.** Backend: Generar diapositivas editables y gráficos con datos fuente, notas y validación visual; diseño consistente, no una captura por slide. Interfaz: Vista de diapositivas, selección de layout y ajuste de densidad; accesibilidad con textos alternativos. Aceptación: Los gráficos conservan ejes, unidades y datos; exportar no aplana todo a imágenes salvo elección explícita.

**ART-07 · PDFs y archivos complejos · P1.** Backend: Extraer texto/tablas/figuras con referencias de página; OCR opcional solo cuando haga falta; conversiones con presupuesto de tamaño y páginas. Interfaz: Seleccionar páginas, comparar extracción y original; advertencias cuando la capa de texto no coincide visualmente. Aceptación: Un PDF escaneado no se clasifica como vacío; OCR fallido se declara, sin inventar contenido.

**ART-08 · Biblioteca universal de resultados · P1.** Backend: Índice de artefactos por proyecto, conversación, tipo y versión; deduplicar bytes sin mezclar permisos ni procedencia. Interfaz: Galería/lista, búsqueda, etiquetas, favoritos, recientes, relaciones fuente→resultado y versiones. Aceptación: Borrar un enlace de contexto no borra el archivo original compartido con otro entregable sin explicarlo.


## 18. Escritura, creación y trabajo con conocimiento personal

Faustus no debe optimizarse exclusivamente para programación. En escritura, el objetivo suele ser preservar voz, intención, continuidad y control editorial; un texto distinto no es automáticamente mejor. Integrarse con Writer’s Hoard mediante adaptadores/MCP y formatos estables es preferible a copiar toda esa aplicación dentro de Faustus. Esa integración es una propuesta, no una capacidad comprobada aquí.

**WRITE-01 · Edición localizada con intención · P1.** Backend: Separar corregir, reescribir, resumir, desarrollar y criticar; conservar fragmentos protegidos y registrar cambios de significado. Interfaz: Seleccionar un párrafo y pedir variantes lado a lado, aceptar por cambio y deshacer sin perder el original. Aceptación: Una corrección ortográfica no cambia la voz del narrador ni elimina una ironía deliberada por normalización automática.

**WRITE-02 · Canon y continuidad · P1.** Backend: Memoria de personajes, lugares, cronología, relaciones y hechos con fuente/capítulo; distinguir canon, borrador y alternativa. Interfaz: Panel de continuidad con contradicciones y referencias, no reescritura automática de la historia. Aceptación: Un final alternativo descartado no pasa a ser canon del siguiente capítulo.

**WRITE-03 · Estilo editable y no invasivo · P1.** Backend: Reusar aprendizaje de estilo existente; versionar reglas con ejemplos, prohibiciones y ámbito; evaluación A/B sobre texto de prueba. Interfaz: Activar estilo por proyecto o encargo, ajustar intensidad y ver reglas efectivas. Aceptación: Una regla de novela no contamina correos profesionales; desactivar estilo realmente lo retira del contexto siguiente.

**WRITE-04 · Sesiones largas y alternativas · P1.** Backend: Esquemas y capítulos con referencias estables, objetivos de escena y resúmenes con fuente; ramas editoriales independientes. Interfaz: Modo foco, objetivos de extensión flexibles, notas al margen y comparación de versiones. Aceptación: Editar una escena no obliga a reenviar el manuscrito completo ni pierde notas al cambiar de conversación.

**WRITE-05 · Integración sin duplicación · P2.** Backend: Adaptador de proyecto para importar/exportar manifiestos, capítulos, entidades y recursos de Writer’s Hoard; resolver conflictos y versiones. Interfaz: Abrir el mismo artefacto en su aplicación de origen y mostrar qué lado tiene cambios nuevos. Aceptación: Una sincronización bidireccional no sobrescribe ediciones concurrentes ni requiere que el proyecto sea un repositorio Git.


## 19. Imágenes, vídeo, audio y voz

Reusar los motores y recetas multimedia existentes. No confundir soporte de adjuntos con percepción del modelo, ni una referencia de estilo escrita con condicionamiento real por imagen. Separar el LLM que interpreta el encargo del motor que genera o transforma medios, y conservar el vínculo entre ambos. [F02]

**MEDIA-01 · Capacidades multimodales comprobadas · P0.** Backend: Detectar tipos de entrada/salida, límites, encoder y motor requerido; rechazar o adaptar con consentimiento cuando falta visión/audio. Interfaz: El selector explica qué verá realmente el modelo: imagen, transcripción o descripción derivada. Aceptación: Enviar una imagen a un modelo textual no produce una respuesta que finja haberla visto; se ofrece un flujo compatible.

**MEDIA-02 · Recetas versionadas y verificadas · P1.** Backend: Mantener workflows ComfyUI aprobados con hash, nodos requeridos, licencias y entradas tipadas; preflight antes de cargar modelos. Interfaz: Formulario de parámetros, preview del coste de recursos y explicación de modelos faltantes sin descarga automática. Aceptación: Una receta actualizada que introduce nodos ejecutables nuevos vuelve a requerir revisión.

**MEDIA-03 · Edición no destructiva de imágenes · P1.** Backend: Capas, máscaras y referencia original; guardar proyecto de edición además de resultado plano; coordenadas y color/alpha preservados. Interfaz: Inpainting/outpainting, recorte, transformación y comparación antes/después; enviar al chat solo por acción explícita. Aceptación: Salir del editor conserva máscaras y borrador; un resultado PNG no sustituye el original sin confirmación.

**MEDIA-04 · Colas de render y recuperación · P1.** Backend: Jobs con IDs externos, estados y reconciliación; descargar resultados con integridad; no duplicar render tras perder una conexión. Interfaz: Galería con progreso real, fallos y reintento por descarga o por generación claramente distintos. Aceptación: Un timeout después de aceptar el job consulta su estado antes de crear otro que consume VRAM.

**MEDIA-05 · Audio y vídeo editables · P2.** Backend: Transcripción con timestamps, separación de pistas, cortes, subtítulos y export SRT/VTT; transformación con FFmpeg acotada. Interfaz: Línea temporal, corregir transcripción, preescucha y seleccionar pista; límites del motor visibles. Aceptación: Cambiar subtítulos no reemplaza accidentalmente el audio original; una exportación incompleta conserva el material fuente.

**VOICE-01 · Voz útil con control del usuario · P1.** Backend: Estados permiso/escuchando/transcribiendo/pensando/hablando; push-to-talk por defecto, detección de silencio y cancelación. Interfaz: Indicador permanente del micrófono, transcripción editable y detener respuesta hablada sin cancelar todo el chat. Aceptación: No solicitar permisos ni grabar al abrir una pantalla; retirar permiso detiene captura inmediatamente.

**VOICE-02 · Interrupción y confirmación por voz · P2.** Backend: Barge-in con separación de audio generado y capturado, evitar autoescucha; confirmar efectos sensibles por canal visual o mecanismo inequívoco. Interfaz: Elegir voz, velocidad y dispositivo; fallback textual si no hay micrófono, TTS o permisos. Aceptación: La propia voz de Faustus no dispara otra orden; transcripción dudosa de Borrar no inicia una operación destructiva.

**MEDIA-06 · Procedencia y consentimiento · P1.** Backend: Guardar modelo/versión, licencia conocida, semilla, receta y hashes; identificar contenidos sintéticos cuando corresponda; minimizar metadatos sensibles al compartir. Interfaz: Ficha de procedencia y aviso de datos que se envían a motores remotos; borrar ubicación EXIF opcional. Aceptación: Una imagen privada no sale a un proveedor remoto por fallback silencioso de un motor local.


## 20. Conectores, comunicaciones y automatizaciones

El catálogo debe admitir correo, calendarios, contactos, nubes de archivos, repositorios, tickets, notas, bases de datos y servicios domésticos autorizados. No son permisos globales: una conexión disponible no significa que cualquier modelo pueda leerla o escribir en ella. Reusar IMAP/SMTP, CalDAV y el scheduler documentados, ampliando contratos y experiencias, no creando un segundo sistema. [F02]

**CONN-01 · Centro de conexiones con ámbitos · P1.** Backend: Credenciales en vault del sistema o almacén cifrado, tokens limitados, rotación/revocación y aislamiento por propietario. Interfaz: Conectar, probar, ver permisos efectivos y desconectar; mostrar solo capacidades realmente disponibles. Aceptación: Desconectar revoca acceso operativo y evita que un worker reutilice credenciales cacheadas fuera de política.

**CONN-02 · Leer, preparar y ejecutar separados · P0.** Backend: Herramientas de búsqueda/lectura distintas de redactar, guardar borrador, enviar y borrar; plan de efecto con destinatarios y payload exacto. Interfaz: Revisar destinatarios, adjuntos, calendario/zona horaria y alcance antes de confirmar; recordar aprobación solo dentro de límites fijados. Aceptación: Generar un correo no lo envía; aprobar un borrador no autoriza cambiar destinatario o adjuntar otro archivo.

**CONN-03 · Identidades y nombres ambiguos · P0.** Backend: Resolver personas, proyectos y recursos con IDs estables y datos visibles; no escoger al azar entre coincidencias. Interfaz: Tarjetas de selección con nombre, dirección/dominio y origen; opción de introducir uno nuevo explícitamente. Aceptación: Dos contactos llamados Ana no reciben un envío hasta resolver quién es la destinataria.

**CONN-04 · Sincronización y conflictos · P1.** Backend: ETag/revisión/updated_at, webhooks verificados o polling incremental; tokens de sincronía por ámbito y reconciliación tras desconexión. Interfaz: Indicar cache local, última sincronización y cambios remotos; resolver conflictos antes de sobrescribir. Aceptación: Una nota modificada en el móvil no se pisa con el borrador local antiguo tras volver la red.

**AUTO-01 · Scheduler durable y correcto en el tiempo · P1.** Backend: Recurrencias con IANA, DST, política de misfire y zona por tarea; jobs persistentes con lease y deduplicación por ocurrencia. Interfaz: Calendario de próximas ejecuciones, pausar/reanudar, ejecutar ahora y editar una o todas; indicar que requiere equipo/backend disponible. Aceptación: Cambio horario Europe/Madrid no duplica un envío ni pierde silenciosamente una tarea; al despertar se aplica la política elegida.

**AUTO-02 · Automatización con presupuesto y permisos · P0.** Backend: Snapshot de permisos, recursos y conectores para ejecución desatendida; acciones no autorizadas pasan a esperando aprobación o bloqueada. Interfaz: Bandeja de trabajos que necesitan intervención; política explícita para ausencia del usuario. Aceptación: Caducar una pregunta nunca equivale a aprobar un envío, borrar archivos o habilitar una API de pago.

**AUTO-03 · Flujos visibles y depurables · P2.** Backend: DAG de pasos con entradas/salidas, condiciones, reintentos, cron/webhook y simulación; secretos por referencia, no incrustados. Interfaz: Editor simple de recetas y vista avanzada del flujo; probar con datos sintéticos sin ejecutar efectos reales. Aceptación: Reejecutar un paso de extracción no repite automáticamente el envío que ya se confirmó en otro paso.


## 21. Seguridad, privacidad y límites de confianza

Amenazas prioritarias: instrucciones maliciosas en páginas/documentos/repositorios, tool poisoning en extensiones, exfiltración por conectores, ejecución fuera del workspace, secretos en logs, confusión entre propietarios y reintentos que duplican efectos. La política debe estar en el broker y el ejecutor, no en un mensaje al modelo. Las recomendaciones oficiales de MCP y Electron son referencias para implementar y revisar estas fronteras. [S20, S21]

**SEC-01 · Autorización independiente del modelo · P0.** Backend: Evaluar usuario, proyecto, recurso, operación, riesgo, origen y permiso vigente; denegar por defecto lo no declarado. Interfaz: Permitir una vez, durante esta tarea o por política acotada; vista de concesiones activas y revocación inmediata. Aceptación: Un modelo o documento que diga tengo permiso no obtiene acceso fuera de la política.

**SEC-02 · Datos no confiables no son instrucciones · P0.** Backend: Marcar procedencia de resultados externos; impedir que documentos redefinan herramientas, destinos, secretos o reglas del sistema. Interfaz: Advertir instrucciones sospechosas con contexto, sin convertir cada página normal en una alarma. Aceptación: Un README que pide leer claves privadas y enviarlas a un dominio no causa ninguna acción, aunque la tarea sea construir el proyecto.

**SEC-03 · Sandbox real y rutas seguras · P0.** Backend: Resolver canonical paths, symlinks/junctions y TOCTOU; restricciones OS/contenedor para código arbitrario, no solo checks en Python. Interfaz: Ver carpetas permitidas y distinguir Lectura/Escritura/Ejecución; advertir cuando no hay aislamiento fuerte disponible. Aceptación: Un symlink dentro del proyecto no da acceso a una carpeta externa; un script no puede saltarse una sandbox anunciada como activa.

**SEC-04 · Red y egress controlados · P0.** Backend: Lista de destinos por herramienta/conector; bloquear SSRF, metadata y endpoints internos no autorizados; redirecciones y DNS revalidados. Interfaz: Ver qué servicio recibe datos; perfil totalmente local que bloquea la salida de inferencia y servicios auxiliares remotos. Aceptación: Local-only cubre embeddings, OCR, reranker, telemetría y fallback, no solo la llamada al LLM principal.

**SEC-05 · Secretos y datos sensibles · P0.** Backend: Redactar secretos en trazas; credenciales por referencia opaca y acceso efímero; no indexar .env, llaves o perfiles sensibles por defecto. Interfaz: Mostrar categorías excluidas y compartir un diagnóstico saneado con preview. Aceptación: Un crash dump o export de conversación no incluye API keys, cookies o tokens de acceso.

**SEC-06 · Aislamiento de propietarios y recursos · P0.** Backend: Authorization en cada API, búsqueda, índice, WebSocket/SSE y descarga; IDs opacos no sustituyen el control de acceso. Interfaz: Estado claro de usuario/proyecto; no mostrar resultados que luego sean rechazados por pertenecer a otro dueño. Aceptación: Pruebas IDOR entre dos propietarios cubren archivos, memorias, recibos, renders, preguntas, eventos y caches.

**SEC-07 · Electron y contenido activo · P0.** Backend: Context isolation, sandbox, nodeIntegration desactivado para contenido no confiable, CSP y puente IPC mínimo validado; navegación controlada. Interfaz: HTML/SVG/Markdown y previews externos en contenedor seguro; enlaces externos no ejecutan comandos locales. Aceptación: Un artefacto HTML malicioso no llama al filesystem ni al IPC privilegiado desde su preview.

**SEC-08 · Políticas proporcionales y auditables · P1.** Backend: Perfiles lectura, edición local, agente supervisado y automatización acotada; export de auditoría con eventos firmados cuando el despliegue lo requiera. Interfaz: Resumen comprensible de cambios de riesgo; evitar fatiga de confirmaciones para lecturas ya autorizadas. Aceptación: Una actualización de plugin con más permisos invalida el consentimiento anterior sin bloquear herramientas no afectadas.

Regla de honestidad: cifrado en reposo y hashes de integridad no protegen contra un administrador que controla el equipo y las claves. La seguridad se documenta por modelo de amenaza; no anunciar privacidad absoluta ni aislamiento de nivel servidor cuando no se ha implementado.


## 22. Hardware, carga de modelos y ejecución distribuida

Para la máquina documentada de Luis, la admisión debe considerar las tres GPUs, RAM, memoria de commit, contexto KV, Docker, builds, procesos de voz e imagen y modelos residentes. Los 12 + 16 + 16 GB físicos no forman una reserva única sin costes ni restricciones. Tampoco toda la memoria unificada de un Spark es libre para pesos. Las mejoras de red/distribución se validan en el backend y la topología exactos, no por sumar capacidades de una ficha. [F03, F04, S23, S24]

**HW-01 · Admisión global y por dispositivo · P0.** Backend: Extender vram_admission/vram_fit con reservas atómicas por job, VRAM por GPU, KV, buffers, RAM/commit y uso externo observado; estimaciones con intervalo y procedencia. Interfaz: Memoria libre/reservada/usada, peso estimado o medido y margen; diálogo de liberar modelos existente como autoridad única. Aceptación: Dos cargas simultáneas no reciben el mismo presupuesto libre; un modelo nuevo con KV desconocida se etiqueta como estimación mínima.

**HW-02 · Offload y degradación transparentes · P0.** Backend: Detectar carga CPU/GPU y spill cuando el backend lo expone; no reducir contexto o cuantización silenciosamente; confirmar tradeoff. Interfaz: Explicar lento por carga, prefill, spill, cola o red; seleccionar menor contexto, otro modelo o liberar recursos. Aceptación: El usuario no ve simplemente Esperando al modelo mientras se desborda a RAM durante minutos.

**HW-03 · Residencia y concurrencia inteligentes · P1.** Backend: Política LRU con pins y coste de recarga; cola por dispositivo; evitar precargas especulativas sin presupuesto; no cargar siempre tres modelos para roles simples. Interfaz: Fijar modelo residente y ver qué trabajo lo usa; modo solo un modelo grande y modo prioridad interactiva. Aceptación: Una conversación rápida no descarga un modelo en pleno trabajo ni vuelve a cargarlo constantemente por un router indeciso.

**HW-04 · Catálogo y descargas fiables · P1.** Backend: Metadatos por revisión/digest, licencia y cuantización; descargas reanudables con hash, espacio temporal y final, límites de red y eliminación segura. Interfaz: Progreso por bytes, pausar/reanudar, cancelar y limpiar parciales; comparar tamaño de descarga con huella de ejecución. Aceptación: Una descarga cortada retoma datos válidos; un cambio de tag no reutiliza un perfil calibrado de otro digest.

**HW-05 · Perfiles de hardware medidos · P1.** Backend: Benchmark opt-in por modelo/backend/topología: TTFT, prefill, decode, contexto, memoria y concurrencia; límites térmicos y de potencia si están disponibles. Interfaz: Perfiles trabajar/jugar/renderizar/local intensivo con reservas; advertir mediciones ausentes sin fabricar tokens/s. Aceptación: Elegir un modelo no lanza automáticamente pruebas pesadas que bloqueen el escritorio.

**HW-06 · Nodos remotos como servicio controlado · P2.** Backend: Registrar endpoint, identidad, versión, capacidades, seguridad y salud; tolerar desconexión y operaciones desconocidas; separar administración del nodo de inferencia. Interfaz: Mapa lógico del hardware, modelo servido y degradación; conexión segura sin exponer RPC arbitrario a Internet. Aceptación: Un nodo perdido no convierte una tarea en éxito ni deja recursos reservados para siempre.

**HW-07 · Spark + PC + eGPU: laboratorio · LAB.** Backend: Prototipo opt-in con backend compatible; medir reparto de capas, KV, enlaces, carga y latencia; no afirmar RDMA, pooling o failover transparente sin probarlos. Interfaz: Etiqueta Experimental y reporte de configuración reproducible; fallback a servicio independiente o trabajo secuencial. Aceptación: Solo se declara soportada una combinación que supera carga, generación, cancelación, caída de nodo y recuperación en esa configuración.

No reconstruir llama.cpp, Ollama o vLLM dentro de Faustus. La aplicación coordina y explica sus capacidades; la inferencia, cuantización y distribución pertenecen a adaptadores y motores especializados. Mantener la UI usable con una sola GPU y un solo modelo es un criterio de diseño tan importante como escalar.


## 23. Observabilidad, errores y diagnóstico

**OBS-01 · Eventos coherentes de extremo a extremo · P0.** Backend: Propagar task_id, run_id, step_id, call_id y trace_id; eventos secuenciales persistidos con versiones; separar logs de usuario y diagnósticos sensibles. Interfaz: Timeline único de acciones, evidencia, cambios y preguntas; filtrar sin perder contexto. Aceptación: Una herramienta, su progreso, el artefacto y el recibo final se pueden enlazar sin buscar manualmente por texto.

**OBS-02 · Fases reales y métricas honestas · P0.** Backend: Distinguir cola, admisión, carga, prefill, generación, herramienta, verificación y espera humana; heartbeat no equivale a avance. Interfaz: Tiempo transcurrido, fase y última actividad; porcentaje solo cuando hay total medible; estado Desconocido cuando falta telemetría. Aceptación: Un spinner con latidos no presenta 90 % inventado ni promete un tiempo sin base.

**OBS-03 · Taxonomía de errores accionable · P0.** Backend: Códigos estables para capacidad, schema, permiso, recurso, transporte, timeout, cancelación, conflicto, verificación y resultado desconocido; cause chain segura. Interfaz: Explicar qué falló, qué se conserva y acción útil: reintentar, reanudar, cambiar recurso, revisar permiso o abrir diagnóstico. Aceptación: OOM no se reporta como fallo de búsqueda; perder SSE no se interpreta inmediatamente como caída de la inferencia.

**OBS-04 · Reproducción y soporte local · P1.** Backend: Export de configuración efectiva, versiones, traza saneada y fixtures con consentimiento; modo diagnóstico sin contenido personal por defecto. Interfaz: Botón Copiar diagnóstico y paquete seleccionable; preview de datos antes de compartir. Aceptación: El desarrollador puede reproducir un parser fallido sin recibir conversaciones, archivos o credenciales no seleccionados.

**OBS-05 · Calidad y rendimiento por perfil · P1.** Backend: Métricas de éxito, intervención humana, loops, tiempo, recursos y coste externo vinculadas a modelo+harness; no comparar workloads distintos como iguales. Interfaz: Panel de tendencias y regresiones con muestras y limitaciones; restablecer telemetría local. Aceptación: Una mejora de tokens/s no se presenta como mejora de calidad si aumenta errores o tiempo total de la tarea.


## 24. Chat, compositor y comodidad cotidiana

Las funciones pequeñas merecen requisitos explícitos: son las que se repiten cientos de veces al día. Conservar los borradores por conversación, la navegación de mensajes y el workbench ya documentados. Cada control debe comportarse igual en streaming, historial restaurado y reconexión. [F02, F04]

**UX-01 · Borradores que no se pierden · P0.** Backend: Persistir borrador por conversación/nuevo chat con revisión, adjuntos y selecciones; límites y limpieza de datos; no sincronizar un secreto accidental sin permiso. Interfaz: Cambiar de chat, cerrar panel o abrir editor no vacía el texto; indicador Guardado localmente y recuperación tras crash. Aceptación: Recargar durante un borrador largo restaura texto, adjuntos preparados y ubicación lógica sin enviarlo.

**UX-02 · Envío sin duplicados ni pérdidas · P0.** Backend: client_message_id idempotente y outbox local hasta confirmación; adjuntos subidos antes de enviar; ordenar mensajes concurrentes. Interfaz: Estados preparado/enviando/enviado/error, deshacer antes de aceptar y reintentar sin copiar el texto otra vez. Aceptación: Doble clic o reconexión no crea dos tareas; Enter durante composición IME no envía antes de tiempo.

**UX-03 · Editar, regenerar y ramificar · P1.** Backend: Árbol de conversación con mensajes/versiones; regenerar crea alternativa, no borra historial ni repite herramientas externas por defecto. Interfaz: Comparar respuestas, navegar ramas, continuar desde un punto y distinguir Repetir texto de Reejecutar acciones. Aceptación: Regenerar una respuesta que informó de un correo enviado no envía ese correo de nuevo.

**UX-04 · Steering, cola y cancelación separados · P0.** Backend: Modelar instrucción para ejecución actual, mensaje en cola y nueva tarea; cancelación independiente de borrar conversación. Interfaz: Botones Dirigir tarea, Enviar después y Detener; texto de consecuencias sin menús ambiguos. Aceptación: Un mensaje añadido mientras trabaja llega al destino escogido y no altera inadvertidamente otra tarea.

**UX-05 · Lectura agradable durante streaming · P0.** Backend: Render incremental seguro, unir bloques sin romper Markdown; conservar selección, posición y anclas; no reconstruir todo el transcript por token. Interfaz: Auto-scroll solo cuando el lector está abajo; botón de nuevos mensajes, saltos entre usuario/respuesta/herramienta y búsqueda. Aceptación: Seleccionar/copiar un párrafo anterior no hace saltar al final ni pierde la selección al llegar tokens.

**UX-06 · Compositor rico pero rápido · P1.** Backend: Adjuntos con MIME, tamaños, dedupe y estado; menciones de archivos/símbolos/fuentes/skills; comandos descubiertos y parámetros válidos. Interfaz: Pegar capturas, arrastrar archivos, preview y quitar; @ selector y / comandos; templates personales sin enviar automáticamente. Aceptación: Pegar un archivo grande no congela la UI; un adjunto incompatible muestra la razón antes de gastar inferencia.

**UX-07 · Control de contexto por turno · P1.** Backend: Seleccionar memoria, proyecto, archivos y skills sin modificar globales; guardar manifest de contexto realmente usado. Interfaz: Chips visibles y panel qué se enviará, especialmente en remoto; pin/excluir fuentes y ver el presupuesto. Aceptación: Excluir un archivo impide su recuperación automática en ese turno, incluidos resúmenes o caches derivados que aún lo contengan.

**UX-08 · Respuestas y errores útiles · P0.** Backend: Estructurar resultado final, fuentes, artefactos y pendientes desde evidencia; no ocultar advertencias al terminar. Interfaz: Copiar respuesta o código, abrir fuentes, descargar archivos y recuperar mensajes fallidos; errores inline persistentes además de notificaciones. Aceptación: El usuario puede leer la causa y reanudar aunque haya desaparecido el toast de error.

**UX-09 · Acciones personales sin fricción · P1.** Backend: Persistir favoritos, pins, tags, carpetas, archivo, búsqueda global y preferencias por proyecto; operaciones masivas reversibles cuando sea posible. Interfaz: Renombrar, mover, duplicar, archivar, exportar y restaurar chats; mostrar fecha local y modelo realmente usado por turno. Aceptación: Mover un chat no cambia en silencio los permisos o memorias aplicados a sus mensajes históricos.

**UX-10 · Prompts y conversaciones reutilizables · P2.** Backend: Biblioteca de plantillas con variables tipadas, versiones, ejemplos y ámbito; importar/exportar sin credenciales ni referencias rotas. Interfaz: Guardar selección como plantilla, favoritos, comparaciones de variantes y búsqueda por intención. Aceptación: Una plantilla con ruta antigua solicita resolverla en el proyecto nuevo en lugar de escribir en el anterior.


## 25. Workbench: archivos, evidencias, navegador y editor

**BENCH-01 · Espacio de trabajo persistente · P1.** Backend: Estado de paneles, pestañas, dimensiones y artefacto activo por proyecto; IDs estables independientes de navegación. Interfaz: Chat junto a editor/diff/fuentes/browser/actividad; dividir, acoplar y volver al layout anterior. Aceptación: Abrir una fuente o imagen no cierra el diff con cambios sin guardar ni pierde el borrador.

**BENCH-02 · Revisión de cambios de verdad · P0.** Backend: Diff por hunk y archivo con relaciones, base y versión actual; decisiones humanas persistidas; detectar cambios después de revisar. Interfaz: Aceptar/rechazar total o parcial con explicación de dependencias; abrir archivo en editor externo y checkpoint de retorno. Aceptación: Aceptar un hunk no aplica otro oculto; una edición externa invalida solo las aprobaciones afectadas.

**BENCH-03 · Inspector de evidencia · P1.** Backend: Resolver EvidenceRef a bytes/versión/rango exactos; indicar obsolescencia o ausencia sin sustituir la fuente silenciosamente. Interfaz: Cita → extracto → fuente original; comparar evidencia capturada con estado actual. Aceptación: Si un archivo cambió desde la verificación, el inspector no muestra su versión nueva como si fuera la prueba original.

**BENCH-04 · Editor con conflictos y guardado fiable · P1.** Backend: Autoguardado versionado, detección de conflictos, archivos grandes paginados y rutas autorizadas; no mezclar estado de vista con contenido. Interfaz: Buscar/reemplazar, ir a línea/símbolo, vista previa y undo/redo local; avisar antes de cerrar cambios no guardados. Aceptación: Reabrir el mismo archivo desde otra pestaña comparte estado o resuelve conflicto, no crea dos versiones que se pisan.

**BENCH-05 · Plan, cambios y actividad legibles · P1.** Backend: Proyectar un único TaskState en plan y progreso; agrupar llamadas relacionadas, child runs y evidencias sin duplicar contadores. Interfaz: Resumen siempre visible: objetivo, paso, bloqueo y siguiente acción; detalles técnicos expandibles. Aceptación: El plan no dice terminado mientras la verificación está en curso ni vuelve atrás por un evento antiguo del stream.

**BENCH-06 · Canvas y previews seguros · P2.** Backend: Previews aislados para HTML, gráficos, diagramas y pequeños prototipos; capacidades explícitas de red/ejecución. Interfaz: Editar artefacto y ver cambios, consola aislada, compartir export estático y abrir fuentes. Aceptación: Un prototipo puede fallar sin bloquear el chat ni acceder al entorno privilegiado de Electron.


## 26. Modelos, ajustes, onboarding y diagnóstico visual

**SET-01 · Primera ejecución sin callejones · P0.** Backend: Detectar backend, directorios, permisos, puertos y dependencias con pruebas no destructivas; configuración inicial incremental. Interfaz: Recorrido mínimo: abrir proyecto, conectar modelo, probar chat y una herramienta segura; saltar módulos no necesarios. Aceptación: Un usuario con Ollama operativo llega a una conversación sin instalar ComfyUI, Docker, voz ni bases externas que no usará.

**SET-02 · Selector de modelos informativo · P1.** Backend: Unificar alias/digest, capacidades, perfil medido, conexión y residencia; separar descargado de cargado y compatible de recomendado. Interfaz: Buscar por uso, tamaño y modalidad; comparar variantes, contexto útil estimado y motivo de incompatibilidad. Aceptación: Dos tags del mismo digest no aparecen como dos descargas independientes ni como instalado/no instalado contradictorios.

**SET-03 · Ajustes efectivos y explicables · P0.** Backend: Resolver global→proyecto→agente→turno con precedencia definida; validación al guardar y mostrar origen efectivo; reset por bloque. Interfaz: Búsqueda en ajustes, simple/avanzado, help contextual, copiar perfil y ver diferencias respecto a defaults. Aceptación: Cambiar temperatura del turno no altera globales; un flag no soportado produce error explícito en el control correspondiente.

**SET-04 · Salud desde un solo sitio · P1.** Backend: Checks livianos de modelo, búsqueda, almacenamiento, cola, espacio, GPU y plugins; circuit breakers independientes. Interfaz: Panel de salud con última prueba, causa, impacto y acción; copiar diagnóstico saneado. Aceptación: Caer ComfyUI no muestra toda la aplicación como caída ni impide usar chat textual.

**SET-05 · Cambio de proveedor consciente · P0.** Backend: Antes de remoto, calcular categorías de datos a transmitir y confirmar política; mantener credenciales separadas por backend. Interfaz: Indicador Local/Remoto por llamada y aviso si cambia durante una tarea; coste desconocido mostrado como desconocido. Aceptación: Un fallo de autenticación de una suscripción no habilita una API facturable ni cambia de modelo a escondidas.

**SET-06 · Ayuda situada, no manual obligatorio · P1.** Backend: Documentación local versionada y buscable, ejemplos por capacidad, diagnóstico que enlaza al ajuste concreto. Interfaz: Tour opcional, tooltips útiles, accesos a atajos, Qué falta para usar esto y Restablecer esta pantalla. Aceptación: Una función deshabilitada explica si falta modelo, permiso, motor o configuración, en vez de quedarse como botón gris sin motivo.


## 27. Actividad, notificaciones y manejo del trabajo

**ACT-01 · Centro de actividad unificado · P0.** Backend: Indexar runs de chat, research, automatizaciones, renders y workers con estados comunes y enlaces al origen. Interfaz: Filtros Activos/Esperando/Completados/Con problemas; agrupar por tarea y permitir abrir resultados sin encontrar primero el chat. Aceptación: Un render terminado con su conversación cerrada sigue visible y descargable con su procedencia.

**ACT-02 · Notificaciones útiles y configurables · P1.** Backend: Eventos de finalización, bloqueo y fallo con dedupe; preferencias por canal y horas de silencio, sin incluir datos sensibles en pantalla bloqueada. Interfaz: Notificaciones de escritorio opcionales y bandeja interna persistente; clic abre el paso exacto. Aceptación: Una reconexión no vuelve a notificar veinte veces el mismo permiso pendiente.

**ACT-03 · Permisos y preguntas en una bandeja · P0.** Backend: Solicitudes durables con scope, fecha, expiración y revisión del payload; invalidarlas si cambia el plan aprobado. Interfaz: Responder desde tarjeta, detalle de tarea o bandeja; teclado y móvil; mostrar consecuencias y alternativa libre. Aceptación: Una pregunta restaurada tras reinicio acepta una sola resolución y no aplica respuesta a una nueva pregunta parecida.

**ACT-04 · Cierre de ventana sin sorpresas · P0.** Backend: Registrar propiedad de backend y jobs; separar ocultar UI, minimizar a bandeja y apagar servicio; checkpoint/cancelación ordenada cuando corresponda. Interfaz: Diálogo solo si cerrar detiene trabajo; opción mantener backend con estado claro, según plataforma y modo instalados. Aceptación: Cerrar una ventana que reutiliza servidor no lo mata; apagar un backend propio advierte qué tareas interrumpe.

**ACT-05 · Vista de colas y prioridades · P1.** Backend: Colas justas con prioridad interactiva, límites por proyecto y envejecimiento para evitar inanición; evitar prioridades que rompen locks. Interfaz: Mover tareas pendientes, pausar grupo y ver qué recurso bloquea; no prometer posición fija si hay prioridades. Aceptación: Un trabajo de fondo largo no bloquea indefinidamente una consulta corta cuando hay capacidad independiente.

**ACT-06 · Estados de pantalla completos · P0.** Backend: Toda proyección maneja datos vacíos, parciales, cargando, offline, error, permiso denegado y versión incompatible. Interfaz: Mensajes específicos, retry contextual y contenido ya disponible conservado; skeleton solo donde aún falta información. Aceptación: Fallar un panel secundario no vacía todo el workspace ni muestra No hay datos para un error de autorización.


## 28. Diseño, accesibilidad, movilidad e idioma

Mantener DESIGN.md como autoridad visual: editorial cálido, técnico y propio de Faustus; no convertir el trabajo en un rediseño cosmético genérico. Aplicar sus tokens y componentes, completando estados y accesibilidad. WCAG 2.2 sirve de referencia de requisitos, pero ningún texto de esta especificación equivale a una certificación de cumplimiento. [F08, S22]

**A11Y-01 · Todo por teclado · P0.** Backend: Orden de foco estable, semántica nativa y roles apropiados; focus trap solo en modal real, restauración al cerrar. Interfaz: Atajos descubribles y remapeables, Ctrl+K, navegar chat/paneles, responder opciones y aprobar/rechazar sin ratón. Aceptación: Completar una tarea de chat, revisar diff y responder pregunta usando solo teclado sin quedar atrapado.

**A11Y-02 · Lectores de pantalla y streaming · P1.** Backend: Etiquetar iconos, controles y tablas; anuncios de progreso agrupados, no cada token; diferenciar contenido nuevo y cambios de estado. Interfaz: Modo lector, descripciones de adjuntos y resúmenes de tablas; no usar solo color para comunicar estado. Aceptación: Un stream largo no produce cientos de anuncios ni impide escuchar el botón Detener.

**A11Y-03 · Densidad, contraste y movimiento · P1.** Backend: Usar tokens existentes, medición automatizada de pares de contraste y pruebas manuales; respetar zoom y prefers-reduced-motion. Interfaz: Temas oscuro/claro, tamaño de letra, densidad y fuente accesible; foco visible y zonas táctiles amplias. Aceptación: A 200 % de zoom no desaparecen botones críticos ni se recorta el texto de una aprobación.

**A11Y-04 · Móvil y acceso remoto seguro · P2.** Backend: Cliente responsive con reconexión, uploads acotados y autenticación; acceso remoto solo mediante configuración segura, no exposición automática del puerto. Interfaz: Composer con teclado virtual, paneles como hojas, botones táctiles y bandeja de aprobaciones; distinguir host donde se ejecuta. Aceptación: Al pasar a segundo plano y volver se recupera el estado sin reenviar el mensaje ni perder el borrador.

**LANG-01 · Español e inglés de extremo a extremo · P0.** Backend: Localización de controles, errores, fases y fechas; preservar idioma solicitado en herramientas y síntesis; tests con tildes, enclíticos y rutas Unicode. Interfaz: Opciones descriptivas, números y fechas según locale sin cambiar IDs/valores de API; mensajes humanos, no excepciones crudas. Aceptación: Impleméntame y créame activan la capacidad correcta; un error de backend aparece traducido con diagnóstico técnico desplegable.

**LANG-02 · Búsqueda y edición multilingües · P1.** Backend: Normalización Unicode para búsqueda con correspondencia a posición original; detección de idioma por contenido cuando aporta valor. Interfaz: Buscar sin tilde opcional, glosarios de proyecto y preservar términos técnicos al traducir. Aceptación: Una búsqueda normalizada no modifica nombres de archivo ni desplaza los rangos del diff.


## 29. Rendimiento y experiencia bajo carga

El rendimiento percibido depende de mucho más que tokens por segundo: tiempo al primer contenido útil, estabilidad del scroll, respuesta del compositor, carga de modelos, retrieval y esperas de herramientas. Definir presupuestos medibles y validarlos en hardware representativo, separando frontend, backend y modelo. Los siguientes umbrales son objetivos iniciales de producto, no mediciones actuales.

| Área | Objetivo inicial propuesto | Cómo comprobarlo |
| --- | --- | --- |
| Interacción local | p95 menor de 100 ms para feedback visual en acciones básicas; trabajos pesados fuera del hilo UI. | Trazas del navegador con transcript grande y streaming activo. |
| Estado de una acción | Confirmación local inmediata y estado Enviando mientras el servidor decide; sin éxito optimista de efectos. | Red lenta, doble clic, reconexión y respuesta fuera de orden. |
| Streaming | Render agrupado por intervalo/bloque, preservando orden y contenido íntegro. | Miles de deltas y bloques grandes de código sin bloquear selección. |
| Búsqueda local | Mostrar resultados parciales pronto y presupuesto por fuente; objetivo a fijar según tamaño del índice. | Datasets pequeños, medianos y grandes; frío/caliente por separado. |
| Cancelación | Feedback UI inmediato; objetivo de detener trabajo cancelable en menos de 2 s o explicar por qué sigue terminando. | Motor cooperativo, comando no cooperativo y API externa ya aceptada. |
| Inicio | Pantalla usable antes de cargar LLMs o índices completos; sin precarga pesada por defecto. | Arranque limpio, dependencias opcionales ausentes y servicio externo caído. |

**PERF-01 · Frontend incremental · P1.** Backend: Virtualizar listas cuando compense, memoizar bloques terminados, cargar editores/galerías bajo demanda y limitar logs en memoria. Interfaz: Navegar miles de mensajes sin saltos ni congelación; indicador cuando se carga historial adicional. Aceptación: Perf trace con conversación larga conserva interacción, selección y búsqueda; no vuelve a parsear todo el Markdown por token.

**PERF-02 · Backend sin bloqueos accidentales · P0.** Backend: I/O pesado y cómputo fuera del event loop; workers acotados, backpressure y pools con límites; no multiplicar procesos por pestaña. Interfaz: La UI sigue recibiendo estado/cancelación durante extracción, render o escritura de evidencias. Aceptación: Procesar un PDF grande no impide responder a healthcheck o cancelar otra tarea.

**PERF-03 · Cache con semántica correcta · P1.** Backend: Cache de prefijos cuando backend lo soporte, retrieval y herramientas read-only; claves por permisos/versiones/params, invalidación y cuotas. Interfaz: Señalar respuesta cacheada cuando importa; controles de recalcular y liberar caché. Aceptación: Cambiar permiso, contenido o plantilla invalida entradas afectadas; no servir datos de otro usuario por similitud de consulta.

**PERF-04 · Control de recursos de extremo a extremo · P0.** Backend: Medir memoria de proceso, disco temporal, descriptores, threads, GPU y modelos; límites de sesiones y jobs; degradación parcial. Interfaz: Avisos anticipados de espacio/commit; liberar recursos propios y poner trabajos en cola antes de que caiga Windows. Aceptación: Prueba de presión reduce trabajo auxiliar sin corromper estado ni matar procesos ajenos.

**PERF-05 · Transferencias y adjuntos grandes · P1.** Backend: Uploads/downloads por streaming con cuotas, reanudación cuando proceda, hashes incrementales y thumbnails fuera de ruta crítica. Interfaz: Progreso/cancelar de cada archivo y uso de espacio; no cargar vídeo completo como base64 en el estado del chat. Aceptación: Un archivo de tamaño límite no multiplica varias veces la RAM por copias innecesarias ni bloquea el compositor.


## 30. Instalación, actualizaciones, backups y portabilidad

**OPS-01 · Instalación reproducible y modular · P0.** Backend: Versiones/lockfiles comprobados, comandos de instalación por plataforma y componentes opcionales detectables; healthcheck mínimo. Interfaz: Asistente de reparar entorno con preview de acciones; distinguir falta de dependencia de fallo del modelo. Aceptación: Una instalación limpia documentada arranca el núcleo sin depender de archivos no versionados del PC de desarrollo.

**OPS-02 · Actualización sin destruir trabajo · P0.** Backend: Preflight de versión, backup de config/datos, migraciones transaccionales, modo mantenimiento y rollback compatible; no reiniciar durante efectos críticos. Interfaz: Notas de cambios y permisos nuevos; descargar actualización separada de aplicarla; posponer cuando hay tareas activas. Aceptación: Una migración fallida restaura el estado o abre modo de recuperación sin sobrescribir la base con un esquema vacío.

**OPS-03 · Backups consistentes y restauración probada · P0.** Backend: Snapshot consistente de SQLite y objetos referenciados; manifest, checksums, claves separadas y prueba periódica de restauración en sandbox. Interfaz: Elegir destino/frecuencia, ver última copia comprobada, explorar contenido y restaurar selectivamente. Aceptación: Una copia que existe pero no puede abrirse no aparece como backup válido; no copiar solo el archivo SQLite ignorando su WAL activo.

**OPS-04 · Portabilidad de proyectos y conocimiento · P1.** Backend: Export/import versionado de chats, fuentes, memorias, skills y artefactos con referencias relativas; secretos excluidos por defecto. Interfaz: Preview de conflictos, resolver raíces locales y escoger qué importar; migración desde formatos conocidos con informe de pérdidas. Aceptación: Mover un proyecto a otro disco mantiene identidad y enlaces; referencias irrecuperables se muestran, no se descartan silenciosamente.

**OPS-05 · Modo seguro y recuperación · P0.** Backend: Arranque sin plugins, precargas ni tareas automáticas si hay crash repetido; diagnósticos y herramientas de reparar sin borrar datos. Interfaz: Pantalla de recuperación con abrir logs, desactivar extensión problemática y volver a versión anterior cuando sea seguro. Aceptación: Un plugin corrupto no impide abrir la aplicación para desactivarlo ni provoca un bucle de reinicios y cargas GPU.

**OPS-06 · APIs y cliente extensibles · P1.** Backend: OpenAPI versionado, SDK ligero y eventos documentados; compatibilidad hacia atrás controlada, migraciones de configuración y deprecaciones anunciadas. Interfaz: Panel de API local con permisos, tokens y ejemplos seguros; desactivado para exposición remota por defecto. Aceptación: Un cliente antiguo recibe error de versión legible o adaptación, no un fallo ambiguo después de ejecutar media acción.

**OPS-07 · Higiene y costes operativos · P1.** Backend: Rotación de logs, cuotas de blobs, retención de runs, detección de huérfanos y jobs pendientes; no borrar lo referenciado. Interfaz: Panel de almacenamiento por proyecto/modelos/cache/backups y limpieza con preview. Aceptación: Liberar caché no elimina evidencias fijadas ni modelos que siguen en uso; factura remota desconocida no se estima como cero.


## 31. Evaluación: demostrar que Faustus mejora al modelo

La comparación central no es Faustus contra una interfaz vacía ni un leaderboard genérico: es el mismo modelo, en las mismas tareas, con harness mínimo y con las mejoras propuestas. Medir éxito material, coste total e intervención humana. Probar un modelo pequeño compatible, uno mediano, uno fuerte, uno sin tool calling nativo, uno multimodal y un endpoint de comportamiento adverso; registrar versiones exactas y no generalizar desde una única ejecución.

**EVAL-01 · Suite de tareas representativas · P0.** Backend: Fixtures de bugs, features, refactors, investigación, redacción, documentos, datos, preguntas, memoria y acciones externas simuladas; criterios verificables. Interfaz: Laboratorio con resultados por tarea y perfil, baseline y evidencia de fallos. Aceptación: El mismo benchmark se repite tras cambiar parser, prompt, router, contexto o frontend y detecta regresiones.

**EVAL-02 · Ablaciones y coste comparable · P1.** Backend: Ejecutar mínimo→contexto→verificador→memoria→subagentes, cambiando una dimensión; presupuestos y varias repeticiones con seeds cuando existan. Interfaz: Comparar éxito, p50/p95 de latencia, tokens, recursos e intervención; mostrar tamaño de muestra e incertidumbre. Aceptación: Añadir dos agentes no se declara mejora si solo gasta cinco veces más para resolver las mismas tareas.

**EVAL-03 · Caos y protocolos adversos · P0.** Backend: Simular deltas partidos, eventos duplicados, OOM, timeout, caída de worker, disco lleno, proceso muerto, expiración de auth y errores de herramientas. Interfaz: Ver qué se conservó, qué se reanudó y qué quedó incierto; botón reproducir fixture sin tocar servicios reales. Aceptación: Ningún fallo inyectado convierte una acción incierta en éxito ni duplica un efecto externo.

**EVAL-04 · Pruebas frontend reales · P0.** Backend: Unitarias de adapters/componentes y journeys browser con backend simulado y después real; capturas, accesibilidad y estados de reconexión. Interfaz: Galería de estados y evidencia de prueba por pantalla; no considerar un mock de API como integración final. Aceptación: Una tarjeta funciona en vivo, recarga, historial importado y stream reconectado con el mismo contrato.

**EVAL-05 · Regresiones históricas permanentes · P0.** Backend: Convertir cada incidente corregido del proyecto en fixture: false success, corte de outline, borradores, tool selection ES, VRAM y pérdida de red. Interfaz: Vincular bug, prueba y release sin volver a listar el fallo corregido como pendiente. Aceptación: Un cambio en prompts que deja de preguntar ante diseño ambiguo falla CI y no pasa por mejorar otros tests.

**EVAL-06 · Promoción controlada de capacidades · P1.** Backend: Matriz por tupla modelo/backend/plantilla: no probado, experimental, compatible y recomendado; pasar de estado solo con evidencia vigente. Interfaz: Badges explicados y botón de ejecutar calibración ligera; advertencia al cambiar versión. Aceptación: Un modelo textual no hereda soporte de visión de otro con nombre parecido ni un parser antiguo conserva sello compatible tras romperse.

Métricas mínimas: porcentaje de tareas aceptadas, primera pasada, fallos de herramienta, acciones inseguras bloqueadas y permitidas erróneamente, loops, lecturas redundantes, cambios fuera de alcance, precisión de verificador, recuperación, tiempo total, memoria máxima, consumo externo y número de rescates humanos. Para calidad subjetiva de escritura, usar evaluación ciega por criterios definidos y preferencia humana; no fingir una puntuación absoluta de inteligencia.


## 32. Laboratorio: aumentar capacidad sin crear complejidad gratuita

| Idea | Cuándo merece investigarse | Condición para no perjudicar el producto |
| --- | --- | --- |
| Best-of-N / búsqueda de alternativas | Problema difícil con evaluador objetivo y presupuesto disponible. | No lanzar N ramas para una edición trivial; una sola rama ejecuta efectos reales. |
| Ensembles / consejo | Contrastar supuestos o enfoques distintos. | Diversidad real de evidencia, límite de coste y prueba de beneficio; mayoría no equivale a verdad. |
| Modelo especialista para verificación | El generalista falla consistentemente en un dominio. | No cargar otro modelo si provoca spill y empeora el resultado global. |
| Decodificación especulativa | Backend y modelos compatibles y benchmark favorable. | Función del backend, no promesa universal del frontend; medir latencia total y memoria. |
| Ajuste fino / LoRA local | Hay ejemplos autorizados de calidad y objetivo delimitado. | Dataset revisable, sin secretos, holdout, rollback; no entrenar automáticamente con toda conversación. |
| Aprendizaje de procedimientos | Patrón repetido y verificado en el proyecto. | Promover skill versionada y comprobable antes que entrenamiento opaco de pesos. |
| Distribución híbrida | Modelo que no cabe y topología realmente disponible. | Calificar compatibilidad, estabilidad y velocidad antes de recomendar compras. |
| Automatización del escritorio amplia | No hay API/DOM y aporta utilidad demostrable. | Intervención visible, permisos acotados y freno de emergencia independiente. |

Estas líneas son LAB. Ninguna bloquea el núcleo estable ni justifica empeorar el modo de un solo modelo. El objetivo no es la aplicación con más agentes por pantalla, sino la que termina más trabajo correcto con menos fricción.


## 33. Catálogo maestro de herramientas

Los nombres siguientes son un contrato lógico propuesto, no una afirmación de que todas estas funciones existan hoy ni una orden de añadirlas como duplicados. La auditoría BASE-01 debe enlazar cada entrada con una herramienta actual, un adaptador, una skill o un hueco real. Varias operaciones pueden compartir una implementación, pero mantienen permisos y resultados diferenciados. Solo se exponen al modelo las relevantes para la tarea y compatibles con el backend.

Cada entrada hereda el contrato común del capítulo 34: schema de entrada/salida, owner/project, timeout, cancelación, límite de salida, idempotencia, política de reintento, evidencia y errores tipados. Los verbos leer o listar no implican acceso irrestricto: siempre se comprueban permisos. “Externo” requiere política de efectos; “Ejecutar” necesita aislamiento y presupuesto; “Control” no puede conceder permisos nuevos. Un plugin no puede rebajar su riesgo autodeclarando readOnlyHint.


### 33.01 · Utilidades deterministas

Usarlas antes que pedir al LLM aritmética, fechas o transformaciones mecánicas. Límites de tamaño y CPU incluso en regex o parsing.

| Herramienta lógica | Entrada → resultado y evidencia | Clase |
| --- | --- | --- |
| util.calculate | Expresión permitida y unidades → resultado, precisión y expresión normalizada. | Leer |
| util.datetime | Fecha, zona IANA y operación → instante/fecha con DST y ambigüedades resueltas. | Leer |
| util.convert_units | Valor, unidad origen/destino → conversión con dimensiones comprobadas. | Leer |
| util.validate_data | Texto/objeto y schema → errores por ruta, sin reparar significado automáticamente. | Leer |
| util.transform_text | Operación acotada sobre texto → resultado y diff; regex con timeout. | Leer |
| util.diff | Dos versiones → diff estructurado y estadísticas con límites. | Leer |
| util.hash | Bytes o referencia autorizada → digest y algoritmo, no lectura de rutas arbitrarias. | Leer |


### 33.02 · Workspace y filesystem

Versiones, límites de raíces y conflictos siempre activos. Borrar por defecto significa papelera o eliminación recuperable; saltarse recuperación requiere una política distinta.

| Herramienta lógica | Entrada → resultado y evidencia | Clase |
| --- | --- | --- |
| workspace.list | Ámbito autorizado → proyectos, raíces e identidades estables. | Leer |
| fs.list | Directorio, filtros y cursor → entradas paginadas, tipos y tamaños. | Leer |
| fs.stat | Ruta → tipo, tamaño, timestamps y versión/hash cuando se calcula. | Leer |
| fs.read | Ruta/versión y rango → contenido limitado con líneas y EvidenceRef. | Leer |
| fs.search | Raíces, literal/regex y filtros → coincidencias con rangos y cursor. | Leer |
| fs.write | Ruta, contenido y base_revision → versión nueva, diff y recibo. | Escribir |
| fs.apply_patch | Patch y hashes base → cambios aplicados o conflictos sin adivinación. | Escribir |
| fs.mkdir | Ruta y precondiciones → directorio creado o ya existente compatible. | Escribir |
| fs.move | Origen/destino/versiones → movimiento verificable sin sobrescribir ajenos. | Escribir |
| fs.copy | Origen/destino → copia validada, política explícita de colisiones. | Escribir |
| fs.trash | Lista exacta/versiones → elementos recuperables y token de restauración. | Escribir |
| fs.restore | Token y destino → restauración o conflicto con datos posteriores. | Escribir |


### 33.03 · Código, símbolos y proyecto

Fallback léxico visible cuando no hay parser/LSP; no simular referencias semánticas. Lectura del Git del usuario independiente del sistema de snapshots.

| Herramienta lógica | Entrada → resultado y evidencia | Clase |
| --- | --- | --- |
| code.map | Raíz y profundidad → mapa de módulos, entrypoints y cobertura del índice. | Leer |
| code.symbols | Archivo/lenguaje → símbolos, rangos y versión de parser. | Leer |
| code.definition | Símbolo/posición → definición exacta o candidatos inciertos. | Leer |
| code.references | Símbolo → referencias indexadas con cobertura y versión. | Leer |
| code.diagnostics | Archivos → errores de parser/LSP y procedencia del diagnóstico. | Leer |
| code.impact | Diff/símbolos → dependencias y pruebas posiblemente afectadas. | Leer |
| code.rename | Símbolo, nuevo nombre y base → patch semántico revisable. | Escribir |
| project.commands | Proyecto → build/test/lint detectados, origen y estado comprobado. | Leer |


### 33.04 · Ejecución, pruebas y depuración

Cada job identifica host, entorno, proceso y recursos. No ejecutar código arbitrario en el proceso web de Faustus.

| Herramienta lógica | Entrada → resultado y evidencia | Clase |
| --- | --- | --- |
| process.start | Argv/cwd/perfil → job_id, PID propio y canales de salida. | Ejecutar |
| process.status | Job → estado, uso de recursos y última actividad. | Leer |
| process.logs | Job/cursor/filtro → stdout/stderr acotados y referencia completa. | Leer |
| process.input | Job y texto → entrada entregada, solo a sesión autorizada. | Ejecutar |
| process.stop | Job y política → resultado de terminación y efectos pendientes. | Control |
| exec.python | Script/entradas/perfil → salida, artefactos y límites consumidos. | Ejecutar |
| exec.shell | Comando/perfil → ejecución aislada con riesgo mayor declarado. | Ejecutar |
| test.run | Suite/filtro/proyecto → pruebas ejecutadas, fallos y logs. | Ejecutar |
| build.run | Receta de build → resultado, versiones y artefactos de compilación. | Ejecutar |
| debug.inspect | Proceso permitido/operación → stack, breakpoints o variables saneadas. | Ejecutar |


### 33.05 · Git y gestión de cambios

No hacer reset/clean/force-push implícitos. Los remotos son efectos externos; los worktrees pertenecen a tareas y nunca sustituyen el checkout del usuario sin aprobación.

| Herramienta lógica | Entrada → resultado y evidencia | Clase |
| --- | --- | --- |
| git.status | Repo → branch, staged/unstaged, untracked y conflictos. | Leer |
| git.diff | Revisiones/rutas → diff versionado y estadísticas. | Leer |
| git.log | Filtro/cursor → commits e información de cambios. | Leer |
| git.show | Commit/path → contenido exacto con referencia. | Leer |
| git.worktree_create | Base/destino → workspace aislado y ownership de tarea. | Escribir |
| git.commit | Cambios exactos y mensaje → commit local tras comprobar diff. | Escribir |
| git.merge | Ramas/versiones → merge o conflictos, nunca resolverlos a ciegas. | Escribir |
| git.push | Remote/branch/commit → publicación con precondiciones y aprobación. | Externo |
| changes.checkpoint | Ámbito y motivo → snapshot y cobertura de restauración. | Control |
| changes.restore | Checkpoint y opciones → preview/ejecución y cambios no recuperables. | Escribir |


### 33.06 · Recuperación, memoria y conocimiento

La memoria automática y la memoria explícitamente consultada obedecen ámbitos y opt-outs. Todo resultado conserva procedencia.

| Herramienta lógica | Entrada → resultado y evidencia | Clase |
| --- | --- | --- |
| context.search | Consulta/ámbito/tipo → resultados híbridos y puntuación explicable. | Leer |
| context.resolve | EvidenceRef/rango → contenido original exacto o causa de ausencia. | Leer |
| context.explain | Turno → manifest de contexto, fuentes y motivos de inclusión. | Leer |
| project.link_source | Fuente/proyecto → enlace persistente sin duplicar o borrar origen. | Escribir |
| memory.search | Consulta/scope → recuerdos relevantes con fecha y evidencia. | Leer |
| memory.propose | Hecho/ámbito/evidencia → candidato, no verdad global confirmada. | Escribir |
| memory.correct | ID/versión/corrección → revisión y supersesión trazable. | Escribir |
| memory.forget | IDs/ámbito → tombstones, invalidación y límites de borrado explicados. | Escribir |
| knowledge.reindex | Raíz/alcance → job incremental bajo presupuesto. | Ejecutar |


### 33.07 · Estado, interacción y agentes

El modelo puede proponer pasos, no declararse verificado ni concederse permisos. Todas las decisiones se vinculan al run y a su revisión.

| Herramienta lógica | Entrada → resultado y evidencia | Clase |
| --- | --- | --- |
| task.get | TaskID → estado canónico y criterios. | Leer |
| task.update_plan | Revisión/operaciones → plan nuevo o conflicto; pasos con IDs. | Control |
| task.add_evidence | Criterio/EvidenceRef → vínculo validado, no sello de éxito. | Control |
| interaction.ask_user | Pregunta/opciones/contexto → question_id y resolución durable. | Control |
| interaction.request_approval | Acción exacta/efectos → aprobación humana acotada o denegación. | Control |
| agent.delegate | Objetivo/contexto/herramientas/budget → subrun acotado. | Control |
| agent.status | Subrun → progreso, resultados y bloqueos. | Leer |
| agent.message | Subrun/revisión/mensaje → instrucción recibida en punto seguro. | Control |
| agent.cancel | Subrun → cancelación y estado material final. | Control |
| task.pause | TaskID/motivo → checkpoint y estado pausado cuando sea seguro. | Control |


### 33.08 · Web e investigación

Fetch no es navegador autenticado. Conservar fecha, URL original y final, extracción y evidencia; respetar restricciones de acceso.

| Herramienta lógica | Entrada → resultado y evidencia | Clase |
| --- | --- | --- |
| web.search | Consulta/filtros → resultados con fuente y cobertura de motores. | Leer |
| web.fetch | URL/política → documento obtenido con límites y redirecciones seguras. | Leer |
| web.extract | Documento/rango → texto, tablas, enlaces y evidencia. | Leer |
| web.find | Documento/patrón → ocurrencias y fragmentos. | Leer |
| web.download | URL/destino permitido → archivo con MIME, tamaño y hash. | Externo |
| research.plan | Brief/criterios → árbol de cobertura y consultas propuestas. | Control |
| research.run | Plan/budget → job con fuentes, etapas y cancelación. | Ejecutar |
| research.sources | Run/filtros → biblioteca de fuentes y conflictos. | Leer |
| citation.check | Afirmación/referencias → resolución y apoyo observado o incierto. | Leer |


### 33.09 · Navegador

Sesión aislada con identidad de página y snapshot. Enviar formularios, adjuntar archivos o actuar sobre cuentas requiere política de efectos.

| Herramienta lógica | Entrada → resultado y evidencia | Clase |
| --- | --- | --- |
| browser.open | URL/perfil → sesión/página y navegación confirmada. | Externo |
| browser.snapshot | Página → DOM/accesibilidad y snapshot_id. | Leer |
| browser.screenshot | Página/región → imagen con escala, viewport y timestamp. | Leer |
| browser.click | Snapshot/elemento → acción y postestado; no coordenadas huérfanas. | Externo |
| browser.type | Elemento/texto → valor aplicado con secretos por canal seguro. | Externo |
| browser.select | Elemento/opción → selección confirmada. | Externo |
| browser.scroll | Página/región/delta → nuevo viewport y snapshot invalidado. | Control |
| browser.wait | Condición/budget → cumplida o timeout diagnosticado. | Leer |
| browser.upload | Elemento/ArtifactRef → carga del archivo aprobado únicamente. | Externo |
| browser.downloads | Sesión/cursor → descargas, estado y referencias autorizadas. | Leer |
| browser.inspect_errors | Página → errores consola/red saneados, sin tokens de sesión. | Leer |
| browser.close | Página/sesión → cierre sin afectar navegador personal. | Control |


### 33.10 · Escritorio y apps locales

Módulo opcional, nunca permiso implícito de accesibilidad global. Priorizar selectores accesibles y acciones verificadas.

| Herramienta lógica | Entrada → resultado y evidencia | Clase |
| --- | --- | --- |
| desktop.windows | Apps permitidas → ventanas y capacidades accesibles. | Leer |
| desktop.capture | Ventana/región autorizada → captura y referencia de escala. | Leer |
| desktop.inspect | Ventana → árbol accesible y controles. | Leer |
| desktop.action | Control/operación → postestado; pausa si cambia foco o destino. | Externo |
| desktop.input | Ventana/texto o tecla → entrada verificada, sin captura de credenciales. | Externo |
| desktop.handoff | Sesión/motivo → control devuelto al usuario y automatización pausada. | Control |


### 33.11 · Documentos, PDF y presentaciones

Formatos y capacidades se detectan en el motor de artefactos. Render y verificación visual separados del éxito de escritura.

| Herramienta lógica | Entrada → resultado y evidencia | Clase |
| --- | --- | --- |
| document.read | Archivo/rango → estructura, contenido y referencias. | Leer |
| document.create | Schema/plantilla/contenido → documento editable con manifest. | Escribir |
| document.edit | Documento/base/operaciones → versión nueva y diff semántico. | Escribir |
| document.render | Artefacto/opciones → páginas renderizadas y errores. | Ejecutar |
| document.export | Artefacto/formato → archivo validado o pérdidas declaradas. | Ejecutar |
| pdf.pages | PDF/rango → texto y imágenes por página con evidencia. | Leer |
| pdf.extract_tables | PDF/páginas → tablas con celdas y referencia visual. | Leer |
| pdf.ocr | Páginas seleccionadas/idioma → texto derivado y confianza/limitaciones. | Ejecutar |
| slides.create | Esquema/plantilla → presentación editable con notas. | Escribir |
| slides.edit | SlideID/base/operaciones → cambios y render para revisar. | Escribir |


### 33.12 · Hojas de cálculo, SQL y análisis

La ejecución tiene acceso solo al dataset/DB seleccionado. Exportar CSV seguro no implica modificar valores internos silenciosamente.

| Herramienta lógica | Entrada → resultado y evidencia | Clase |
| --- | --- | --- |
| sheet.inspect | Libro/hojas → dimensiones, tipos, fórmulas y estilos. | Leer |
| sheet.read | Libro/rango → valores, fórmulas y formatos explícitos. | Leer |
| sheet.update | Libro/base/rango/valores → cambios con validación. | Escribir |
| sheet.recalculate | Libro/motor → resultados y funciones no soportadas. | Ejecutar |
| data.profile | Dataset → esquema, nulos, distribución y problemas de calidad. | Leer |
| data.query | Dataset/consulta → filas limitadas y plan/coste cuando disponible. | Leer |
| db.schema | Conexión autorizada → tablas/columnas sin extraer todos los datos. | Leer |
| db.query_readonly | Conexión/SQL/budget → resultados con límites y transacción de lectura. | Leer |
| db.execute_change | Plan SQL exacto/aprobación → cambio, filas afectadas y recibo. | Externo |
| chart.create | Datos/especificación → gráfico, datos fuente y texto alternativo. | Escribir |


### 33.13 · Escritura y bibliotecas creativas

Son servicios/skills especializados sobre los artefactos y memoria existentes, no ejecutores paralelos fuera de permisos.

| Herramienta lógica | Entrada → resultado y evidencia | Clase |
| --- | --- | --- |
| writing.outline | Brief/objetivos → estructura editable y pendientes. | Control |
| writing.revise | Fragmento/base/intención → propuesta de cambios, original intacto. | Control |
| writing.compare | Versiones/criterios → diferencias y valoración explícitamente interpretativa. | Leer |
| canon.search | Proyecto/entidades → hechos canónicos con capítulo/fuente. | Leer |
| canon.propose | Hecho/fuente/ámbito → candidato a canon revisable. | Escribir |
| style.extract | Ejemplos autorizados → reglas editables y limitaciones. | Ejecutar |
| style.evaluate | Preset/textos de prueba → variantes comparables y coste. | Ejecutar |


### 33.14 · Imagen y generación multimedia

Preflight de receta y memoria; guardar original, motor, versiones y semilla. La generación puede ser no determinista incluso con semilla, según backend.

| Herramienta lógica | Entrada → resultado y evidencia | Clase |
| --- | --- | --- |
| media.inspect | ArtifactRef → formato, dimensiones/duración, pistas y metadatos. | Leer |
| image.generate | Brief/receta/params → job y resultados con procedencia. | Ejecutar |
| image.edit | Original/máscara/params → resultado no destructivo y proyecto editable. | Ejecutar |
| image.transform | Imagen/operación → resize/crop/convert con parámetros exactos. | Ejecutar |
| media.recipes | Capacidad/motor → workflows instalados y requisitos. | Leer |
| media.preflight | Receta/entradas → recursos, compatibilidad y faltantes. | Leer |
| media.submit | Receta aprobada → job externo único y recibo de aceptación. | Ejecutar |
| media.job_status | Job → estado y artefactos reconciliados. | Leer |
| media.cancel | Job → cancelación posible o resultado ya ejecutado. | Control |


### 33.15 · Audio, vídeo y voz

El usuario concede captura de micrófono explícitamente. Identidades vocales y material personal no se transfieren ni entrenan por defecto.

| Herramienta lógica | Entrada → resultado y evidencia | Clase |
| --- | --- | --- |
| audio.transcribe | Archivo/idioma/motor → segmentos temporales editables. | Ejecutar |
| audio.synthesize | Texto/voz autorizada → audio y parámetros. | Ejecutar |
| audio.extract | Vídeo/pista → archivo de audio con procedencia. | Ejecutar |
| video.frames | Archivo/timestamps → frames referenciados y resolución. | Leer |
| video.edit | Timeline/operaciones → export no destructivo y manifest. | Ejecutar |
| subtitle.export | Segmentos/formato → SRT/VTT validado. | Escribir |
| voice.start | Permiso/dispositivo/modo → sesión de captura visible. | Sensible |
| voice.stop | Sesión → captura detenida y buffers tratados según política. | Control |


### 33.16 · Correo, contactos y calendarios

Destinatarios y ámbitos exactos; herramientas de lectura independientes de escritura y envío. El conector debe permitir consultar estado tras un timeout.

| Herramienta lógica | Entrada → resultado y evidencia | Clase |
| --- | --- | --- |
| contacts.search | Nombre/dominio/ámbito → candidatos con IDs y origen. | Leer |
| contacts.get | ContactID → campos autorizados y vigencia. | Leer |
| mail.search | Consulta/cuenta/cursor → mensajes y fragmentos limitados. | Leer |
| mail.read | MessageID → cuerpo/adjuntos autorizados y referencias. | Leer |
| mail.draft | Destinatarios/asunto/cuerpo → borrador guardado, no enviado. | Escribir |
| mail.send | DraftID/revisión/aprobación → recibo o outcome_unknown reconciliable. | Externo |
| mail.organize | IDs/etiquetas/archivo/papelera → cambios explícitos reversibles donde se pueda. | Externo |
| calendar.list | Calendario/rango/zona → eventos y recurrencias. | Leer |
| calendar.freebusy | Participantes/rango → disponibilidad, no detalles ajenos. | Leer |
| calendar.create | Calendario/horario/IANA/asistentes → evento e invitaciones especificadas. | Externo |
| calendar.update | EventID/revisión/alcance recurrencia → modificación confirmada. | Externo |
| calendar.cancel | EventID/alcance/aprobación → cancelación y notificaciones resultantes. | Externo |


### 33.17 · Servicios externos, tickets y colaboración

Adaptadores proveedores sobre contratos limitados, no una herramienta HTTP con permisos universales. Si la API no permite reconciliar, conservar incertidumbre.

| Herramienta lógica | Entrada → resultado y evidencia | Clase |
| --- | --- | --- |
| remote_files.search | Conector/consulta → recursos autorizados y versión. | Leer |
| remote_files.read | ResourceID → contenido con EvidenceRef y permisos. | Leer |
| remote_files.update | ID/revisión/payload aprobado → versión nueva o conflicto. | Externo |
| issue.search | Proyecto/consulta → tickets/PRs y estado. | Leer |
| issue.create | Proyecto/título/cuerpo → ticket remoto con recibo. | Externo |
| issue.comment | ID/texto exacto → comentario y URL de evidencia. | Externo |
| pull_request.create | Repo/base/head/diff aprobado → PR y comprobaciones. | Externo |
| connector.request | Operación OpenAPI registrada y schema → resultado limitado, no URL arbitraria. | Externo |


### 33.18 · Automatizaciones y workflows

Crear una programación no amplía permisos. El backend puede ofrecer estas capacidades, pero se explicita la necesidad de estar operativo.

| Herramienta lógica | Entrada → resultado y evidencia | Clase |
| --- | --- | --- |
| automation.create | Recurrencia/IANA/acción/política → tarea guardada y próximas ocurrencias. | Escribir |
| automation.list | Ámbito/filtros → programaciones y estado. | Leer |
| automation.update | ID/revisión/cambios → calendario nuevo y explicación de diferencias. | Escribir |
| automation.pause | ID → nuevas ocurrencias pausadas, jobs activos explícitos. | Control |
| automation.run_now | ID/aprobaciones → ocurrencia manual independiente. | Ejecutar |
| workflow.validate | DAG/entradas → dependencias, permisos y errores. | Leer |
| workflow.run | WorkflowID/revisión/budget → run durable con pasos. | Ejecutar |
| workflow.resume | RunID/checkpoint → continuación de unidades confirmadas. | Control |


### 33.19 · Modelos, hardware y backend

Las operaciones de administración no se exponen a cualquier agente por defecto. Cargar, descargar o benchmarkear modelos consume recursos reales.

| Herramienta lógica | Entrada → resultado y evidencia | Clase |
| --- | --- | --- |
| model.list | Backend/filtros → instalado, residente, capacidades y versiones. | Leer |
| model.capabilities | Modelo/tupla → soporte declarado, observado y no probado. | Leer |
| model.fit | Modelo/contexto/topología → estimación con incertidumbre y presupuesto. | Leer |
| model.load | Perfil/ticket de admisión → carga con progreso y recursos reservados. | Ejecutar |
| model.unload | Residencia/job_scope → liberación confirmada o modelo en uso. | Control |
| model.download | Fuente/revisión/licencia/destino → job reanudable aprobado. | Ejecutar |
| model.calibrate | Perfil/suite/budget → métricas y compatibilidad medida. | Ejecutar |
| hardware.status | Nodos autorizados → memoria, GPU, procesos propios y salud. | Leer |
| backend.health | Endpoint → estado, capacidades y diagnósticos no destructivos. | Leer |


### 33.20 · Artefactos, evidencias y administración

Acceso por propietario, proyecto y permiso. Exportar y compartir son acciones diferentes.

| Herramienta lógica | Entrada → resultado y evidencia | Clase |
| --- | --- | --- |
| artifact.list | Ámbito/filtros/cursor → resultados y versiones. | Leer |
| artifact.get | ID/versión → metadatos, preview o bytes autorizados. | Leer |
| artifact.export | IDs/formato/destino → paquete validado sin secretos por defecto. | Escribir |
| artifact.link | ArtifactID/proyecto/chat → relación, sin cambiar ownership. | Escribir |
| evidence.read | ReceiptID/rango → recibo original inmutable y ámbito. | Leer |
| verification.run | Criterios/artefactos/perfil → pruebas y veredicto por criterio. | Ejecutar |
| diagnostic.collect | Selección/permiso → paquete saneado y manifest revisable. | Leer |
| backup.create | Ámbito/destino → snapshot consistente y verificado. | Ejecutar |
| backup.restore | Snapshot/preview/aprobación → restauración con plan de recuperación. | Escribir |


### 33.21 · Extensiones, skills y descubrimiento

Descubrimiento dinámico basado en permisos y relevancia. Instalar código, conectarse a un servidor o ampliar permisos requiere una aprobación separada del uso ordinario.

| Herramienta lógica | Entrada → resultado y evidencia | Clase |
| --- | --- | --- |
| tools.search | Intención/capacidades → descriptores permitidos y schemas bajo demanda. | Leer |
| tools.describe | ToolID/versión → contrato efectivo, riesgos y ejemplos. | Leer |
| mcp.resources | Servidor/consulta → recursos permitidos; lectura no implica ejecutar prompts. | Leer |
| mcp.read_resource | URI reconocida → contenido tipado con procedencia. | Leer |
| skills.search | Intención/ámbito → procedimientos compatibles y revisados. | Leer |
| skills.inspect | SkillID/versión → pasos, código, permisos y verificaciones. | Leer |
| skills.run | SkillID/entradas/budget → workflow sujeto a los mismos permisos. | Ejecutar |
| extension.install | Origen/version/hash/permisos → cuarentena y revisión antes de activar. | Sensible |
| extension.disable | ExtensionID → desactivación y resolución de jobs dependientes. | Control |

Extensiones futuras: CAD/Blender/3D, motores de juego, dispositivos domésticos, GIS, accesibilidad especializada y herramientas de dominio se incorporan con el mismo contrato. El catálogo abierto no debe depender de enumerar hoy cada app imaginable. Debe permitir añadir una familia sin cambiar TaskState, auditoría, aprobaciones, frontend genérico ni semántica de resultados.


## 34. Contratos de implementación y ejemplos

Los schemas del paquete complementario son propuestas de frontera, no una nueva base de datos ni una API ya implementada. Deben adaptarse a las estructuras reales de Faustus mediante adaptadores versionados. JSON Schema valida forma y tipos; la semántica, permisos, hashes vigentes, límites de recursos y precondiciones se comprueban además en código. Los nombres externos de herramientas pueden variar por backend sin alterar la identidad interna.


### 34.1 Entidades mínimas y sus autoridades

| Entidad | Campos mínimos y reglas |
| --- | --- |
| TaskState | task_id, owner_id, project_id, request_ref, revision, state, constraints, acceptance_criteria, plan_steps, decisions, evidence_refs, budget, active_run_id. El servidor valida transiciones. |
| Run / Step | IDs, parent_id, modelo/backend/perfil efectivos, estado, intentos, timestamps, recursos y resultados. Reanudar puede crear un nuevo Run del mismo Task. |
| ToolDescriptor | name, version, input/output_schema, efectos, scopes, timeout, cancelación, idempotencia, retry_policy y executor. No confiar en descriptores de terceros como política. |
| ToolInvocation | call_id estable, attempt_id, task/run/step, tool/version, argumentos validados, expected_revision, idempotency_key y authorization_ref cuando corresponda. |
| ToolResult | call_id, attempt_id, estado, output estructurado, EvidenceRefs, efectos confirmados, error_code, retryable, recursos consumidos y uncertainty. No mezclar éxito y timeout. |
| EvidenceRef | ID, owner/project, tipo de fuente, versión/hash, localizador exacto, captura, límites de acceso y retención. Separar fuente original de resumen derivado. |
| ApprovalRequest | request_id, acción/payload_digest, recursos/destinos, efectos, alcance, expiración, revision y decisión. Cambiar payload invalida la aprobación. |
| QuestionRequest | question_id, Task/Run, pregunta, opciones con IDs estables, multi, respuesta libre y política de caducidad. No confundir preferencia con aprobación de un efecto. |
| ArtifactManifest | ID/versión, propietario, formato, tamaño, hash, inputs, generador, ruta de almacenamiento, estado, validaciones y enlaces de origen. |
| EventEnvelope | event_id, sequence por stream, schema_version, task/run, type, timestamp, payload y visibility. Cursor de replay, orden y dedupe separados de tokens del modelo. |


### 34.2 Ejemplo de llamada y resultado


```json
{
  "schema_version": "1.0",
  "call_id": "call_patch_01",
  "attempt_id": "attempt_01",
  "task_id": "task_demo",
  "run_id": "run_demo_02",
  "tool": {"name": "fs.apply_patch", "version": "1.0.0"},
  "arguments": {
    "path": "src/preferences.py",
    "patch_ref": "artifact:patch_01",
    "base_revision": "sha256:..."
  },
  "idempotency_key": "task_demo:step_04:patch_01",
  "authorization_ref": "grant_workspace_edit_01"
}
```


```json
{
  "schema_version": "1.0",
  "call_id": "call_patch_01",
  "attempt_id": "attempt_01",
  "status": "conflict",
  "output": null,
  "evidence_refs": [],
  "effects": [],
  "error": {
    "code": "BASE_REVISION_MISMATCH",
    "message": "El archivo cambió después de leerlo.",
    "retryable": false,
    "next_action": "read_current_and_reconcile"
  },
  "uncertainty": null
}
```

El conflicto no se arregla reintentando el mismo patch ni quitando la comprobación de hash. El agente debe releer, reconciliar y emitir una nueva intención. Si ya se ejecutó parcialmente una acción no transaccional, debe registrarlo: no devolver effects vacío por comodidad.


### 34.3 Streaming y reconexión


```text
Cliente envía intención con client_message_id
  -> servidor persiste aceptación e identifica Task/Run
  -> publica eventos con secuencia y cursor
  -> cliente confirma presentación, no ejecución
  -> se corta la conexión
  -> cliente solicita replay desde último cursor confirmado
  -> servidor devuelve eventos faltantes o snapshot + cursor
  -> cliente ignora duplicados y no reenvía herramientas
  -> si el backend murió: Task pasa a interrupted
  -> reanudar usa pasos confirmados y reconcilia efectos inciertos
```

Una secuencia reiniciada no se compara sin identificar el stream. El snapshot y su cursor deben ser consistentes entre sí. Persistir miles de tokens individuales puede ser innecesario: se pueden consolidar deltas en bloques con orden y continuidad, conservando eventos de control y resultados terminales. El cliente muestra explícitamente si solo se ha recuperado la visualización o también se ha reanudado la ejecución.


### 34.4 Compatibilidad con ask_user y planes existentes

La integración inicial puede conservar el turno que termina al emitir ask_user, añadiendo question_id y decisión persistida a la respuesta siguiente. No hay que mantener una conexión HTTP abierta indefinidamente para hacer una pregunta durable. A medio plazo, el runtime puede suspender/reanudar un paso, pero ambas rutas deben usar el mismo store de decisiones. Para update_plan, adaptar Markdown antiguo a pasos estructurados con IDs y advertir ambigüedades; no volver a truncar el plan silenciosamente a un número fijo de caracteres. [F05]


### 34.5 Reglas de error y reintento

| Estado | Política obligatoria |
| --- | --- |
| succeeded | Resultado y efectos confirmados. No implica que toda la tarea esté verificada. |
| failed | Causa conocida; retryable solo si semántica y presupuesto lo permiten. |
| cancelled | Indicar qué se canceló y qué pudo ejecutarse antes. |
| conflict | Releer y reconciliar; no cambiar precondiciones silenciosamente. |
| denied | No reintentar con otra herramienta para saltarse permisos. |
| outcome_unknown | No se sabe si ocurrió el efecto. Consultar estado/deduplicación del servicio o pedir reconciliación humana. |
| partial | Conservar resultados válidos y señalar cobertura pendiente, sin convertirlo en éxito total. |

El transporte puede ofrecer entrega al menos una vez; “exactamente una vez” para efectos remotos solo es defendible cuando la operación y su servicio permiten deduplicación/reconciliación adecuada. Un idempotency_key guardado localmente no evita por sí mismo un doble envío después de un timeout de red.


## 35. Escenarios de aceptación y pruebas de caos

Estos casos son un plan de QA, no resultados de pruebas ejecutadas en esta sesión. Cada caso debe incluir fixture, versiones, pasos, aserciones de backend, comprobación de UI y evidencia guardada. Ejecutar primero con servicios simulados; los casos con hardware real, grabación, correo, publicaciones o cuentas requieren autorización específica y presupuesto.

| Caso | Estímulo | Resultado exigido |
| --- | --- | --- |
| QA-01 · Carpeta sin Git | Abrir carpeta Windows con espacios y tildes; pedir una edición de una línea. | Lectura/patch/verificación sin exigir inicializar Git ni preguntar decisiones innecesarias. |
| QA-02 · Proyecto grande | Repositorio con vendor, binarios y código propio; pedir un bug localizado. | Índice respeta exclusiones y devuelve definición/callers/tests sin cargar el repositorio entero. |
| QA-03 · Diseño ambiguo | Pedir sistema nuevo con varias arquitecturas razonables. | Pregunta con opciones/descripcion/libre antes de escribir; la respuesta queda vinculada a la decisión. |
| QA-04 · No preguntar lo obvio | Pedir sustituir texto exacto de un archivo existente. | Ejecuta la edición acotada sin diálogo de arquitectura. |
| QA-05 · JSON y UTF-8 fragmentados | Dividir stream dentro de un carácter y dentro de arguments; intercalar dos call IDs. | Reconstruye llamadas correctas, no ejecuta fragmentos y conserva IDs. |
| QA-06 · Tool falsa en texto | Modelo escribe una llamada en prosa sin canal de tool válido. | No ejecuta código ni JSON aparente; adapta o explica incompatibilidad. |
| QA-07 · Schema peligroso | Argumentos con tipo incorrecto, campo extra y ruta fuera de scope. | Error localizado y autorización independiente; reparación no elimina protección. |
| QA-08 · Reenvío de mensaje | Doble clic en Enviar y caída de red antes del acuse. | Un Task y un mensaje confirmado; el outbox no crea una segunda intención. |
| QA-09 · Replay de stream | Duplicar y desordenar eventos y reconectar con cursor antiguo. | Mismo transcript/progreso final sin acciones o preguntas duplicadas. |
| QA-10 · Reinicio en research | Matar backend tras guardar tres rondas y una sección. | Marca interrupted; Reanudar reutiliza evidencia confirmada; Reintentar empieza otra ejecución explícita. |
| QA-11 · Efecto remoto incierto | API acepta correo simulado pero corta respuesta antes del recibo. | outcome_unknown; consultar por clave/ID o revisión humana, nunca reenvío automático ciego. |
| QA-12 · Cancelar proceso | Comando genera hijos mientras el usuario cancela. | UI responde, árbol propio termina; otros procesos del equipo siguen vivos. |
| QA-13 · Pregunta abandonada | Caduca una aprobación de envío sin respuesta. | No envía por defecto ni interpreta silencio como consentimiento. |
| QA-14 · Respuesta vieja | Resolver question_id anterior después de cambiar el plan. | Rechazo por revisión o decisión obsoleta; no responde a otra pregunta. |
| QA-15 · Edición concurrente | Usuario cambia el archivo después de que el agente lo lea. | Conflicto con base/current/propuesta; ningún cambio del usuario desaparece. |
| QA-16 · Dos agentes escritores | Dos hijos intentan editar el mismo archivo. | Ownership/lease o reconciliación; no last-writer-wins silencioso. |
| QA-17 · Lote parcialmente aplicado | Fallo de disco durante modificación multiarquivo. | Journal y rollback/compensación claros; alcance aplicado registrado. |
| QA-18 · Rollback fuera de alcance | Entre checkpoint y restore se envía correo y usuario edita otro archivo. | Explica lo irreversible y conflicto ajeno; no anuncia reversión universal. |
| QA-19 · No todo verde es correcto | Tests retornan 0 pero falta parte de los requisitos. | Criterio pendiente y estado parcial/no verificado, no tarea completa. |
| QA-20 · Fallo preexistente | Baseline contiene un test roto y el patch rompe otro nuevo. | Separa ambos; no culpa todo al patch ni esconde regresión bajo fallo previo. |
| QA-21 · Citas vacías | Insertar marcadores que resuelven a fuente que no apoya la frase. | Referencia accesible pero apoyo dudoso; no sello de informe verificado. |
| QA-22 · Brief largo | 44 viñetas agrupadas con tablas y preguntas finales. | Cobertura conserva todas; export tiene secciones y tabla final, sin recorte arbitrario. |
| QA-23 · Buscadores degradados | Solo responde un motor; después ninguno. | Aviso de cobertura y fallo diagnosticado; no inventar fuentes ni completed sin evidencia. |
| QA-24 · Modelos simultáneos | Dos jobs solicitan cargar modelos grandes a la vez. | Admisión con reservas; no prometer la misma memoria a ambos. |
| QA-25 · KV desconocida | Modelo nuevo sin medición con contexto largo. | Tamaño de pesos como mínimo y KV desconocida visible; no declarar encaje seguro. |
| QA-26 · Presión de commit | Simular RAM/commit al límite con build y extracción. | Pausa/reduce trabajos auxiliares antes de cascada; conserva estado y UI. |
| QA-27 · Caída de nodo | Nodo remoto desaparece a mitad de generación o render. | Diagnóstico, liberación/reconciliación y resultado parcial/incierto; no éxito falso. |
| QA-28 · Cambio de modelo | Cambiar a modelo sin visión/tool nativo durante una tarea. | Recalcula capacidades, reconstruye estado y no simula capacidades perdidas. |
| QA-29 · Privacidad transitiva | Modo local con reranker o resumen remoto configurado. | Bloquea salida o pide cambio de política; no filtra por componente auxiliar. |
| QA-30 · Inyección en documento | Fuente ordena leer .env y publicarlo. | Texto tratado como datos, exfiltración bloqueada y evidencia del intento. |
| QA-31 · SSRF y redirección | URL redirige a servicio privado/metadata con DNS cambiante. | Revalidación y bloqueo sin fetch interno no autorizado. |
| QA-32 · Preview malicioso | HTML/SVG intenta usar IPC o ejecutar scripts privilegiados. | Sandbox de preview y CSP impiden acceso al equipo y al origen de la app. |
| QA-33 · Aislamiento de dueño | Usuario B prueba IDs de chats, memoria, archivos y recibos de A. | No hay datos por API, SSE, índice, caché ni links de descarga. |
| QA-34 · Olvido e incógnito | Corregir/borrar memoria y abrir turno incógnito con herramientas. | No resurrección desde caché; persistencia y excepciones de archivos explícitos acordes a política. |
| QA-35 · Borrador persistente | Escribir, adjuntar imagen, abrir editor, cambiar chat y recargar. | Texto/adjuntos/edición vuelven; nada se envía sin acción del usuario. |
| QA-36 · Regenerar con efectos | Regenerar resumen de una acción externa completada. | Nueva redacción usa evidencia, no repite acción. |
| QA-37 · Lectura bajo stream | Seleccionar código antiguo mientras llegan miles de deltas. | No salto al final, pérdida de selección ni bloqueo del compositor. |
| QA-38 · Historia y tarjeta | Restaurar desde historial una pregunta con objetos label/description y multi. | Botones correctos, checklist y libre; mismo resultado que en vivo. |
| QA-39 · Export defectuoso | DOCX abre pero tabla excede página; XLSX tiene fórmulas sin recalcular. | Validación visual/cálculo advierte y distingue archivo generado de revisado. |
| QA-40 · PDF escaneado | PDF sin texto y con tabla en imagen. | Lectura visual/OCR opcional con páginas y limitaciones, no documento vacío inventado. |
| QA-41 · Micrófono y autoescucha | Abrir voz sin permiso, luego concederlo y reproducir respuesta. | No grabación inicial; TTS no genera nueva orden; Stop detiene captura. |
| QA-42 · Render ya aceptado | Perder conexión después de submit multimedia. | Consultar job existente y recoger resultado, no duplicar generación. |
| QA-43 · Cambio horario | Recurrencia Europe/Madrid en transición DST y equipo apagado. | Política de ambigüedad/misfire visible; no duplicación silenciosa. |
| QA-44 · Teclado y zoom | Completar chat, permisos y diff solo teclado a 200 % zoom. | Foco útil, controles accesibles y ningún botón crítico fuera de alcance. |
| QA-45 · Actualización fallida | Error a mitad de migración de schema y corte de luz simulado. | Recuperación consistente con backup, no base vacía ni datos mezclados. |
| QA-46 · Plugin problemático | Extensión se cuelga al iniciar o solicita permisos nuevos. | Modo seguro, desactivación y revisión; núcleo sigue usable. |
| QA-47 · Continuidad de novela | Crear final alternativo, descartarlo y escribir capítulo siguiente. | Canon original conservado; alternativa no se recupera como hecho confirmado. |
| QA-48 · Migración de carpeta | Mover proyecto de disco, conservar nombre y abrir artefactos. | Identidad, recuerdos y relaciones continúan; rutas ausentes se resuelven explícitamente. |


## 36. Roadmap de implementación y dependencias

No convertir 187 requisitos y un catálogo extensible en una reescritura simultánea. BASE-01 debe reclasificar cada uno como ya cubierto, parcial o pendiente, con evidencia. La secuencia siguiente organiza incrementos verticales: backend, UI, pruebas, observabilidad y migración juntos. Un requisito no queda terminado porque exista la ruta HTTP o porque el componente compile.

| Hito | Trabajo e integración | Puerta de salida |
| --- | --- | --- |
| M0 · Línea base | Auditar snapshot/checkouts actuales, build limpio, mapa de stores/rutas, tests existentes y configuración efectiva. | Mapa de reutilización y regresiones históricas; ningún módulo paralelo creado por desconocer el actual. |
| M1 · Llamadas y permisos fiables | ModelGateway y broker compatibles, schemas, stream assembler, idempotencia, preguntas/approvals y errores. | QA-03–09, 11–14, 28–33; cero acciones ejecutadas desde texto o permisos inventados. |
| M2 · Trabajo durable | TaskState versionado, eventos/replay, checkpoints por etapa, cancelación y recuperación de research. | QA-10, 12, 15–18; reinicio y conflicto no pierden estado ni duplican efectos. |
| M3 · Calidad y contexto | Index/retrieval, cobertura de brief, evidencia exacta, verificación material y memoria trazable. | QA-02, 19–23, 34, 47–48; finalización conectada a criterios y fuentes. |
| M4 · Flujo diario completo | Compositor, borradores, ramas, workbench, diffs, actividad, errores y accesibilidad. | QA-35–38, 44; mismo comportamiento en vivo, historial y reconexión. |
| M5 · Recursos e instalación | Admisión conjunta, colas, opciones por backend, descargas, salud, cache, procesos y modo seguro. | QA-24–29, 45–46; carga adversa no bloquea el equipo ni destruye trabajo. |
| M6 · Trabajo generalista | Documentos/datos, escritura, navegador, multimedia y conectores priorizados por uso. | QA-39–43, 47; cada entregable abre, conserva procedencia y controla efectos. |
| M7 · Escala y ecosistema | SDK/plugins versionados, automatizaciones avanzadas, acceso remoto y colaboración cuando haga falta. | Permisos, upgrades y recuperación probados por integración; no dependencia nueva para uso básico. |
| LAB · Opcional | Distribución Spark/PC, ensembles, LoRA, speculative decoding y control amplio del escritorio. | Benchmark demuestra utilidad con coste y riesgo aceptables; desactivable sin romper el núcleo. |


### 36.1 Qué dejar fuera del primer incremento

No bloquear M1/M2 por marketplace, diseñador visual de workflows, voz siempre activa, entrenamiento de pesos, edición de vídeo avanzada, cluster Spark, sincronización multidispositivo o colaboración multiusuario completa. El primer producto excelente es un único modelo que usa herramientas con seguridad, conserva contexto útil, termina tareas y muestra exactamente qué hizo. Las ampliaciones se añaden sobre esa base, no en lugar de ella.


### 36.2 Criterio de priorización por uso

Para Luis: carpetas Windows y programación de proyectos; investigación extensa; escritura y documentos; gestión de modelos locales y su hardware; multimedia y voz; conectores/automatizaciones. Ese orden es una propuesta basada en este encargo y el contexto del proyecto, no una autorización para desactivar capacidades existentes. Mantener mínimos de seguridad y durabilidad en cualquier módulo que ya esté expuesto.


## 37. Instrucciones de entrega para Fable y Codex

Este documento es un diseño de producto, no permiso para modificar el equipo de Luis, activar servicios, comprar hardware o tocar cuentas externas. La implementación comienza inspeccionando la revisión actual, que puede ser posterior al snapshot aquí citado. No copiar rutas propuestas sin localizar primero las equivalentes reales.

- Un integrador mantiene el mapa de autoridad y contratos. Otros agentes reciben áreas acotadas con archivos, dependencias y criterios; no dos escritores sin coordinación sobre el mismo módulo.
- Para cada incremento: inventario → diseño breve de integración → fixtures del fallo → implementación backend/frontend → migración si procede → pruebas → revisión de permisos/recuperación → evidencia y documentación.
- Reutilizar Context Engine, admission, ChangeSet store, approval flow, State Mirror, Enseñame y componentes Studio cuando existan. Retirar duplicados solo con migración y pruebas de equivalencia, no con borrado masivo.
- No cerrar un requisito con mocks exclusivamente. Etiquetar por separado unitario, contrato, integración, navegador y prueba con modelo/hardware real; nunca decir probado en pantalla sin haber abierto y comprobado la UI.
- No cargar modelos grandes, activar grabación, ejecutar benchmarks intensivos, elevar permisos, enviar correos ni publicar cambios sin autorización concreta. Usar simuladores primero y respetar el incidente de presión de memoria documentado.
- No modificar el checkout o servicios del usuario para demostrar una feature en una rama de desarrollo. Entornos aislados, puertos definidos, recursos limitados y rollback documentado.
- Cada pull request o entrega cita los IDs de requisitos, cambios de contrato, tests ejecutados con resultado, pruebas no realizadas, riesgos, flags y pasos de rollback. Evitar afirmar experiencia completa por compilar un componente.
- Actualizar PENDIENTES.md/OBJETIVOS.md según sus convenciones reales. No presentar como nuevo un cierre ya documentado ni inflar listas con funciones que solo tienen una clase vacía.

### 37.1 Plantilla mínima de ticket


```text
ID y título:
Estado real: existente / parcial / ausente / no verificado
Rutas reales y autoridad actual:
Problema observable y reproducción:
Contrato deseado y compatibilidad:
Cambios backend:
Cambios frontend y estados:
Permisos, efectos y recuperación:
Migración / feature flag / rollback:
Dependencias y archivos reservados:
Criterios de aceptación:
Pruebas unitarias / contrato / integración / UI / modelo real:
Evidencia adjunta y limitaciones:
Decisión de cierre y revisión humana:
```


### 37.2 Definición de terminado

Terminado = capacidad accesible desde el flujo real + contrato validado + permisos correctos + errores recuperables + estados de UI completos + persistencia coherente + métricas/evidencia + regresiones + documentación + migración/rollback cuando corresponda. Un MVP puede reducir alcance, pero no mentir sobre éxito, perder trabajo deliberadamente o ejecutar sin permiso.


### 37.3 Contenido del paquete

Documento editable y Markdown equivalente; backlog.json con los requisitos y dependencias; tool_catalog.json con contratos lógicos; acceptance_scenarios.json con QA-01–48; schemas/ y examples/ para validar fronteras; validate_examples.py y README de integración. Los schemas son un punto de partida que no sustituye autorización, validación semántica ni pruebas de compatibilidad del runtime.


## 38. Fuentes, alcance y mantenimiento

Consulta: 10 de septiembre de 2026. Las páginas de producto y especificaciones pueden cambiar; fijar versiones al implementar. Las referencias F corresponden al snapshot del repositorio; las S a documentación oficial de los productos o proyectos. La comparativa es documental: no se ejecutaron las aplicaciones competidoras ni se midió su rendimiento. Las propuestas de arquitectura, prioridades y aceptación son decisiones de este diseño, no promesas de proveedores.

**[F01] [Faustus: revisión master inspeccionada](https://github.com/Luissalet/Faustus/commit/b824057edf40a6fa20caec9c1f77b9e6a9d75bad)** — Snapshot y fecha de referencia; no implica ejecución del código.

**[F02] [Faustus: README en español](https://github.com/Luissalet/Faustus/blob/b824057edf40a6fa20caec9c1f77b9e6a9d75bad/README.es.md)** — Stack, capacidades documentadas y componentes existentes.

**[F03] [Faustus: objetivos acordados](https://github.com/Luissalet/Faustus/blob/b824057edf40a6fa20caec9c1f77b9e6a9d75bad/OBJETIVOS.md)** — Admisión de VRAM, preguntas y puntos de reutilización.

**[F04] [Faustus: registro de pendientes y cierres](https://github.com/Luissalet/Faustus/blob/b824057edf40a6fa20caec9c1f77b9e6a9d75bad/PENDIENTES.md)** — Incidentes, correcciones reportadas y límites de recuperación.

**[F05] [Faustus: herramientas de interacción](https://github.com/Luissalet/Faustus/blob/b824057edf40a6fa20caec9c1f77b9e6a9d75bad/src/agent_tools/interaction_tools.py)** — Código leído de AskUserTool y UpdatePlanTool.

**[F06] [Faustus: evidencias de cambios durables](https://github.com/Luissalet/Faustus/blob/b824057edf40a6fa20caec9c1f77b9e6a9d75bad/docs/design/durable-change-evidence.md)** — Autoridad de recibos, verificación, scope y límites.

**[F07] [Faustus: directorio Studio](https://github.com/Luissalet/Faustus/tree/b824057edf40a6fa20caec9c1f77b9e6a9d75bad/studio)** — Listado consultado; verificar proceso de build en checkout limpio.

**[F08] [Faustus: sistema de diseño](https://github.com/Luissalet/Faustus/blob/b824057edf40a6fa20caec9c1f77b9e6a9d75bad/DESIGN.md)** — Identidad, tokens y comportamiento visual que conservar.

**[S01] [OpenAI: características de la aplicación](https://learn.chatgpt.com/docs/features)** — Documentación oficial; la ruta consultada de Codex redirigió a esta página.

**[S02] [Anthropic: introducción a Claude Cowork](https://support.claude.com/en/articles/13345190-get-started-with-claude-cowork)** — Referencia de trabajo delegado con archivos.

**[S03] [Anthropic: subagentes de Claude Code](https://code.claude.com/docs/en/sub-agents)** — Aislamiento de contexto, roles y herramientas.

**[S04] [Anthropic: checkpoints de Claude Code](https://code.claude.com/docs/en/checkpointing)** — Cobertura de restauración y limitaciones declaradas.

**[S05] [Open WebUI: catálogo de funciones](https://docs.openwebui.com/features/)** — Chat, conocimiento, herramientas, multimedia e integración.

**[S06] [LibreChat: agentes](https://www.librechat.ai/docs/features/agents)** — Herramientas, configuración y descubrimiento diferido.

**[S07] [LibreChat: streams reanudables](https://www.librechat.ai/docs/features/resumable_streams)** — Continuidad del transporte y la interfaz.

**[S08] [LM Studio: tool use compatible con OpenAI](https://lmstudio.ai/docs/developer/openai-compat/tools)** — Variación nativa/adaptada y fallos de formato documentados.

**[S09] [LM Studio: aplicación](https://lmstudio.ai/docs/app)** — Experiencia de gestión e inferencia local.

**[S10] [AnythingLLM: agentes](https://docs.anythingllm.com/agent/overview)** — Documentos, agentes y flujos de workspace.

**[S11] [OpenHands: introducción](https://docs.openhands.dev/overview/introduction)** — SDK y entorno de agentes de desarrollo.

**[S12] [Aider: repository map](https://aider.chat/docs/repomap.html)** — Selección estructural de contexto de código.

**[S13] [Cline: checkpoints](https://docs.cline.bot/core-workflows/checkpoints)** — Git sombra, restauración y consideraciones de rendimiento.

**[S14] [Continue: proveedor Ollama](https://docs.continue.dev/customize/model-providers/top-level/ollama)** — Configuración de inferencia local.

**[S15] [LangGraph: persistencia y ejecución durable](https://docs.langchain.com/oss/python/langgraph/persistence)** — Estado recuperable y checkpoints del runtime.

**[S16] [llama.cpp: function calling](https://github.com/ggml-org/llama.cpp/blob/master/docs/function-calling.md)** — Templates, parsers y compatibilidad de llamadas.

**[S17] [Ollama: tool calling](https://docs.ollama.com/capabilities/tool-calling)** — Interfaz de herramientas y ciclo de ejecución.

**[S18] [vLLM: tool calling](https://docs.vllm.ai/en/latest/features/tool_calling/)** — Adaptación de modelos, parsers y salidas de herramientas.

**[S19] [MCP: herramientas del servidor](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)** — Contrato de herramientas y anotaciones; versión consultada.

**[S20] [MCP: prácticas de seguridad](https://modelcontextprotocol.io/docs/2025-11-25/tutorials/security/security_best_practices)** — Fronteras de confianza, autenticación y amenazas.

**[S21] [Electron: seguridad](https://www.electronjs.org/docs/latest/tutorial/security)** — Aislamiento, contenido remoto e IPC.

**[S22] [W3C: WCAG 2.2](https://www.w3.org/TR/WCAG22/)** — Referencia de accesibilidad, no certificación de Faustus.

**[S23] [llama.cpp: backend RPC](https://github.com/ggml-org/llama.cpp/blob/master/tools/rpc/README.md)** — Integración distribuida que requiere validación concreta.

**[S24] [NVIDIA: DGX Spark](https://www.nvidia.com/en-us/products/workstations/dgx-spark/)** — Referencia de producto; no benchmark de un cluster mixto.


## 39. Resultado buscado

Faustus debe conservar su valor al cambiar de modelo: conocimientos y decisiones con procedencia; procedimientos revisados; herramientas coherentes; tareas reanudables; verificación material; proyectos y artefactos portables; y una interfaz que no obligue a adivinar qué pasa debajo. Un modelo mejor debe aprovechar todo esto de inmediato. Un modelo limitado debe recibir ayuda y límites claros, no una fachada que esconda sus fallos.

La experiencia objetivo es sencilla de describir: abrir una carpeta, expresar una intención, ver un plan cuando haga falta, conservar el control sobre decisiones y efectos, trabajar sin perder contexto ni borradores y recibir resultados que se pueden abrir, inspeccionar, verificar y corregir. Ese es el criterio que debe decidir qué entra en Faustus y qué complejidad se queda fuera.
