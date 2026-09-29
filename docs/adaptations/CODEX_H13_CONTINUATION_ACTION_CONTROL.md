# H07/H13 parcial — acción pura gobierna la ampliación

Fecha: 2026-09-30. Continuación de [ampliaciones shadow](CODEX_H13_CONTINUATION_SHADOW.md),
con fuente original fijada [Codex b1e72963c3b71a9265a551e54beff078384efed9](https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9).
No se repite la revisión upstream ni la matriz de 432 combinaciones.

## Promoción acotada

Al superar el presupuesto de rondas, `decide_round_extension` recibe el estado
previo del gate y su acción válida extend/stop gobierna la entrada a la rama de
ampliación. El bucle conserva la concesión original de `max_rounds`, decremento
de ciclos, racha, techo, mensajes de continuación, notas del ledger y SSE.
No se usan los deltas de la decisión para alterar presupuesto ni se convierte
su causa en una nueva razón pública de parada. La comparación de transiciones
permanece como diagnóstico acotado.

Se admite una decisión de la dataclass prevista, acción extend/stop y campos
con tipos correctos. Excepción, objeto distinto, acción no válida o campos
malformados dejan la acción ausente y usan explícitamente la condición legacy.
Ese fallback conserva la conducta anterior cuando fallaba el observador, sin
dar nuevos grants ni modificar permisos o presupuesto de autonomía.

El contrato sí cambia para una decisión válida: ya no es sólo observación.
El test previo que demostraba que una predicción válida equivocada no podía
cambiar el control ahora prueba que un stop válido impide la ampliación.
Sus controles de ciclos, ledger y logs acotados se mantienen. El helper AST
legacy selecciona la condición fallback original para conservar la referencia.

## Evidencia y límites

15 pruebas focalizadas ejecutan mediante AST el gate completo real, sin arrancar
modelos. Cinco escenarios (ciclo configurado, progreso, recuperación, racha y
techo) comparan estado, presupuesto, ciclos, racha, mensajes, notas, SSE,
awaiting/exhausted y stop reason con el gate legacy. Ocho casos fuerzan errores
o resultados malformados en extensión y cierre; dos prueban autoridad stop y
los dos eventos de progreso existentes. Los grants y las razones públicas se
afirman además de comparar snapshots.

Validación final del coordinador sobre código congelado: 30 pruebas correctas
y una matriz deselectada en 3.51 s (nueva suite y shadow existente). La selección
ampliada previa obtuvo 70 pruebas correctas y una deselectada en 89.74 s;
había recogido 13 casos nuevos antes de añadir los dos controles de campos
malformados. No se suman ambas corridas como una suite única.

Esta promoción no constituye el controlador común de todo el turno: retry,
completion engine, plan coverage, autorización y presupuesto de autonomía
siguen fuera. No se afirma equivalencia de todo agent_loop. En particular la
causa pura loop_recovery no sustituye las razones públicas actuales de cierre.
No se registran contenido de usuario ni decisiones durables nuevas.
