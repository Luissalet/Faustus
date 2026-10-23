"""Editable starting briefs; discovery has no side effects or model calls."""
from copy import deepcopy

_TEMPLATES = {
    "en": [
        {"id": "opportunities", "title": "Opportunities with evidence", "brief": {
            "statement": "Prepare a shortlist of opportunities for [project and target audience].",
            "context": "Use the linked sources and the period [dates]. Define the selection criteria and scope first. For each candidate, retain the source, observation date, relevant evidence and missing information. Distinguish observed facts from inference. Do not invent contacts, budgets or interest.",
            "deliverable": "A saved, readable table with candidate, fit, supporting source, date, uncertainty and suggested next step. Explain exclusions and open the final file to check its contents. This assignment prepares research; sending messages requires its own explicit request.", "refs": []}},
        {"id": "offer", "title": "One-page offer", "brief": {
            "statement": "Draft a one-page offer for [audience] about [product or service].",
            "context": "Use the linked product brief, audience evidence and constraints. Keep confirmed capabilities separate from proposals. Use only supplied prices and dates; mark missing ones explicitly. Preserve the requested tone and format.",
            "deliverable": "A saved one-page draft covering the problem, intended outcome, scope, deliverables, supplied terms and proposed next step, with source notes available for review. Open the file and verify readability. Do not publish, send or commit to terms from this drafting assignment.", "refs": []}},
        {"id": "editorial", "title": "Editorial plan", "brief": {
            "statement": "Prepare an editorial plan for [project, audience and period].",
            "context": "Use the linked source material, tone, channels and production capacity. Explain which idea each piece develops and the evidence behind factual claims. Separate existing assets from assets still to produce. Use [timezone] for dates.",
            "deliverable": "A saved calendar and editable briefs with topic, purpose, channel, date, source links, required assets and completion criteria. Check date consistency and open the final files. Publishing and scheduled execution remain separate explicit actions; link an existing automation if recurrence is needed.", "refs": []}},
        {"id": "weekly", "title": "Weekly summary", "brief": {
            "statement": "Prepare the weekly summary for [project] covering [start and end dates].",
            "context": "Use the linked tasks, runs, documents and measurements. Distinguish completed work from declared progress and unresolved items. Calculate totals with tools, retain units and observation dates, and identify absent data. Compare only equivalent periods. Use [timezone].",
            "deliverable": "A saved report with completed outcomes and evidence, changes in measurements, unresolved work, decisions needed and next actions. Link the underlying runs and files; open the report to verify figures and sources. For recurrence, link the existing native automation; this brief does not create or enable a schedule.", "refs": []}},
    ],
    "es": [
        {"id": "opportunities", "title": "Oportunidades con evidencia", "brief": {
            "statement": "Prepara una selección de oportunidades para [proyecto y público objetivo].",
            "context": "Usa las fuentes enlazadas y el periodo [fechas]. Define primero los criterios de selección y el alcance. Para cada candidato conserva fuente, fecha de observación, evidencia pertinente e información ausente. Distingue hechos observados de inferencias. No inventes contactos, presupuestos ni interés.",
            "deliverable": "Una tabla guardada y legible con candidato, encaje, fuente, fecha, incertidumbre y siguiente paso propuesto. Explica exclusiones y abre el archivo final para comprobar su contenido. Este encargo prepara la investigación; enviar mensajes requiere un encargo explícito propio.", "refs": []}},
        {"id": "offer", "title": "Oferta de una página", "brief": {
            "statement": "Redacta una oferta de una página para [público] sobre [producto o servicio].",
            "context": "Usa el brief del producto, la evidencia del público y las restricciones enlazadas. Distingue capacidades confirmadas de propuestas. Usa solo los precios y fechas facilitados; indica expresamente los ausentes. Conserva el tono y formato pedidos.",
            "deliverable": "Un borrador guardado de una página con problema, resultado previsto, alcance, entregables, condiciones facilitadas y siguiente paso propuesto, con notas de fuentes para revisar. Abre el archivo y comprueba su legibilidad. Este encargo de redacción no publica, envía ni compromete condiciones.", "refs": []}},
        {"id": "editorial", "title": "Plan editorial", "brief": {
            "statement": "Prepara un plan editorial para [proyecto, público y periodo].",
            "context": "Usa las fuentes, el tono, los canales y la capacidad de producción enlazados. Explica la idea que desarrolla cada pieza y la evidencia de sus afirmaciones factuales. Distingue recursos existentes de los que faltan por producir. Usa [zona horaria] para las fechas.",
            "deliverable": "Un calendario guardado y briefs editables con tema, propósito, canal, fecha, fuentes, recursos necesarios y criterios de terminación. Comprueba las fechas y abre los archivos finales. Publicación y ejecución programada siguen siendo acciones explícitas separadas; enlaza una automatización existente si necesitas recurrencia.", "refs": []}},
        {"id": "weekly", "title": "Resumen semanal", "brief": {
            "statement": "Prepara el resumen semanal de [proyecto] entre [fecha inicial y final].",
            "context": "Usa las tareas, ejecuciones, documentos y mediciones enlazados. Distingue trabajo terminado de progreso declarado y asuntos abiertos. Calcula totales con herramientas, conserva unidades y fechas de observación e identifica datos ausentes. Compara solo periodos equivalentes. Usa [zona horaria].",
            "deliverable": "Un informe guardado con resultados terminados y evidencias, cambios de mediciones, trabajo abierto, decisiones necesarias y próximos pasos. Enlaza ejecuciones y archivos de origen; abre el informe para comprobar cifras y fuentes. Para recurrencia enlaza la automatización nativa existente; este brief no crea ni activa una programación.", "refs": []}},
    ],
}


def catalog(locale="en"):
    if locale not in _TEMPLATES:
        raise ValueError("Choose en or es for the brief language")
    return {"locale": locale, "templates": deepcopy(_TEMPLATES[locale])}
