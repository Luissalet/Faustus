# H24 parcial: aislamiento de imports en el banco de pruebas

Al combinar la selección de skills con el bucle completo, la prueba de cierre
se atascó hasta el timeout: `_strip_think_blocks` recibía un `MagicMock` en lugar
de texto. Aislada, la misma prueba terminó correctamente en 17,08 s.

La causa era `test_skill_index_prompt_injection.py`: durante la colección
instalaba sustitutos globales permanentes de `src.agent_tools`, SQLAlchemy y
modelos de base de datos. Los imports posteriores del bucle retenían funciones
simuladas incluso en pruebas que necesitaban las reales.

Se retiran esos sustitutos. Las dependencias están disponibles en el entorno de
pruebas del proyecto; las preferencias y datos de cada prueba siguen aislados
mediante monkeypatch y directorios temporales. No cambia código de producción.

Se repitió la selección en el mismo orden: **252 pruebas correctas en 65,69 s**,
incluyendo skills, decisiones shadow, plan/cierre, historial de workers,
contabilidad y Code Mode. La ejecución anterior con timeout no se cuenta como
validación aprobada. No se atribuye el timeout a una regresión de producción.

No se han saneado todos los módulos históricos que puedan sustituir imports.
Este incremento corrige la contaminación demostrada, no certifica aislamiento
de toda la suite ni sustituye un banco de tareas con modelos reales.

Referencia conceptual ya visitada: [H24 del análisis](CODEX_HARNESS_ANALISIS_2026-09-29.md)
y [Codex fijado](https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9).
Corrección propia del banco local; no se volvió a revisar upstream.
