# H10 parcial — refresco de referencias de aprobación preservadas

Fuente conceptual ya visitada: [H10 del análisis local](CODEX_HARNESS_ANALISIS_2026-09-29.md) y [Codex fijado b1e72963](https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9/codex-rs/core). Implementación propia sobre el backlog de Faustus; no establece equivalencia completa con Codex ni reaudita el catálogo upstream.

## Fallo reproducido

Continuación del análisis original `CODEX_HARNESS_ANALISIS_2026-09-29.md` (H10) y del pendiente declarado en `CODEX_H10_COMPACTION_CONTINUITY.md`. El bloque mecánico `Pending approvals` sobrevivía como mensaje system al resolver la tarjeta: un store SQLite temporal real pasó de pending a denied, pero una nueva compactación conservaba texto y metadata como pending. Es una incoherencia de contexto; el store seguía siendo la autoridad de permisos.

## Incremento

`refresh_compaction_approvals` trabaja sobre copias del prompt, sólo mensajes system con metadata de compactación y referencias estructuradas. Reconstruye el bloque mecánico desde metadata y exige su coincidencia como sufijo con el delimitador generado de dos saltos de línea. Sólo ese bloque se reemplaza: las referencias aún pending permanecen; las resueltas dejan la lista pending. Resumen libre, objetivo, restricciones y fuentes se conservan. El historial original no se modifica ni se reescribe en el store.

La metadata `approval_refresh` conserva todas las referencias históricas, estados leídos y `checked_at` UTC. La anotación visible indica que no es autorización. Los estados son los del contrato/store, no una evaluación de vigencia ni concesión de permiso. Una tarjeta eliminada se informa como missing, nunca denied. Una lectura fallida o estado no admitido se informa como unknown; sus referencias históricas permanecen aunque se retire del bloque exacto la afirmación pendiente actual. Si el bloque no coincide, su contenido libre se conserva íntegro y se etiqueta el contexto como histórico. Una anotación modificada externamente también impide reemplazar el bloque; no se borra texto libre.

Se ejecuta al construir cada estado de ruta en agent_loop, incluso sin compactación, y en las entradas de maybe_compact y compact_with_integrity para sus otros callers. Un refresco repetido sustituye su propia anotación exacta y no acumula anotaciones. No realiza acciones de aprobación, entrega ni llamadas a modelos. No convierte granted en consentimiento para ejecutar: el consumidor debe seguir consultando la autoridad de aprobación existente.

## Validación y límites

`tests/test_compaction_approval_refresh.py`: store temporal real pending, denied, granted y eliminación; fallo del store; bloque no coincidente; contenido y anotación alterados; roles libres intactos; timestamps, metadata histórica y repetición sin crecimiento; ambos compactadores sin compactación; circuito real de agent_loop captura el prompt principal sin compactar ni llamar a HTTP externo.

Alcance: sólo referencias mecánicas identificadas por metadata. No reinterpreta menciones de aprobación en prosa libre ni demuestra confianza global, autorización válida del plan, portabilidad de checkpoints o captura atómica del store con la inferencia. La lectura es un estado observado en checked_at; una decisión posterior sigue gobernada por el store. No modifica el historial persistido de compactaciones previas. La política de aprobación y sus permisos permanecen iguales.


Metadata legacy malformada: antes de reconstruir el bloque se validan listas de tarjetas y sus campos textuales, restricciones/fuentes como listas de texto, objetivo textual y la estructura de la anotación previa. Si algún campo no cumple el formato generado, se devuelve el mensaje original íntegro sin consultar el store ni añadir estados, timestamps o afirmaciones actuales. Esa excepción conservadora mantiene el contexto histórico tal como llegó; no lo presenta como una actualización verificada. Las pruebas incluyen entradas de aprobación no diccionario, atributos con tipos inválidos y anotaciones previas no textuales.


Selección final tras los guards de metadata legacy:

```text
venv/Scripts/python.exe -m pytest -q tests/test_compaction_approval_refresh.py tests/test_compaction_instruction_provenance.py tests/test_compaction_preserve_persistence.py tests/test_ctx_compaction_integrity.py tests/acceptance/test_a14_compaction_preserve.py tests/test_context_compactor.py tests/test_context_compactor_regressions.py tests/test_context_compactor_nonstring.py
```

Resultado final: **142 pruebas aprobadas**, 36,18 s. Incluye 29 casos del nuevo archivo (18 de metadata legacy), sin modelos, correo, red externa ni datos personales. Las selecciones anteriores se solapan y no se suman a este resultado.
