# H22 parcial — provider policy usa evidencia contextual

Fecha: 2026-09-30. Continuación de [calibración scoped](CODEX_H22_SCOPED_CALIBRATION.md),
con fuente original fijada [Codex b1e72963c3b71a9265a551e54beff078384efed9](https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9).
No se repite la revisión upstream, se ejecutan modelos ni se hacen requests externos.

## Fallo reproducido

Con almacén temporal real, un True en el manifiesto global legacy aparecía en
provider policy como «verificado en esta conexión» para otro endpoint. Un False
legacy rechazaba la ruta incluso después de guardar un True scoped exacto para
ese endpoint nativo. Los manifiestos de alternativas recibidos del caller
también podían elevar probes legacy a evidencia de la conexión seleccionada.

## Cambio acotado

La rama de calibración local usa `get_effective_manifest` con el ID real del
endpoint (`connection_id`, `endpoint_id` o `id`) y protocolo derivado únicamente
de una URL nativa explícita `/api` o `/api/chat`. Sin ID o protocolo no adopta
observaciones. Los anuncios globales legacy siguen disponibles como fallback
de declaraciones si no hay evidencia scoped exacta ni anuncios del endpoint.
Un registro scoped exacto con anuncios vacíos no activa ese fallback.

El caller puede aportar `model_digest` o `digest` para el modelo seleccionado;
se acepta sólo un string, sin consulta de tags, búsqueda de blobs ni inferencia
desde otro registro. Sin digest se consulta sólo el ámbito de model_id. Por
ello una calibración guardada bajo digest no se usa si el caller no conoce ese
digest: el resultado puede seguir siendo unknown/claimed. Con digest explícito
coincidente se permite la reutilización entre tags del mismo blob y ámbito.

Las alternativas conservan las listas explícitas de capacidades. Sus
observaciones proceden exclusivamente del registro scoped actual del almacén,
coincidente con modelo/digest, endpoint y protocolo del candidato. Un diccionario
suministrado, aunque copie metadatos de ámbito exactos, no crea observaciones.
El registro actual prevalece sobre el manifiesto suministrado, incluso si éste
declara True frente a un False del almacén o contiene anuncios frente a un
registro actual deliberadamente vacío. Sin registro exacto el manifiesto sólo
aporta anuncios. El caller debe haber enumerado la alternativa; no se descubren
modelos ni conexiones. Para manifiestos
remotos no hay protocolo scoped establecido aquí: también son declaraciones.
La configuración declarada por el caller no se eleva a evidencia de un probe.

Se conservan los requisitos, unknown no bloqueante, claimed y listas explícitas,
los rechazos por evidencia scoped False, privacidad, facturación y flags de
confirmación. No se modifican router, sesiones ni agent_loop.

## Validación y pendientes

27 pruebas nuevas con almacén temporal cubren legacy True/False con/sin anuncios,
scoped True/False, endpoint/protocolo ausente o distinto, digest desconocido o
no string, alias con digest explícito, scoped actual frente a legacy contrario,
alternativas exactas, legacy, modelo distinto, protocolo desconocido y remoto,
metadatos de ámbito copiados sin registro, conflicto con la observación actual
y anuncios vacíos actuales frente a declaraciones suministradas.

Dos fixtures existentes esperaban rechazo de un False legacy sin protocolo:
antes de adaptarlas hubo 56 pruebas correctas y esos dos fallos. Ahora guardan
un False scoped real y URL nativa, conservando sus asserts de rechazo. Los
controles legacy se mantienen por separado en la suite nueva. Suite conjunta
de provider policy, CMP11 y calibración scoped: 106 pruebas correctas.

Router offline y cambios de modelo/sesión siguen pendientes. La disponibilidad
de digest en callers reales es un límite importante: este incremento no añade
I/O para obtenerlo ni cambia sus descriptores. Se mantienen los límites de
endpoint_id sin huella de configuración/versionado del piloto anterior.
