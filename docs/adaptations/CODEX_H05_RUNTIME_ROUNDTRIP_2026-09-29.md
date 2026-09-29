# H05 parcial: roundtrip del catálogo real de herramientas

Fuente original: [ToolExecutor de OpenAI Codex](https://github.com/openai/codex/blob/b1e72963c3b71a9265a551e54beff078384efed9/codex-rs/tools/src/tool_executor.rs), revisión `b1e72963c3b71a9265a551e54beff078384efed9`, copia local inspeccionada en `D:/LocalAI/inspiration/codex-harness-20260929`. Licencia del proyecto: Apache-2.0. No se ha copiado código Rust. El patrón original reúne especificación, exposición y handler en una entidad; esta entrega aborda únicamente una incompatibilidad previa de Faustus necesaria para avanzar hacia ese patrón.

## Problema y decisión

`ToolRegistry.snapshot()` emitía nombres planos como `bash` y `read_file`, mientras `ToolDescriptor.from_mapping()` exigía nombres con punto. El catálogo real tiene 220 herramientas y cinco carecen de descripción. Por tanto, serializar ese catálogo y leerlo con su contrato no funcionaba por dos razones, no solo por el nombre.

Se conserva el parser predeterminado de spec v2 sin relajar sus reglas. Un perfil explícito `ToolDescriptor.from_runtime_mapping()` admite nombres planos y `mcp__servidor__herramienta`, además de nombres canónicos con punto, y permite una descripción presente pero vacía. No inventa documentación para rellenar los cinco huecos. Comparte con el parser estricto las verificaciones de tipos, claves desconocidas, versión, límites, efectos, retry y schemas objeto.

`ToolRegistry.parse_snapshot()` valida una lista JSON decodificada, rechaza duplicados y devuelve orden estable. `snapshot()` normaliza y valida sus descriptores mediante ese mismo perfil. Las entradas MCP incompatibles se omiten con aviso de log, conservando las válidas y las herramientas internas; no se convierten números o listas en nombres/descripciones válidos.

Los nombres no se renombran ni se traducen a aliases. Una invocación conserva exactamente el nombre y versión del descriptor. Leer un descriptor no registra un handler ni autoriza ejecución.

## Evidencia

- Catálogo real: 220 herramientas internas, más un MCP de prueba con guion, mayúsculas y punto en el nombre.
- Recorrido comprobado: catálogo → JSON → parser runtime → mappings y fingerprints individuales idénticos → fingerprint de catálogo idéntico.
- Referencias en `ToolInvocation` serializadas y parseadas sin modificar nombres ni fingerprints.
- Negativos: nombre/path inválido, MCP incompleto, número como nombre, claves desconocidas, schemas que no son objetos, timeout booleano/string/cero, versión incorrecta, efecto desconocido, scopes mal tipados, retry fuera de rango y descripción inválida.
- El parser spec sigue rechazando el nombre plano y la descripción vacía; la descripción ausente también se rechaza en runtime.
- MCP inválido no elimina los descriptores válidos del catálogo.

**262 pruebas correctas**, ejecutadas con `venv/Scripts/python.exe -m pytest`. Pruebas reproducibles en `tests/test_tool_registry_roundtrip.py`; además se ejecutan las suites de catálogo, fixtures spec v2, rutas de contratos/MCP y autorización de herramientas ante contexto externo. Sin llamadas a herramientas ni conexiones MCP reales.

## Pendiente para cerrar H05/H06

**H05 no está cerrado.** El registro todavía agrega autoridades separadas y declara algunos límites genéricos. No reúne handlers, límites efectivamente aplicados, políticas por argumentos y exposición en una única entidad ejecutable. Se conserva esa deuda explícitamente.

**H06 no está implementado por este cambio.** El snapshot no congela de forma profunda schemas anidados ni captura owner, política y revocaciones por paso. Tampoco fija las herramientas MCP de una llamada en vuelo.

El perfil runtime es opt-in: la salida de catálogo no se convierte por ello en un documento conforme al JSON Schema estricto de spec v2. La importación runtime mantiene los límites actuales (por ejemplo descripción ≤4000 caracteres); un MCP que los supere queda fuera de este catálogo, aunque su servidor pueda ofrecerlo por otros caminos. Unificar esa aceptación con el despacho sigue pendiente.
