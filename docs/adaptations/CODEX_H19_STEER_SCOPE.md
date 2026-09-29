# H19 parcial: una instrucción sin destino no se difunde a todos los workers

Al revisar la entrega de mensajes del Council se reprodujo un fallo de ámbito:
si `dispatch.get(run_id)` no encontraba el run, fallaba o devolvía una sesión
vacía, `DispatchTaskExecutor.steer` recorría todos los workers vivos y les
encolaba la instrucción. El aviso en logs decía que no podía determinar el
destino, pero la respuesta devolvía éxito.

Ahora exige resolver la sesión padre antes de leer destinatarios. Sin ella
devuelve `False` y no llama a ningún worker. Con padre conocido conserva el
filtro de pertenencia y el comportamiento existente. No se cambian permisos ni
se incorporan nuevos canales de mensajes.

Tres casos de regresión fallaron antes de la corrección. Después pasaron
**61 pruebas** de adaptadores Council y eventos de subagentes. Todo usa dobles
en proceso; no se enviaron instrucciones a workers reales ni a otras personas.

Alcance: corrige una entrega fuera de ámbito encontrada al preparar H19. Un
`True` sigue significando encolado, no lectura, aplicación o persistencia tras
reinicio. Recibos durables y recuperación de mensajes siguen pendientes.

Procedencia: [backlog H19](CODEX_HARNESS_ANALISIS_2026-09-29.md), comparado con
[Codex b1e72963](https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9).
Implementación propia en el adaptador existente; sin copia de código upstream.
