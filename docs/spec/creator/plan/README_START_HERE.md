# Faustus Creator — paquete de implementación

El paquete desarrolla un espacio Creator multimedia y mejoras profundas sobre capacidades que Faustus ya tiene. Contiene **156 features**, **43 paquetes de trabajo**, especificación de producto, arquitectura, model intelligence, adapters, orquestación, pruebas y handoffs. Es un plan propuesto, no una implementación ni una certificación de motores.

La baseline es `Luissalet/Faustus@fa567977a00505b18a0fdf1b6e4d1a83f56270c6`. Antes de cambiar código, comparar el HEAD. No reconstruir proyectos, artifact store, permisos, workflows o sistema de modelos sólo por crear Creator.

## Archivos de lectura

[Plan maestro completo](FAUSTUS_CREATOR_PLAN_MAESTRO.md) · [Guía de decisiones en Word](FAUSTUS_CREATOR_GUIA.docx) · [Guía en Markdown](GUIA_DE_DECISIONES.md). La guía resume las decisiones; el maestro y las fichas contienen el desarrollo técnico completo.

## Lectura recomendada

Para comenzar la implementación, leer [baseline y gaps](docs/00_BASELINE_Y_GAPS.md), [arquitectura y contratos](docs/03_ARQUITECTURA_Y_CONTRATOS.md), [modelos y recursos](docs/04_MODELOS_Y_RECURSOS.md) y [roadmap](docs/07_ROADMAP_Y_PAQUETES.md). Después abrir una sola ficha en `backlog/workpacks/`, empezando por WP00.

Para producto y UX, leer [Creator](docs/02_CREATOR_PRODUCTO.md), [recetas completas](docs/11_RECETAS_DE_PRODUCTO.md) y [catálogo de features](docs/13_CATALOGO_FEATURES.md). Para auditar decisiones, usar [fuentes/donantes](docs/01_FUENTES_Y_DONANTES.md), [ADR](docs/10_ADR_DECISIONES.md) y [fuentes completas](docs/14_FUENTES_COMPLETAS.md).

[Orquestación y continuidad](docs/05_ORQUESTACION_Y_CONTINUIDAD.md), [adapters multimedia](docs/06_ADAPTADORES_MULTIMEDIA.md), [pruebas](docs/08_PRUEBAS_Y_ACEPTACION.md) y [seguridad/licencias/migración](docs/09_SEGURIDAD_LICENCIAS_Y_MIGRACION.md) detallan los contratos de ejecución.

El informe maestro reúne la documentación, pero no es la entrada recomendada para un agente con presupuesto de contexto limitado. Los JSON del backlog y las 43 fichas permiten delegar por paquetes y dependencias.

## Entrega a Claude y Codex

Usar [handoff Claude](prompts/CLAUDE_HANDOFF.md), [handoff Codex](prompts/CODEX_HANDOFF.md) o [revisor](prompts/REVIEWER_HANDOFF.md), junto a una ficha WPxx. Completar la [plantilla de asignación](prompts/ASIGNACION_DE_PAQUETE.md) con HEAD, dependencias, write set y presupuesto de pruebas. Un único integrador debe coordinar archivos compartidos y lane de tests/builds pesados.

`backlog/features.json` y `backlog/workpacks.json` son formatos propios de este paquete, no se afirma que sean importables directamente por el Board de Faustus sin adaptación. No se han creado issues, commits, PRs ni cambios en el repositorio.

## Cobertura y límites

Se leyeron Faustus y los tres repositorios nombrados: Chat On Steroids, SteroidChat y Killer ChatGPT Prompts. Los enlaces cortos no se resolvieron; se usaron los nombres explícitos aportados. **Los 15 posts de X permanecen sin contenido recuperado y sin proyectos atribuidos.** Esto limita la cobertura de esas fuentes, no se oculta ni se sustituye por suposiciones. Ver el [ledger completo](docs/12_COBERTURA_Y_LIMITES.md).

Invoke, WhisperX, VideoLingo, Chatterbox, ACE-Step, Wan, LTX, OpenCut, OTIO y documentación técnica complementaria son investigación adicional independiente, no identidades recuperadas de los posts. Las sugerencias de implementación son propuestas propias y se distinguen de funcionalidades declaradas por upstream.

No se ejecutó Faustus, su suite, inferencia, benchmarks, acceso de cuentas ni captura de micrófono. Las observaciones de código son estáticas y requieren pruebas. Licencias y capacidad de hardware se deben confirmar por revisión y configuración antes de copiar, distribuir o ejecutar.

## Contratos y verificación del paquete

[Contratos de referencia](contracts/README.md): cuatro schemas y siete ejemplos sintéticos. No son APIs existentes, no contienen medios reales ni resultados de modelos y no inician operaciones. Desde la raíz del paquete, con Python y `jsonschema` ya disponibles:

```bash
python tools/validate_plan.py
```

El script no instala dependencias, no usa red y no ejecuta Faustus. Comprueba estructura, IDs, DAG, links internos, schemas y coherencia de ejemplos. El informe `VALIDACION_DEL_PAQUETE.md` distingue estas comprobaciones de las pruebas de aplicación pendientes.
