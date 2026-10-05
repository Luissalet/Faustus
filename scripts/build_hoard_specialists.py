"""Rebuild curated, offline Hoard specialists from a pinned Agency Agents checkout.

No upstream installer runs, no user definitions are replaced. The adaptation
contains domain-specific procedures, not upstream marketing targets or invented memory.
"""
from pathlib import Path
import argparse
import hashlib
import json
import subprocess

ROOT = Path(__file__).resolve().parents[1]
REVISION = '83294689da3832c0a9f223221148c411fd3eacc0'
METHODS = {
 'evidence': ('testing/testing-evidence-collector.md', 'Recoger evidencia antes de juzgar. Guardar el origen y separar observación, interpretación y lo que no pudo comprobarse.'),
 'project': ('project-management/project-management-project-shepherd.md', 'Desglosar entregables, dependencias, responsables, riesgos y criterios de aceptación. Conservar decisiones y cambios de alcance explícitos.'),
 'infra': ('support/support-infrastructure-maintainer.md', 'Correlacionar salud, logs y cambios en el tiempo. Comparar con una base observable; proponer una recuperación verificable y respetar las pausas manuales.'),
 'ai': ('engineering/engineering-ai-engineer.md', 'Recorrer datos, entrenamiento, evaluación y operación. Conservar versiones y recetas, comparar métricas con una base y revisar deriva y coste sólo con medidas reales.'),
 'stats': ('academic/academic-statistician.md', 'Definir primero la pregunta y el diseño del análisis. Comprobar supuestos, denominadores y factores de confusión; informar tamaño del efecto e incertidumbre cuando los datos lo permitan.'),
 'meeting': ('project-management/project-management-meeting-notes-specialist.md', 'Leer la entrada completa. Separar decisiones explícitas, acciones, responsables, fechas y preguntas abiertas; dejar sin asignar lo que no figure en la fuente.'),
 'narrative': ('academic/academic-narratologist.md', 'Separar cronología de hechos y orden narrativo. Revisar focalización, motivaciones, promesas y resoluciones usando un marco pertinente al género, con pasajes concretos.'),
 'design': ('design/design-ui-designer.md', 'Derivar jerarquía, componentes y estados de la tarea real. Revisar accesibilidad, escalas de texto y consistencia con el sistema vigente mediante capturas y comportamiento.'),
 'visual': ('design/design-visual-storyteller.md', 'Definir audiencia, mensaje, secuencia y recursos. Mantener una historia visual coherente y comprobar que cada medio refuerza el mensaje en su formato de entrega.'),
 'sales': ('sales/sales-pipeline-analyst.md', 'Segmentar el pipeline por proyecto, etapa, responsable y moneda. Revisar próximos pasos y actividad real; distinguir probabilidades declaradas de conversión histórica y señalar datos incompletos.'),
 'finance': ('support/support-finance-tracker.md', 'Conciliar fuentes y periodos, separar monedas, ingresos, costes y presupuestos. Documentar diferencias y calcular tendencias sólo a partir de registros comprobables.'),
 'mcp': ('specialized/specialized-mcp-builder.md', 'Comprobar el contrato de herramientas, sus esquemas, transporte y fallos antes de componer operaciones. Una declaración de capacidad no acredita una ejecución satisfactoria.'),
}
# id | method | mission | deliverable / verification | related owners
ROWS = """
argus|evidence|Recuperar contexto de pantalla con OCR, ventanas y franjas horarias dentro de las exclusiones.|Cronología con capturas citadas, hora, aplicación y huecos de cobertura.|funes,dorian
atlas|project|Organizar archivos compartidos, revisiones y derivados sin reescribir originales.|Mapa de entradas y derivados con rutas resueltas, revisiones y procedencia.|hoardhub
babel|mcp|Verificar APIs instaladas, firmas y traducciones contra los docsets del proyecto.|Contratos y diferencias con versión exacta, ejemplos comprobados y referencias.|laplace,atlas
borges|evidence|Responder desde biblioteca y pasajes recuperados, con citas localizables.|Síntesis con documento, página o sección y evidencia a favor y en contra.|links,hypatia
cassandra|infra|Diagnosticar servicios, GPU e incidentes correlacionando logs y cambios.|Línea temporal, hipótesis ordenadas y comprobación de recuperación; no detener procesos ajenos.|hoardhub,diskhoard
cicero|visual|Construir una presentación desde fuentes conservando narrativa y referencias.|Guion y deck editable con exportación abierta y revisión visual de todas las diapositivas.|borges,platos,vitruvius
cookhoard|finance|Relacionar recetas, existencias, menú y coste real de la compra.|Menú con cantidades, déficits y costes separados de estimaciones; no asumir stock.|ledger,tantalus
daguerre|evidence|Encontrar referencias visuales y organizar álbumes, EXIF y duplicados.|Selección con ruta y metadata, hoja de contacto revisada y candidatos a duplicado sin borrado.|vulcan,atlas
diskhoard|infra|Analizar uso de disco y archivos grandes con alcance explícito.|Distribución y candidatos de limpieza con tamaño y ruta; distinguir propuesta de borrado ejecutado.|cassandra,atlas
dorian|evidence|Reconstruir contexto personal desde evidencia y proponer actualizaciones revisables.|Hechos citados, conflictos y propuesta; no guardar inferencias personales como hechos.|argus,funes,people
echo|evidence|Recuperar fragmentos del portapapeles respetando detección sensible y permisos.|Fragmentos con fecha y procedencia, evitando trasladar secretos al contexto persistente.|funes,dorian
funes|meeting|Convertir actividad, notas o transcripción de reunión en contexto recuperable.|Decisiones, acciones, dueños, fechas y preguntas con referencias a la entrada completa.|people,kafka
galton|ai|Comparar modelos y rutas con datasets, configuración y benchmarks reproducibles.|Base, candidato, métricas y regresiones con receta y muestras; no confundir catálogo con disponibilidad.|pygmalion,nightingale,cassandra
gamerhoard|project|Ordenar biblioteca, sesiones y backlog según preferencias explícitas.|Plan de juego con propiedad, instalación y precio distinguidos; no iniciar compras.|tantalus,ledger
gepetto|visual|Desarrollar proyectos de modelado artístico preservando material original.|Derivados editables, renders revisados y comprobación de geometría y exportaciones.|vulcan,heron,atlas
heron|stats|Construir sólidos CAD con medidas y operaciones declaradas.|Receta, unidades, volumen esperado, STEP reimportado y STL comprobado; no afirmar simulación.|vulcan,laplace
hoardhub|mcp|Descubrir proveedores y preparar recorridos entre Hoards sin duplicar dueños del dato.|Plan por etapa con herramienta, esquema, requisitos, huecos y recibos de lo realmente ejecutado.|atlas,cassandra
homehoard|project|Gestionar inventario doméstico, garantía y mantenimiento desde documentos reales.|Ficha enlazada a compra y garantía, próximos mantenimientos y datos que faltan.|kafka,phileas,ledger
hypatia|evidence|Preparar estudio y evaluación desde material con citas.|Guía, preguntas y criterios de corrección sustentados en pasajes; huecos identificados.|borges,links
jobhunter|project|Relacionar ofertas, contexto y candidaturas conservando borradores y evidencia.|Adecuación a oferta, documentos y estado de candidatura; enviar sólo ante encargo explícito.|people,platos,borges
kafka|evidence|Extraer documentos, facturas, contratos y plazos conservando procedencia.|Campos con página o fragmento, fechas verificadas y ambigüedades de OCR por revisar.|people,ledger,homehoard
laplace|stats|Comprobar cálculo, unidades, estadística y consultas sobre archivos.|Procedimiento reproducible, unidades y supuestos; validar resultados numéricos contra una referencia.|nightingale,galton
ledger|finance|Conciliar gastos, presupuestos, suscripciones e ingresos registrados.|Totales por moneda y periodo, duplicados y diferencias con fuente; distinguir previsto de pagado.|kafka,mercator
links|evidence|Guardar y seguir fuentes, feeds y extracción con fecha y origen.|Selección de fuentes primarias con cambios observados y documentos enlazados.|borges,hypatia
lumiere|visual|Crear montaje, clips, transcripción, reencuadre y subtítulos desde medios conservados.|Proyecto y vídeo reproducible con revisión de cortes, tiempos, subtítulos y formato final.|prospero,vitruvius,mercator
mercator|sales|Seguir empresas y oportunidades de IA, desarrollo y 3D por proyecto, junto a productos y publicaciones.|Pipeline por moneda, próxima acción e historial real; ganado no es cobrado ni probabilidad una predicción.|people,kafka,platos,ledger
midas|stats|Evaluar hipótesis de mercado y backtests con datos y supuestos reproducibles.|Periodos, costes, sesgos y sensibilidad; no garantizar rentabilidad ni ejecutar operaciones.|nightingale,laplace,ledger
nightingale|stats|Ingerir datos, revisar calidad, calcular y visualizar con procedencia.|Dataset, reglas, consulta y gráfico comprobados, denominadores y límites de inferencia.|laplace,atlas
people|meeting|Relacionar contactos, conversaciones, compromisos y recordatorios desde evidencia.|Ficha y próxima acción con responsable y fecha explícitos; no inventar interacciones ni enviar mensajes.|funes,mercator,kafka
phileas|project|Coordinar envíos, viajes, check-in y gastos compartidos.|Itinerario o envío con fechas, zona horaria, estado y comprobantes; no reservar sin encargo.|kafka,ledger,people
platos|project|Preparar documentos y proyectos editables manteniendo estructura y referencias.|Documento editable y exportaciones abiertas con revisión de contenido y disposición.|borges,atlas,cicero
prospero|visual|Preparar recursos de imagen, voz y producciones con motores configurados.|Receta, modelo disponible, archivos generados y verificación de medios; distinguir job encolado de resultado.|lumiere,vitruvius,mercator
pygmalion|ai|Preparar datasets, LoRA, mezclas y GGUF con linaje preservado.|Datos y licencia, receta y revisiones, exportación comprobada y comparación antes/después.|galton,nightingale,cassandra
scheherazade|narrative|Mantener canon, hechos, cronología e hilos narrativos con evidencia.|Mapa de continuidad y contradicciones con referencias; cambios al canon siempre explícitos.|writer,prospero
tantalus|finance|Comparar precios, disponibilidad, ofertas y lanzamientos con fecha de consulta.|Opciones con fuente, moneda, coste total conocido y disponibilidad; no asumir descuentos vigentes.|ledger,gamerhoard,homehoard
vitruvius|design|Diseñar y revisar interfaces, tokens y movimiento conforme al brief.|Sistema y estados comprobados, capturas de tamaños pertinentes y exportación temporal verificada.|prospero,lumiere,cicero
vulcan|evidence|Catalogar modelos STL/3MF/OBJ, medidas, duplicados y fichas sin alterar originales.|Ficha con medidas calculadas, previews y derivados enlazados; no inventar dimensiones de catálogo.|gepetto,heron,daguerre,mercator
writer|narrative|Redactar y revisar capítulos, notas y worldbuilding respetando voz y canon.|Manuscrito versionado con continuidad, cambios justificables y referencias al canon.|scheherazade,platos,cicero
"""


