# Cobertura de enlaces y límites de evidencia

## Enlaces aportados

Se contabilizan 19 entradas: Faustus, tres enlaces cortos con repositorio nombrado y 15 posts de X. Resolver el repositorio por el nombre aportado no equivale a verificar la redirección. Ninguna fuente inaccesible respalda una feature.

### IN00 — Luissalet/Faustus
[Enlace original](https://github.com/Luissalet/Faustus). Estado: `read_pinned`.
Baseline leída y fijada a commit.

### IN01 — totec448-spec/chat-on-steroids
[Enlace original](https://share.google/66LLYnkE8efi28kxd). Estado: `shortlink_unresolved_named_repository_read`.
Repositorio identificado por el texto aportado, no por una redirección comprobada.

### IN02 — buluma/steroid-chat
[Enlace original](https://share.google/aMvMPyzebopno3VQx). Estado: `shortlink_unresolved_named_repository_read`.
Repositorio identificado por el texto aportado, no por una redirección comprobada.

### IN03 — calin-ciobanu/killer_chatgpt_prompts
[Enlace original](https://share.google/cupil5Lfrbz1OXBc6). Estado: `shortlink_unresolved_named_repository_read`.
Repositorio identificado por el texto aportado, no por una redirección comprobada.

### X01 — Post no recuperado
[Enlace original](https://x.com/MikuBTC/status/2096925496323813778). Estado: `inaccessible`.
Sin contenido recuperable; no implica que el post no exista. No se identifica el proyecto mostrado.

### X02 — Post no recuperado
[Enlace original](https://x.com/nicos_ai/status/2098764217947914722). Estado: `inaccessible`.
Sin contenido recuperable; no implica que el post no exista. No se identifica el proyecto mostrado.

### X03 — Post no recuperado
[Enlace original](https://x.com/zaynmcps/status/2098721110883700738). Estado: `inaccessible`.
Sin contenido recuperable; no implica que el post no exista. No se identifica el proyecto mostrado.

### X04 — Post no recuperado
[Enlace original](https://x.com/alextalksai/status/2098849604582424891). Estado: `inaccessible`.
Sin contenido recuperable; no implica que el post no exista. No se identifica el proyecto mostrado.

### X05 — Post no recuperado
[Enlace original](https://x.com/liambraus/status/2098721693216714754). Estado: `inaccessible`.
Sin contenido recuperable; no implica que el post no exista. No se identifica el proyecto mostrado.

### X06 — Post no recuperado
[Enlace original](https://x.com/midudev/status/2098427672439222506). Estado: `inaccessible`.
Sin contenido recuperable; no implica que el post no exista. No se identifica el proyecto mostrado.

### X07 — Post no recuperado
[Enlace original](https://x.com/Antonio_RodriIA/status/2098436432402477334). Estado: `inaccessible`.
Sin contenido recuperable; no implica que el post no exista. No se identifica el proyecto mostrado.

### X08 — Post no recuperado
[Enlace original](https://x.com/shanyanggm/status/2098941338297458746). Estado: `inaccessible`.
Sin contenido recuperable; no implica que el post no exista. No se identifica el proyecto mostrado.

### X09 — Post no recuperado
[Enlace original](https://x.com/Lucy_love_AI/status/2098665675229434101). Estado: `inaccessible`.
Sin contenido recuperable; no implica que el post no exista. No se identifica el proyecto mostrado.

### X10 — Post no recuperado
[Enlace original](https://x.com/benyuls/status/2098406287294001650). Estado: `inaccessible`.
Sin contenido recuperable; no implica que el post no exista. No se identifica el proyecto mostrado.

### X11 — Post no recuperado
[Enlace original](https://x.com/RoundtableSpace/status/2098754751726784783). Estado: `inaccessible`.
Sin contenido recuperable; no implica que el post no exista. No se identifica el proyecto mostrado.

### X12 — Post no recuperado
[Enlace original](https://x.com/servasyy_ai/status/2098953042557272240). Estado: `inaccessible`.
Sin contenido recuperable; no implica que el post no exista. No se identifica el proyecto mostrado.

### X13 — Post no recuperado
[Enlace original](https://x.com/Ryrenz/status/2098828993478754517). Estado: `inaccessible`.
Sin contenido recuperable; no implica que el post no exista. No se identifica el proyecto mostrado.

### X14 — Post no recuperado
[Enlace original](https://x.com/boniusex/status/2098681014981964233). Estado: `inaccessible`.
Sin contenido recuperable; no implica que el post no exista. No se identifica el proyecto mostrado.

### X15 — Post no recuperado
[Enlace original](https://x.com/smratitiwa86867/status/2099031869287272502). Estado: `inaccessible`.
Sin contenido recuperable; no implica que el post no exista. No se identifica el proyecto mostrado.

## Qué sigue abierto

Los 15 posts pueden contener herramientas o técnicas importantes que todavía no se han identificado. La cobertura de esa parte de la petición sigue incompleta; no se debe afirmar que el catálogo agotó todas sus aportaciones. WP42 permite incorporarlas como un delta de investigación sin bloquear decisiones independientes ya fundadas.

No se realizaron pruebas en vivo de Faustus, benchmarking de modelos, pruebas con cuentas de proveedores, comprobaciones de micrófono ni ejecución de modelos. No se revisó todo el código de cada donante ni todas sus licencias. Las observaciones estáticas se transforman en candidatos de prueba, no en diagnósticos definitivos.

## Cómo completar una fuente sin contaminar el plan

Registrar URL del post y contenido realmente leído; extraer enlace canónico; verificar autor/repositorio/revisión; identificar función de código o documentación pertinente; distinguir implementado frente a roadmap; revisar licencia; comparar con una feature existente y proponer profundización o feature nueva. Conservar el estado anterior y la fecha de resolución. Un enlace sólo promocional sin código/API puede inspirar producto, pero no justificar una dependencia de implementación.

## Alcance de la validación adjunta

`tools/validate_plan.py` comprueba IDs, enlaces internos, dependencias de paquetes, schemas y ejemplos. No ejecuta Faustus ni redes. El fichero de verificación generado describe únicamente esas comprobaciones. Los gates de aplicación real están especificados en el plan de pruebas y deben ejecutarse en un checkout/entorno configurado.

