# Contratos de referencia

Los cuatro schemas son especificaciones ilustrativas, no el contrato de una API de Faustus ya desplegada. Los siete ejemplos son fixtures sintéticas sin endpoints de red, precios verificados, medios reales ni pruebas de modelos. No ejecutan nada.

Al implementarlos, reutilizar convenciones y autoridades del HEAD. Los schemas muestran estructura; el runtime debe validar además autorización, pertenencia de occurrences, límites, referencias existentes, consentimiento, revisión de recetas, huellas completas y recursos físicos.

Los tiempos enteros se serializan como strings para preservar precisión entre Python y JavaScript. La razón del clock expresa ticks por segundo; por ejemplo, 24/1. `duration_ticks=120` equivale a cinco segundos en ese clock. Los consumidores deben usar enteros/racionales y conversiones explícitas.

Un transcript admite alineación parcial y no requiere inventar timestamps para cada palabra. Speaker IDs no son identidades humanas verificadas. Un manifest fixture no puede promocionarse a soporte de un modelo real.

`configuration_fingerprint` del ejemplo identifica únicamente su configuración ilustrativa; el fingerprint operacional completo propuesto en el informe combina además identidad del deployment, motor, pesos, template y recursos. `approval.plan_digest` se calcula sobre el plan sin el objeto approval y debe incluir todo el contrato relevante en la implementación real.

Validación local: `python tools/validate_plan.py` desde la raíz del paquete. Requiere Python 3 y jsonschema ya instalado. El script no instala dependencias, no usa redes y no invoca Faustus.
