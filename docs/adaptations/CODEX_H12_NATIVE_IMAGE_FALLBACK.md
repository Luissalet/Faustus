# H12 parcial: historial con imágenes nativas y bloques en fallback

## Diferencia reproducida

Un mensaje sintético con `images[]` nativo y `content=[text: "Compare original and
edited screenshot"]` conserva su instrucción al normalizar para Ollama. Sin
embargo, `vision_routing.strip_images` reemplazaba el contenido por una nota de
imagen omitida al preparar una ruta sin visión: la rama nativa trataba todo content
que no fuera string como texto vacío. Si además había bloques image_url, una
segunda rama sobrescribía las notas de las imágenes nativas; el conteo decía que
habían sido reemplazadas aunque sus notas desaparecieran.

El filtro se ejecuta por candidato desde los wrappers de fallback streaming y
no-stream. Este caso concierne a la proyección de historial; no cambia handlers
de generación/edición, persistencia o interfaz.

## Cambio

Para `content` lista, se conserva una lista: los bloques no imagen permanecen en
su orden, incluyendo tipos opacos que la política anterior ya conservaba. Los
bloques imagen se sustituyen por sus notas en el mismo lugar. Las notas de
`images[]` se añaden al final, porque esa colección nativa no contiene posiciones
intercaladas dentro de content. Se elimina el campo images de la copia destinada
al candidato sin visión. Se preserva el conteo de ambas superficies.

Para content string se mantiene la forma y comportamiento existentes: texto
original seguido de notas. No cambian el predicado de capacidad, la selección de
ruta ni `_replacement_texts`. El historial original no se modifica; una ruta con
visión sigue recibiendo sus adjuntos.

## Evidencia

Seis casos nuevos comprueban imágenes nativas con lista de textos/tipo opaco,
mezcla con bloque imagen, lista vacía, string y los dos wrappers reales de
fallback. El transporte sintético falla con 503 en el candidato con visión y
responde en el candidato sin visión: este último conserva las dos instrucciones,
el bloque opaco y tres notas, mientras el primero y el historial original conservan
las imágenes. No se envían peticiones a modelos ni servicios.

Validación: `venv/Scripts/python.exe -m pytest tests/test_native_image_fallback.py
tests/test_vision_routing.py tests/test_llm_core_ollama.py -q`:
**54 correctas en 8,71 s**, proceso terminado con exit 0. Sin datos personales.

## Fuente y límites

Incremento H12 del [análisis fijado](CODEX_HARNESS_ANALISIS_2026-09-29.md),
referencia original conceptual
[Codex b1e72963](https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9).
Implementación propia sobre rutas actuales; no se repiten revisiones antiguas de
OpenAI/Anthropic, upstream o el radar de proyectos. El shape nativo se comprueba
contra el normalizador Ollama local, no como certificación de un servidor real.

No se preservan píxeles en la proyección del candidato que no puede verlos: se
conservan texto, orden representable y notas, dejando los adjuntos en el historial
original. La asociación de descripciones con attachments usa la política previa;
no se crea un esquema universal de media ni se certifican protocolos nuevos.
Los bloques no imagen permanecen con la misma política de copia superficial;
no se afirma aislamiento de consumidores que muten bloques anidados después.
H12 continúa parcial.