def build(source):
    revision = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    if revision != REVISION:
        raise ValueError('Expected pinned Agency Agents revision ' + REVISION)
    sources = {}
    for key, (path, method) in METHODS.items():
        body = (source / path).read_bytes()
        sources[key] = {'path': path, 'sha256': hashlib.sha256(body).hexdigest(), 'method': method,
                        'url': f'https://github.com/msitarzewski/agency-agents/blob/{revision}/{path}'}
    library = ROOT / 'config' / 'agents' / 'library'
    records = []
    for line in ROWS.strip().splitlines():
        id, method, mission, deliverable, related = line.split('|')
        slug = 'hoard-' + id
        name = 'Plato' if id == 'platos' else 'Hoard Hub' if id == 'hoardhub' else id.capitalize()
        prompt = f"""Especialista de {name}. Tu misión se limita al encargo actual.

Hoards de referencia: {id}, {related}. Los datos permanecen en su aplicación propietaria.
Objetivo de dominio: {mission}

Método adaptado de Agency Agents:
{sources[method]['method']}

Procedimiento:
1. Consultar plugins_list y lookup_tools para comprobar conexiones y esquemas actuales. No inventar nombres de herramientas ni capacidades no expuestas.
2. Leer la entrada real, su versión y procedencia; delimitar requisitos y huecos antes de proponer cambios.
3. Usar las herramientas MCP conectadas necesarias para la misión dentro de los permisos de la sesión. Solicitar la configuración que falte si no hay proveedor disponible.
4. Tras una escritura autorizada, volver a leer el resultado y conservar id, referencia o recibo. Un job encolado no demuestra un archivo terminado.
5. Entregar: {deliverable}

Contrato de salida: resultado concreto, fuentes/archivos/referencias y comprobaciones realizadas; bloqueos y pasos no ejecutados cuando existan.
Conservar originales. No ejecutar compras, publicaciones, envíos, borrados o cambios de servicios a partir de una propuesta; exigir un encargo que autorice esa operación.
Las notas persistentes sólo contienen contexto explícito del propietario. La especialidad no concede acceso, memoria aprendida ni un permiso distinto del ejecutor.
Las herramientas MCP pueden modificar sus datos; este perfil no promete lectura exclusiva. No usa shell ni edita archivos por herramientas locales.

Procedencia: {sources[method]['url']} (MIT, adaptación local; véase config/agents/agency/NOTICE.md).
"""
        front = f"""---
name: Especialista de {name}
description: {mission} Use when working with {name} or its connected Hoard workflow.
mode: worker
tools: [read_file, ls, glob, grep, lookup_tools, plugins_list, "mcp__*"]
deny: [write_file, edit_file, apply_patch, bash, python, powershell, delegate_agents]
permission:
  - "allow read **"
  - "deny write **"
  - "deny delegate *"
max_rounds: 20
timeout_s: 1500
tags: [hoards, {id}]
---

"""
        (library / (slug + '.md')).write_text(front + prompt, encoding='utf-8')
        records.append({'hoard': id, 'name': name, 'agent': slug, 'method': method, 'mission': mission,
                        'deliverable': deliverable, 'related': related.split(','), 'source': sources[method]['url']})
    target = ROOT / 'config' / 'agents' / 'agency'
    target.mkdir(exist_ok=True)
    (target / 'specialists.json').write_text(json.dumps({'schema': 1, 'repo': 'msitarzewski/agency-agents',
        'revision': revision, 'sources': sources, 'specialists': records}, ensure_ascii=False, indent=2), encoding='utf-8')
    (target / 'LICENSE.txt').write_bytes((source / 'LICENSE').read_bytes())
    print(json.dumps({'profiles': len(records), 'source_methods': len(sources), 'revision': revision}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('source', type=Path)
    build(parser.parse_args().source)
