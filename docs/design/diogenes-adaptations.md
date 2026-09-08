# Adaptaciones de Diogenes

Revisión: 08-09-2026. Fuente revisada: [CommanderTurtle/diogenes, dev, 31d62b59](https://github.com/CommanderTurtle/diogenes/tree/31d62b59b13a8087018de4a959cbb817103f4f1f). Este documento registra decisiones implementadas, no una cola de ampliaciones.

## Integrado en Faustus

### Recuperación de fuentes en investigación

Inspirado en [87d01f6f](https://github.com/CommanderTurtle/diogenes/commit/87d01f6f801276fecc7b6b9c8aa0c9a88585de69), de CommanderTurtle.

`src/deep_research.py` conserva texto original cuando la página se descargó correctamente pero el extractor falla o devuelve una salida vacía. El resultado incluye URL, título y `extraction_mode=rendered_page_fallback`; no se presenta como una conclusión verificada. Las páginas vacías y los rechazos explícitos por falta de relevancia siguen descartándose.

Se guardan hasta 6.000 caracteres de fuente, y la síntesis recibe hasta 2.000 de evidencia junto a un resumen acotado. El registro de citas y los controles de idioma propios de Faustus se mantienen. Una generación final vacía ya no borra el informe acumulado. Pruebas: `tests/test_research_source_recovery.py`, `tests/test_deep_research_report.py` y las regresiones existentes de investigación.

### Cancelación segura de consultas compartidas

Adaptación del arreglo de daixiheguu en [ce04dc1d](https://github.com/CommanderTurtle/diogenes/commit/ce04dc1db46bd198e2455b61a7b1102df2c5a274), incorporado a la historia de Diogenes desde Odysseus.

En `src/task_scheduler.py`, cancelar un consumidor ya no cancela la consulta que comparte con otros. Cancelar al productor despierta a los consumidores y permite un nuevo intento. La entrada pendiente se limpia también ante cancelaciones repetidas. Se consume la excepción del futuro aunque no queden consumidores. Pruebas: `tests/test_task_scheduler_cache.py`.

## Comparado y no duplicado

- **Enlaces de citas:** la implementación de Faustus ya protege enlaces Markdown, código, HTML y puntuación; las citas consecutivas conservan destinos separados. No se sustituye por la variante de Diogenes.
- **Hermes/Librarian/Retrieval:** Faustus ya tiene contexto por proyecto, memoria recuperable y delegación entre agentes. No se añade un segundo registro de herramientas ni un stack de almacenamiento paralelo por defecto.
- **Services, tmux, Sandwich y motores Colibri/Prism:** son una infraestructura extensa orientada a otros entornos y hardware. Se mantiene el sistema actual de Cookbook, clientes oficiales y procesos con propiedad verificada; no se instalan runtimes o motores ajenos como efecto de esta revisión.
- **Investigación larga y visión directa:** no se importa todo el cambio `fa510e59`, que modifica contratos de proveedores y la antigua interfaz. Se adopta la recuperación de evidencia compatible con el flujo actual y sus controles de contexto.

Licencia: AGPL-3.0-or-later, con atribución en `ACKNOWLEDGMENTS.md`.
