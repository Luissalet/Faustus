# Imágenes de chat mediante Prospero

Faustus conserva el chat, los adjuntos, los permisos, la galería y el historial.
Prospero ejecuta la generación y las ediciones por instrucciones con sus motores
ComfyUI existentes. Este puente es una primera etapa de consolidación; no elimina
los transportes de imagen anteriores ni migra imágenes existentes.

## Activación

1. Conecta Prospero desde Conectores. Su `app_url` debe ser un origen HTTP local
   con puerto explícito: `127.0.0.1`, `localhost` o `::1`.
2. En Ajustes → IA por defecto → Imágenes, selecciona **Prospero** como estudio.
   El ajuste `image_execution_backend` se guarda por usuario. El valor inicial
   `configured` conserva la elección anterior de modelo y calidad.
3. Activa el permiso habitual de generar imágenes y configura en Prospero el
   motor/modelos ComfyUI. Para editar una referencia hace falta un motor de
   Prospero que admita esa modalidad; seleccionar el puente no instala modelos.

En un chat normal, `generate_image` usa el puente y `edit_image` acepta la acción
`instruction`, una imagen propia de la galería y un `prompt` con los cambios.
Los adjuntos propios incluyen un identificador de galería en su contexto textual,
también para modelos de chat sin visión. Por ejemplo: «ponme un sombrero» o
«haz esta imagen en este estilo». La calidad del cambio depende del motor.
El modo de chat que selecciona directamente un modelo de imagen también respeta
la elección del estudio. El resultado se publica en galería y en el chat, y vuelve
a mostrarse al reabrir el historial.

La acción `inpaint` también usa Prospero cuando es el estudio seleccionado.
Requiere `image_id`, `mask_id` de la misma cuenta y dimensiones, `prompt` y
`strength` entre 0 y 1 (por defecto 0.75). Blanco en la máscara indica la zona
a redibujar y negro la zona a conservar. Usa la plantilla `sdxl_inpaint` ya
existente en Prospero y necesita su checkpoint SDXL configurado. La imagen y
la máscara se importan al proyecto privado de esa sesión. Se conserva el
servicio anterior cuando el estudio seleccionado es `configured`.

## Recuperación sin repetir generación

Cada petición conserva `request_id`, conexión, propietario, sesión, proyecto,
trabajo y asset en `DATA_DIR/prospero_images.db`. Los IDs de herramientas se
derivan del run y de la llamada reales del servidor. Repetir la misma petición
recupera su trabajo; otra llamada intencional puede generar otra imagen igual.

`image_job({"request_id":"…"})` consulta una petición existente de la misma
sesión/usuario y recoge su resultado, sin crear proyecto, importar referencia ni
enviar una nueva generación. Comprueba permisos y conexión actuales. Un resultado
pendiente/incierto lleva `outcome_unknown` y una instrucción de reconciliación,
para evitar que el worker lo convierta en un reintento automático.

Los intentos se guardan antes de los POST. Si se pierde la respuesta al crear
proyecto, importar referencia o enviar trabajo y no se conoce su ID remoto,
se requiere reconciliación manual: no se repite automáticamente. Archivo PNG,
galería y recibo usan recuperación idempotente, no una única transacción conjunta.

## Límites de esta etapa

- El guard de Host/origin de Prospero no es autenticación de usuarios. Faustus
  autoriza y vincula owner/sesión a sus proyectos, trabajos y assets privados.
  Este puente no convierte la API de Prospero en un servicio multiusuario público.
- Entrada PNG/JPEG/WebP y salida PNG, hasta 32 MiB y 40 millones de píxeles.
  Una petición produce una imagen. Descargas confinadas a assets verificados;
  no se usan URLs de resultado suministradas por el motor.
- No se expone cancelación remota: el interrupt de ComfyUI puede afectar otro
  trabajo. Cancelar la espera no implica cancelar ni repetir el trabajo remoto.
- Prospero registra el envío a ComfyUI y retiene los trabajos inciertos al
  reiniciar. La recolección automática del resultado de esos trabajos GPU
  interrumpidos y un journal completo de batches aún no están implementados.
- OpenAI-compatible `images/generations`, `images/edits`, upscale, rembg y
  harmonize anteriores continúan en Faustus. Inpaint anterior se conserva como
  alternativa al estudio Prospero. El traslado de los demás servicios es
  pendiente; no afirmar extracción completa de procesamiento.

## Referencias y comprobación

Implementación propia sobre la API de [Prospero original](https://github.com/Luissalet/ProsperosHoard),
revisión examinada `38fad2b896447da211aa206209d7432b1a0a8525` y protección de
reinicios `ad45e84` (`docs/IMAGE_SUBMISSION_RECOVERY.md` en Prospero).
La propuesta de deduplicar motores procede de la petición del usuario.

Pruebas: SQLite y galería temporales, HTTP/ComfyUI simulado, aislamiento de usuarios,
revocación de permisos, recibos y reinicios, edición de referencia, recuperación
sin POST, rutas de herramientas/admisión y configuración real de Studio.
Comprobación visual de ajustes en escritorio oscuro y móvil claro. No se ha
ejecutado una generación o edición con un modelo/GPU real en el piloto inicial.

Validación real posterior, 30-09-2026: retrato ficticio SDXL y cuatro ediciones
por instrucciones sin máscara con Qwen-Image 2.1 int8 en RTX 5060 Ti de 16 GB:
sombrero rojo, estilo anime, añadir Darth Vader y añadir Keanu Reeves. Todas
terminaron y se publicaron como PNG 1024×1024 en la galería de la cuenta/sesión
de prueba aislada. Inspección visual realizada; mantienen rasgos reconocibles,
pero la difusión redibuja detalles y no promete preservar píxeles o identidad
con exactitud. `image_job` real recuperó las cuatro salidas con los mismos IDs
sin aumentar los cinco jobs (original y cuatro ediciones). Se probaron la
herramienta/adapter, los motores y la recuperación; no una interacción completa
de chat con selección de herramienta por un LLM y navegador en esta corrida.

Se observó una espera de VRAM entre renders por caché DynamicVRAM fuera del
contador de memoria activa de PyTorch. Fue necesario liberar manualmente la
caché del ComfyUI dedicado a la prueba. La admisión de memoria sigue pendiente
de corrección; esto no certifica una secuencia desatendida de múltiples ediciones.
No se alteraron ajustes/datos del usuario ni se descargaron modelos nuevos.
