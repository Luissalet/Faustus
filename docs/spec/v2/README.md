# Faustus — Paquete de implementación v2.0

Ubicación en el repo: `docs/spec/v2/`. Baseline auditada: `b824057e` (master, 10-09-2026). El mapa de reutilización (BASE-01) vive en `MAPA_REUTILIZACION.md` junto a este README.

## Empezar
1. Leer capítulos 00–04 de `Faustus_Especificacion_Integral_v2.md`.
2. Auditar el checkout actual; la referencia del documento es `b824057edf40a6fa20caec9c1f77b9e6a9d75bad`.
3. Mapear requisitos/herramientas a código existente antes de crear módulos nuevos.
4. Elegir un incremento vertical y aplicar su definición de terminado.

## Contenido
- `Faustus_Especificacion_Integral_v2.docx`: documento editable y maquetado (43 páginas en la revisión renderizada).
- Markdown: especificación integral equivalente al cuerpo del DOCX.
- `backlog.json`: 187 requisitos, prioridades, backend, frontend, aceptación y dependencias.
- `tool_catalog.json`: 192 contratos lógicos propuestos, no inventario de funciones actuales.
- `acceptance_scenarios.json`: 48 escenarios de pruebas; no ejecutados contra Faustus.
- `sources.json`: 32 referencias y fecha de consulta.
- `schemas/`: 8 propuestas JSON Schema Draft 2020-12.
- `examples/`: 8 ejemplos válidos y 12 casos deliberadamente inválidos.
- `validate_examples.py`: validación local del paquete, sin red ni acceso a Faustus.

## Validar el paquete
```bash
python -m pip install 'jsonschema[format]>=4,<5'
python validate_examples.py
```
La herramienta comprueba schemas, ejemplos, argumentos en segunda pasada, referencias y dependencias. No prueba autorización, semántica, hash vigente, recuperación, compatibilidad real ni la aplicación.

## Reglas de implementación
Conservar las autoridades actuales; no duplicar Context Engine, admission, ChangeSets, approvals o memoria. Un esquema no concede permisos. No ejecutar herramientas desde texto libre. No reintentar ciegamente efectos remotos. No ocultar incertidumbre o pruebas no realizadas. No activar micrófono, correo, servicios externos o cargas intensivas para cerrar un ticket sin autorización.

Un agente integrador coordina contratos. Los implementadores reciben archivos y criterios acotados. Cada entrega incluye rutas verificadas, tests ejecutados, UI comprobada, limitaciones, feature flags y rollback.
