# H22: coherencia al resolver credenciales de endpoint

El resolver captura la configuración privada del endpoint A antes de consultar credenciales refrescables. Una nueva sesión SQLite consulta después el registro actual únicamente para comparar: id, connection_revision, base_url, api_key, provider_auth_id, endpoint_kind, owner e is_enabled. No adopta la configuración B ni reintenta credenciales.

Si cambió, desapareció o no puede verificarse, `EndpointConfigurationChanged` (RuntimeError) falla con un mensaje fijo sin valores secretos. También verifica tras un error del resolver de credenciales: una mutación detectada prevalece sobre ese error; sin mutación, conserva el comportamiento legacy del error original. Los catches internos y externos relevantes de endpoint_resolver propagan la excepción, incluidas las cadenas y la identificación del descriptor. Worker, visión, búsqueda de modelos y fanout no convierten esta excepción en selección del coordinador, de otro endpoint o del fallback de entorno.

La reproducción con SQLite temporal y resolver OAuth sintético muestra que, al retirar sólo el guard, se obtiene URL/token B junto a connection_revision A. Las regresiones prueban ambos resolvers públicos, cadenas, cambios de configuración, borrado/deshabilitación/cambio de owner, fallo de credenciales concurrente, renovación normal sin cambio de identidad y una nueva llamada explícita que captura B. Los probes de consumidores no hacen requests a modelos ni proveedores.

## Límites

Este control estrecha la carrera observada entre la lectura de A y la resolución de credenciales en otra sesión. No es una transacción distribuida ni un lease: una mutación posterior a la comparación aún es posible. Depende de la rotación de revisiones de endpoints vinculados a auth en el mismo commit de cambios de identidad de credenciales. La renovación ordinaria de access tokens conserva identidad y se acepta.

Endpoints con api_key directa, sin provider_auth_id y sin esta consulta intercalada de credenciales, mantienen su contrato. No se amplía el guard a otros productores HTTP, catálogos/modelos, plantillas o cambios posteriores al snapshot. Los callers ajenos a los cuatro suppressors corregidos pueden tener tratamiento propio de errores; no se afirma eliminación universal de TOCTOU.

Fuente de referencia Codex fijada por el análisis existente: b1e72963c3b71a9265a551e54beff078384efed9. Sin nueva exploración upstream, modelos reales, tráfico externo ni cambios de permisos.

## Evidencia de validación

Freeze final: suite nueva 63 passed en 3.00s; selección final resolver/modelos/headers/URL, owner, controles de fanout y task endpoint junto a nueva suite: 116 passed en 8.24s. Una selección amplia previa (con 49 casos nuevos antes de las últimas regresiones) terminó 254 passed en 97.98s, incluyendo visión, foreground routing y auth ChatGPT; no se presenta como validación del freeze final. Diff check limpio en las cinco fuentes.

## Protección de refresh concurrente (24f3685c)

ProviderAuthSession incorpora credential_revision opaca y versionado optimista
ORM. Un refresh iniciado en A ya no sobrescribe una reconexión B, incluso cuando
la reconexión conserva los mismos tokens. El conflicto revierte la escritura y
se propaga como error tipado/409. Endpoint resolver conserva esa incertidumbre
sin fallback de entorno, incluido el conflicto entre dos refresh ordinarios
que mantienen estable connection_revision. No adopta las credenciales nuevas
como resultado del intento antiguo. SQL externo sigue fuera de esta garantía.

Validación integrada del coordinador: 102 correctas en5,92s, incluyendo8 CAS,
14 invalidación vinculada,72 coherencia y8 research. Fixtures research usan
endpoints SQLite reales (ee4b0411). Son pruebas de concurrencia/lógica sin GPU;
no requests a modelos/proveedores. Fuente original del análisis:
https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9.
