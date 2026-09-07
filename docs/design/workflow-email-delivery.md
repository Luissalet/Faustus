# Envío de correo desde workflows

El runtime de producción conecta `deliver` al correo SMTP de Faustus. Es el
mismo runtime para avance HTTP y para continuación en segundo plano. El flujo
no envía al crearse: primero debe iniciarse y luego una persona debe aprobar el
correo concreto. El agente puede solicitar la aprobación, pero no concedérsela.

## Configuración del paso

```json
{
  "id": "send_report",
  "type": "deliver",
  "config": {
    "channel": "email",
    "account_id": "ID_DE_LA_CUENTA",
    "to": ["destinatario@example.com"],
    "subject": "Informe del proyecto",
    "body_from": "results.write_report.text"
  },
  "needs": ["write_report"]
}
```

`body` admite texto literal en lugar de `body_from`. `account_id` es opcional:
si falta se resuelve una cuenta saliente del mismo propietario. El identificador
real, remitente, transporte, destinatarios, asunto, contenido y run/paso quedan
incluidos en la huella de la aprobación. Cambiar cualquiera invalida el permiso.
La tarjeta muestra asunto, remitente, huella y una vista previa del cuerpo;
cuando se recorta, lo indica para revisar el contenido completo del workflow.

El cuerpo y la alternativa HTML se limitan a 1 MiB cada uno, el asunto a 200
caracteres y los destinatarios combinados (`to`, `cc`, `bcc`) a 50. BCC sólo
aparece en el sobre SMTP y en la aprobación del propietario, nunca en las
cabeceras del mensaje. `html` o `html_from` añaden la alternativa HTML; se
conserva el cuerpo de texto como alternativa accesible.

`attachments` admite hasta diez adjuntos, con un total de 10 MiB. Cada entrada
lleva `filename` (nombre sin ruta), `content` o `content_from` (referencia a
un resultado del flujo), `encoding` (`utf-8` por defecto, o `base64` para
binarios) y `mime_type` opcional. Ejemplo:

```json
{"filename": "informe.md", "content_from": "results.write_report.text", "mime_type": "text/markdown"}
```

No se leen rutas arbitrarias del disco. La aprobación incluye nombres, tamaños,
tipos y huellas de los adjuntos, además de todos los destinatarios y del HTML.
Modificar cualquiera de estos datos invalida la aprobación anterior. Los datos
se preparan en memoria antes de autorizar el transporte: éste envía esos bytes.

## Persistencia y respuestas

- Una autorización se consume una sola vez, con dueño comprobado y escritura
  condicional; una carrera perdida no llega al transporte.
- El envío sólo empieza con un claim vigente y un workflow todavía activo.
- SMTP tiene un timeout de 30 segundos y corre fuera del bucle principal.
- La confirmación significa **aceptado por SMTP**, no recepción garantizada
  en la bandeja final. El `Message-ID` queda guardado para localizar el envío.
- Un rechazo parcial conserva listas de destinatarios aceptados/rechazados,
  sin copiar respuestas privadas del servidor. No se presenta como éxito total.
- Si se pierde la respuesta, se registra un efecto incierto y no se reintenta
  automáticamente, incluso si el archivo del workflow afirma `idempotent`.
- Un reinicio no convierte un efecto incierto en una autorización para reenviar.
- Una respuesta humana ya registrada despierta el workflow en segundo plano.
  El worker no concede aprobaciones; una negativa o caducidad detiene el paso.

## Código y comprobaciones

- `src/workflows/delivery.py`: validación, cuenta, permiso exacto y transporte.
- `src/workflows/store.py`: registro de efectos acotado al worker/intento.
- `src/workflows/engine.py`: identidad del paso, conservación de recibos y veto
  al reintento cuando un efecto ya pudo ocurrir.
- `src/workflows/scheduler.py`: observación de respuestas humanas y continuación.
- `routes/email_helpers.py`: un rechazo SMTP parcial deja de ser éxito completo.
- `tests/test_workflow_delivery.py`, `test_workflow_approval_integrity.py`,
  `test_workflow_effect_fencing.py` y `test_email_partial_delivery.py`.

Las pruebas usan SQLite real y transporte SMTP simulado. No se ha enviado correo
a destinatarios reales durante esta implementación.
