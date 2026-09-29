# H22 parcial — router offline usa declaraciones

Fecha: 2026-09-30. Continuación del [piloto scoped](CODEX_H22_SCOPED_CALIBRATION.md)
y [provider policy contextual](CODEX_H22_PROVIDER_POLICY_SCOPE.md), con fuente
original fijada [Codex b1e72963c3b71a9265a551e54beff078384efed9](https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9).
No se repite la revisión upstream ni se ejecutan modelos o requests externos.

## Reproducción y cambio

`score_candidates` recibe nombres de modelos, sin endpoint ni protocolo. Con un
True en el manifiesto global legacy daba +3, explicaba «tool_call probado» y
consideraba satisfecho el requisito aunque no identificaba la conexión que iba
a ejecutarlo. Un False legacy imponía «probado y falla» aun existiendo anuncio
positivo. Esa puntuación confundía confianza en un registro con una capacidad
probada de la ruta real.

El lector pasa a `get_effective_manifest` sin fabricar contexto. Sólo recupera
declaraciones del manifiesto global: un anuncio tools positivo conserva +1 y
«declarado (no probado)»; sin anuncio conserva unknown y la penalización de -1000
si hay un requisito sin satisfacer. True/False legacy quedan intactos en el
almacén, pero no aportan ni bonus probado ni rechazo por probe. Una observación
scoped tampoco se elige sin conocer su endpoint/protocolo.

No se alteran mapas de capacidades, pesos, thresholds, mínimo de capacidades,
latencia, orden determinista, flags ni escalado. En particular JSON mode no tiene
fallback anunciado en el mapa vigente: un True legacy no lo satisface, ni se
inventa una nueva clave de anuncios. Velocidad e historial siguen siendo señales
por nombre de modelo como antes; aislar esas métricas por deployment es otro
alcance. Cambios de modelo/sesión también siguen pendientes.

## Validación

12 pruebas nuevas con almacén temporal real cubren legacy True/False con/sin
anuncio, scoped sin contexto, bonus declarado +1 frente al +3 previo, JSON
desconocido, pesos de velocidad e historial, puerta de latencia, unión de
requisitos, desempate por nombre, preferencia local y escalado con/sin permiso
y bloqueo por privacidad local-only.

Se adaptan siete fixtures de `test_l97_model_router.py` autorizadas: los positivos
de selección usan anuncios tools; la aritmética determinista es 4.6 (1 de anuncio,
2 de velocidad, 1.6 de historial) y el negativo legacy False pasa a unknown.
La unión de mínimo JSON conserva rechazo y ahora explica unknown. Los asserts
de flags, resultado local, explicación, preview sin log y descubrimiento de
modelos permanecen. Las pruebas nuevas mantienen controles legacy separados.

Suite de router, provider policy y calibración scoped: 128 pruebas correctas.
