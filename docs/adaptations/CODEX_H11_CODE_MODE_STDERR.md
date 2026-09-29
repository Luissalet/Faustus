# H11 parcial: drenado continuo de stderr en Code Mode

Fallo reproducido: un script propio que escribía 1 MB sin saltos de línea al
descriptor 2 (`os.write`) quedaba bloqueado y terminaba por timeout. El runner
solo leía 8192 bytes una vez y dejaba de vaciar el pipe. `sys.stderr` del guest
está capturado como salida ordinaria, por lo que esa ruta no reproduce el mismo
problema; la prueba usa deliberadamente el descriptor real.

Ahora stderr se drena hasta EOF por bloques de hasta 8192 bytes, conservando
solo los últimos 8192 bytes. La respuesta de terminación sigue exponiendo como
máximo los últimos 2000 caracteres decodificados. La memoria del lector queda
acotada y no depende del volumen acumulado; no se escribe un log adicional.

Pruebas con subprocess real, sin LLM ni datos del usuario: 2 MB sin LF seguidos
de `finished`, stderr grande a ambos lados de una llamada al bridge simulada
(el protocolo stdout sigue intacto), y salida abrupta que conserva únicamente
la cola final con marcador conocido.
Selección completa de Code Mode (recibos, garantías, aprobación y A10/A11):
**26 pruebas aprobadas**.

No cambia límites, permisos, aislamiento o activación de Code Mode. El fallo
separado de frames JSON mayores de 64 KiB en stdout sigue pendiente de otro
incremento. Tampoco crea auditoría completa de operaciones directas del host.

Fuente conceptual ya visitada: [H11](CODEX_HARNESS_ANALISIS_2026-09-29.md) y
[Codex fijado b1e72963](https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9).
Implementación propia; no se repite revisión upstream.
