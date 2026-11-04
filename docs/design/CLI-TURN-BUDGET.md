# Contexto de entrada de un turno CLI

La CLI admite --input-token-budget entre 4096 y 200000, igual que el campo input_token_budget de la API existente. El SID del ejemplo corresponde a una conversación ya creada:

```text
python scripts/faustus_run.py -p "Consulta el equipo indicado" --session SID --input-token-budget 48000
```

También puede fijarse FAUSTUS_RUN_INPUT_TOKEN_BUDGET sólo en el entorno del proceso que se lanza; una opción explícita prevalece, incluso sobre un entorno inválido. Sin ambos, se omite el campo y se conserva la política del servidor. El vigilante normal puede heredar ese entorno temporal sin cambiar su sesión o sus archivos. Los argumentos inválidos fallan antes de crear cliente, chat o conexiones.

El mismo valor se envía en cada tramo del turno, incluidas las reanudaciones aprobadas, y aparece en run_start cuando se pide NDJSON. Se conserva la política nativa del servidor: presupuestar contexto no autoriza truncar la petición humana intacta. Si ésta no cabe, el servidor debe rechazarla con su error de capacidad. Este control no borra historial ni registros de efectos inciertos, ni modifica modelo, preferencias globales o permisos.

El valor es un control de contexto de la API; no promete un coste exacto total ni una velocidad. Las reservas de herramientas/salida y la tokenización tienen sus propios límites. Hay que medir input_tokens y tiempos reales del modelo. Las pruebas usan transporte HTTP y aprobación con datos ficticios/puerto efímero; no prueban por sí solas mejora de latencia ni éxito de una tarea real. La evaluación1672 y su ensayo acotado se registran aparte en el Ágora de Silver y research-inbox/faustus-cli-budget1672.
