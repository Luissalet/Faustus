# H16 parcial: resultados inciertos no son demostraciones exitosas

La captura automática de «Enséñame» tenía un criterio paralelo de éxito: ausencia
de `error` y código de salida cero. Así, resultados `partial`, `outcome_unknown`,
`cancelled`, `failed`, `denied` o `conflict` con código cero se grababan como
demostraciones exitosas.

El hook usa ahora el resultado ya normalizado por el dispatcher: solo `succeeded`
produce `success=True`. Si no se pudo normalizar, tampoco declara éxito. Se
conserva el resultado bruto para revisión y la captura sigue siendo best-effort;
no se cambia la respuesta de la herramienta ni se activa el modo de enseñanza.

Seis regresiones fallaron antes. Después pasaron **41 pruebas** entre captura,
servicio de enseñanza e intención/resultados H03/H04. Los tests del hook usan
handler y receptor sintéticos; no escriben el archivo de la supuesta herramienta,
no instalan skills ni capturan actividad personal.

Este cambio corrige el dato transmitido a la captura automática. No aprueba un
procedimiento, no verifica por sí mismo el efecto externo ni revisa retroactivamente
las demostraciones anteriores o las observaciones manuales.

Fuente conceptual: [backlog H16](CODEX_HARNESS_ANALISIS_2026-09-29.md) y
[Codex b1e72963](https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9).
Adaptación propia del principio de distinguir resultado y verificación; sin código
copiado de upstream.
