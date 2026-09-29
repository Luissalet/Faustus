# H16 parcial: estados de ejecución como evidencia del harness

Implementado: `TurnLedger.record` y las fuentes recuperadas de
`metadata.tool_events` solo aceptan resultados que superen los controles de
error/permisos/código existentes y normalicen a `succeeded`.

Antes, `write_file` con `status=partial` o `outcome_unknown`, sin error y con
código cero o ausente, producía `ok=True`. Una tarea cerrada después quedaba
`verified=True` y `mutation_backed=True`. Además, `web_fetch` desconocido en
el historial contaba como página consultada y respaldaba afirmaciones sobre
fuentes. Ambos fallos se reprodujeron usando el ledger real, sin ejecutar
herramientas ni escribir archivos del usuario.

Se reutiliza `normalize_tool_result`. `result_status` persistido es canónico:
solo `succeeded` respalda evidencia; valores ajenos o malformados no verifican.
El campo `status` de productores antiguos tiene también vocabulario libre
(`ok`, `ready`, `completed`): las cadenas siguen la compatibilidad del adaptador
compartido. Un tipo inválido en ese campo no puede respaldar verificación.
Las respuestas heredadas sin estado conservan el criterio existente.

Pruebas nuevas: 53 casos de estados parcial/desconocido/cancelado/fallido/rechazado/
conflicto, código cero o ausente, directo e histórico, controles de éxito,
compatibilidad heredada y contradicciones. Se comprueba progreso, mutaciones y
fuentes sobre `TurnLedger`, no solo una función auxiliar. Las solicitudes
`tool_calls` aisladas dejan de contar como páginas leídas y no pueden anular un
resultado explícito fallido/desconocido del mismo mensaje. Los mensajes `tool`
con texto plano conservan compatibilidad, mientras sus estados explícitos y los
resultados JSON se comprueban. Un fallo posterior no borra una lectura exitosa previa.

Selección de estados, harness y fuentes: **191 pruebas aprobadas**.
La selección con integración del loop terminó con **193 pruebas aprobadas** y
se inició antes del ajuste final de fuentes históricas: valida la primera variante del criterio de resultados,
no se atribuye al código final. Los 191 casos anteriores sí se ejecutaron después
del ajuste de llamadas anunciadas y resultados mezclados.

Límites: no completa H16 ni certifica semánticamente toda respuesta. Quedan
verificadores de workers/CodeMode; los mensajes `tool` heredados de texto plano
siguen contando como fuente si no contienen señales estructuradas de fallo. Esto
no demuestra que el contenido leído respalde cada afirmación concreta del modelo.
No cambia presentación Studio, permisos, productores o contrato compartido.

Referencia ya analizada: [backlog H16](CODEX_HARNESS_ANALISIS_2026-09-29.md) y
[Codex fijado b1e72963](https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9).
Adaptación propia del principio de distinguir ejecución y evidencia de éxito.
