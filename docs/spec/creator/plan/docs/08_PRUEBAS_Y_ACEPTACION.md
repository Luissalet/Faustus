# Verificación y criterios de aceptación

## Niveles de evidencia

El plan exige distinguir **documentado**, **localizado en código**, **integrado por ruta/UI**, **probado con fixture** y **probado con motor real en una configuración identificada**. Son capas acumulables, no etiquetas que se puedan intercambiar. Una prueba de adapter falso demuestra el contrato del runtime, no que un modelo real genere imágenes o audio útiles.

Las pruebas de este paquete sólo validan documentos, IDs, dependencias y schemas de referencia. No son resultados de la suite de Faustus. El integrador debe ejecutar las pruebas de aplicación apropiadas en un checkout real. El README del proyecto no debe actualizarse con números de éxito tomados de esta validación documental.[^F15][^F01]

## Matriz mínima de fallos

| Frontera | Inyección de fallo | Resultado esperado |
|---|---|---|
| Plan | Capability ausente o constraint incompatible. | Refusal explícito sin modelo cargado ni llamada generativa. |
| Autorización | Cambiar input, destinatario, modelo o revisión tras aprobar. | Approval invalidado para esa operación. |
| Submit | Engine acepta pero la respuesta se pierde. | Estado incierto; reconciliar, no duplicar el render. |
| Ejecución | Worker reinicia o pierde su lease. | Resultado antiguo no puede aplicarse a otro intento. |
| Cancelación | Completar al mismo tiempo que cancelar. | Transición canónica consistente y efecto externo descrito honestamente. |
| Collect | Disco lleno o descarga interrumpida. | Temporal verificable; no anunciar completed de un archivo inexistente. |
| Artefactos | Dos owners comparten bytes iguales. | Acceso separado a occurrences; no filtración por hash. |
| Edición | UI y agente escriben la misma revisión. | 409/ramas; no último escritor gana. |
| Contexto | Handoff apunta a una versión antigua. | Delta de cambios y revalidación antes de continuar. |
| Recursos | Dos endpoints usan la misma GPU. | Una sola capacidad física y cola/rechazo conservador. |
| Local-only | Plugin o custom node intenta subir un asset. | Bloqueo real de salida o adapter no autorizado, no simple aviso. |
| Import | ZIP path traversal, SVG activo, streams enormes. | Rechazo acotado y ningún efecto fuera del root. |

## Fixtures audiovisuales

Mantener fixtures pequeños y generados de forma controlada: imagen RGBA con cuadrantes conocidos, capa con máscara asimétrica, audio de tonos/silencios, vídeo con contador de frame y puntos de sincronización, sample VFR, audio de voces solapadas autorizado y subtítulos de distintas escrituras. No distribuir grabaciones personales o voces clonadas para probar el sistema por comodidad.

Para el compositor, tests de orden de operaciones, orientación, coordenadas de máscara y comparación preview/export. Para timeline, intervalos racionales, retiming y concatenación con audio de distinta frecuencia. Para captions, solapes, split/join, idioma, líneas vacías, caracteres especiales y roundtrip de formatos soportados.

Una imagen aceptada por el endpoint no prueba visión semántica. Una transcripción con timestamps no prueba que todos estén alineados. Un exit code cero de FFmpeg no prueba que se exportaron las pistas deseadas. Cada criterio técnico debe leer el output pertinente.[^F07][^F09]

## Golden paths prioritarios

**G1. Vídeo local subtitulado.** Importar clip autorizado, transcribir/alinear, corregir una frase, insertar subtítulo, recortar un intervalo y exportar. Verificar duración, texto de revisión final, mapping temporal y reproducción. Cerrar/reabrir la pantalla entre etapas no pierde borradores.

**G2. Imagen por región.** Importar PNG, marcar sujeto/estilo, pintar máscara, planificar inpaint, aceptar una variante y exportar. El recibo registra inputs verdaderos; el original no cambia y undo devuelve exactamente la composición previa.

