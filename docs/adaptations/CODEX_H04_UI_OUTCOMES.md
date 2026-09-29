# H04: resultados parciales y desconocidos en Studio

Estado: **implementado este tramo de presentación**, H04 global no se declara cerrado.
Continúa [la conservación de efectos](CODEX_H04_EFFECT_RESULTS.md) y el
[backlog de Codex](CODEX_HARNESS_ANALISIS_2026-09-29.md).
Referencia original: [normalización de Codex fijada](https://github.com/openai/codex/blob/b1e72963c3b71a9265a551e54beff078384efed9/codex-rs/core/src/context_manager/normalize.rs#L21).
Se adapta el principio de conservar lo conocido del resultado; no se copia código.

## Cambio comprobable

Antes, un resultado parcial o desconocido podía mostrarse como éxito porque Studio
solo observaba `exit_code`, que puede ser cero o estar ausente. Ahora la salida SSE
y el registro persistido incluyen `result_status`; la ejecución tras aprobación
también conserva estos campos. El adaptador y el modelo de Studio usan el estado
normalizado tanto en directo como al restaurar conversaciones.

Los pasos parciales y sin confirmación llevan indicador ámbar, etiqueta explícita
y consejo de comprobar el resultado antes de repetir. Hay textos en español e
inglés; el color no es la única señal. Los controles fallido, rechazado y cancelado
siguen sin mostrarse como éxito. Un estado explícito malformado se trata como
desconocido; los registros antiguos sin estado mantienen la interpretación anterior.

La proyección de incertidumbre permite únicamente `reason` y `reconcile_action`,
si son cadenas, limitadas a 512 caracteres cada una. No propaga metadatos arbitrarios.
La interfaz muestra el motivo escapado por React y consejo comprensible, sin mostrar
la acción interna de reconciliación como jerga al usuario.

## Evidencia y límites

- `pytest tests/test_tool_presentation.py tests/test_studio_model_js.py tests/test_codex_h04_effect_results.py -q`: **33 aprobadas**.
- `node studio/checks/model.check.mjs`: aprobado, con parcial/desconocido frente a
  códigos ausente, cero y uno en eventos reales decodificados e historial.
- TypeScript `tsc --noEmit` y `npm run build`: aprobados. Persisten avisos anteriores
  de referencias a fuentes y tamaño del bundle.
- Render local de la implementación real `ToolRail`, con datos ficticios y estados
  restaurados: etiquetas y avisos visibles, sin indicador verde, a anchura de escritorio
  y contenedor de 390 px. En Chrome headless la ventana mínima era 500 px, por lo que
  se comprobó el contenedor de 390 px explícito; no se afirma emulación móvil completa.
  No se llamó al backend ni a un modelo ni se modificaron conversaciones del usuario.

Esta prueba visual aislada no sustituye un recorrido de conversación en navegador
con SSE real. La propagación Python se prueba en su proyección compartida; los puntos
de emisión y persistencia se revisaron en código. No se infiere certeza nueva para
productores heredados ni se cambian políticas de repetición, permisos o reconciliación.