**G3. Canción editable.** Letra propia, generación con motor configurado, comparación de tomas, aceptación y export. Cuando WP25 esté disponible, regenerar un intervalo manteniendo partes aceptadas y exportar LRC revisado. El test de motor real es opt-in y no se sustituye por un WAV vacío creado por una fixture.

**G4. Continuidad de producción.** Objetivo con varios entregables, un paso fallido y cambio de modelo permitido. Crear handoff, reanudar y producir sólo lo pendiente. No se vuelve a publicar ni facturar un efecto de aceptación desconocida.

**G5. Bundle portable.** Tras WP34, exportar un proyecto con medios, moverlo a otro root y reimportar sin servicios de red. Verificar inventario, hashes, pertenencia y revisiones; un asset faltante impide declarar export completo.

## Métricas de producto

Comparar una tarea fija antes y después: pasos manuales, cambios perdidos, tasa de entregables técnicamente válidos, reintentos evitables, coste conocido/desconocido, cola y latencias por fase. Para calidad artística, registrar evaluadores y preferencias, no convertirlas en una garantía objetiva de mejor modelo.

No usar el número bruto de tests o de proveedores como métrica de madurez. Un provider entry que nunca ejecutó un flujo real no equivale a soporte operativo. Una mejora de velocidad exige comparar condiciones y variabilidad, y no deteriorar calidad o estabilidad.

## Ejecución de pruebas en Faustus

Leer `tests/README.md` y `tests/TESTING_STANDARD.md` del checkout actual. Usar su entorno Python, no el Python global. La guía documenta `tests/run_focus.py` con taxonomía, dry-run y lane rápida. Ejemplos reales de la guía:[^F15]

```bash
./venv/bin/python tests/run_focus.py --dry-run --area services --sub-area cookbook
./venv/bin/python tests/run_focus.py --area security
./venv/bin/python tests/run_focus.py --area services --fast --durations 25
```

En Windows, usar el ejecutable del entorno virtual realmente configurado y traducir las rutas; no asumir que todos los usuarios tienen el mismo nombre de venv. Los tests nuevos deben integrarse con la taxonomía del repositorio, no crear otra colección invisible al runner.

Los comandos npm se ejecutan desde la raíz del checkout que contiene su `package.json`, siguiendo el estándar vigente. La baseline documenta `npm ci`, `npx tsc --noEmit` y `npm run build`. No lanzar varias instalaciones, builds y suites completas simultáneas sobre la máquina de Luis.[^F01][^F14]

El ciclo de un PR es prueba focalizada del cambio, integración pertinente, typecheck/build cuando toque y revisión independiente. La suite global es responsabilidad del integrador en una revisión concreta, sin presentar saltos, xfails añadidos para ocultar fallos o casos simulados como éxito de motores reales.


[^F15]: Faustus · tests/README.md. [Fuente](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/tests/README.md). Consultado el 13 de septiembre de 2026. Lectura 1–145: pruebas focalizadas, taxonomía y disciplina de cambios.

[^F01]: Faustus · README.md. [Fuente](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/README.md). Consultado el 13 de septiembre de 2026. Capacidades generales, arquitectura y licencia declarada AGPL-3.0-or-later; resultados de pruebas reportados por el repositorio, no reproducidos aquí.

[^F07]: Faustus · src/media_capabilities.py. [Fuente](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/media_capabilities.py). Consultado el 13 de septiembre de 2026. Probes baratos; diferencia paquete instalado / modelo preparado; Piper no conectado al proveedor TTS en este módulo.

[^F09]: Faustus · src/model_calibration.py. [Fuente](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/src/model_calibration.py). Consultado el 13 de septiembre de 2026. Lectura 1–200: identidad Ollama por digest, seis probes y distinción anunciado/probado.

[^F14]: Faustus · PENDIENTES.md. [Fuente](https://github.com/Luissalet/Faustus/blob/fa567977a00505b18a0fdf1b6e4d1a83f56270c6/PENDIENTES.md). Consultado el 13 de septiembre de 2026. Incidentes y configuración de hardware reportados; no inventario medido en este trabajo.
