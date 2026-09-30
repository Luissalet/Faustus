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

La acción `harmonize` (integrar un recorte o suavizar un montaje) también pasa
por Prospero con el estudio seleccionado: es el img2img SDXL existente
(`operation=img2img`) con `strength` como fracción redibujada (0.4 por defecto)
y un prompt de armonización por defecto si no se da otro.

## Recuperación sin repetir generación

Cada petición conserva `request_id`, conexión, propietario, sesión, proyecto,
trabajo y asset en `DATA_DIR/prospero_images.db`. Los IDs de herramientas se
derivan del run y de la llamada reales del servidor. Repetir la misma petición
recupera su trabajo; otra llamada intencional puede generar otra imagen igual.

`image_job({"request_id":"…"})` consulta una petición existente de la misma
sesión/usuario y recoge su resultado, sin crear proyecto, importar referencia ni
enviar una nueva generación. `image_job({"request_id":"…","action":"cancel"})`
cancela solo ese trabajo: en cola se cancela al momento; en ejecución queda
`cancel_requested` y Prospero lo para en su siguiente punto de control; si ya
había terminado, se recoge la imagen en vez de perderla. Comprueba permisos y conexión actuales. Un resultado
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
- La cancelación es acotada: Prospero lee la cola de ComfyUI y solo interrumpe
  el prompt de ese trabajo si es el que se ejecuta, o lo saca de la cola si
  espera; nunca el interrupt global. Parar el turno del chat no cancela el
  trabajo remoto (la imagen se recoge luego); cancelar es explícito.
- Al arrancar, Faustus recoge los trabajos que el proceso anterior dejó en
  `waiting` con job conocido (`reconcile_pending_images`), bajo su propio
  propietario, sesión y conexión; nunca reenvía. Los envíos sin job conocido
  siguen necesitando reconciliación manual. No hay journal completo de batches.
- OpenAI-compatible `images/generations`, `images/edits`, upscale y rembg
  anteriores continúan en Faustus. BLOQUEADO: upscale/rembg en Prospero
  necesitan modelos que no están instalados (RealESRGAN y un modelo de recorte);
  su descarga requiere autorización. Inpaint anterior se conserva como
  alternativa al estudio Prospero.

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

Corrección posterior: Prospero `1fefe06` mide el acelerador primario y añade
admisión explícita para un endpoint dedicado fijado en `backend.json`:
`{"comfy":{"url":"http://127.0.0.1:8189","manage_memory":true}}`.
Exige capacidad suficiente y cola vacía del mismo endpoint; no inventa memoria
libre ni envía `/free`. Esa opción por sí sola evitó la espera, pero la segunda
edición falló por OOM con ComfyUI 0.37/aimdo 0.5.5.

La secuencia real posterior arrancó el ComfyUI propio con
`--disable-smart-memory`, manteniendo DynamicVRAM activo. Sombrero azul y anime
terminaron consecutivamente en 98,03 y 81,96 s, sin liberación manual ni `/free`.
Ambos PNG fueron inspeccionados; jobs done con recibos/prompt_id y galería propia.
Antes del segundo render había 15.844.507.644 bytes libres: esa secuencia no
necesitó la excepción de memoria baja. La admisión tiene pruebas sintéticas;
no es garantía universal contra OOM. No se cambió el arranque/configuración de
servicios personales. Los servicios propios de QA quedaron cerrados.
454 pruebas completas Prospero correctas (220,38 s), build correcto y 26 pruebas
focales del coordinador correctas (4,04 s). Fuente y límites detallados en
[Prospero](https://github.com/Luissalet/ProsperosHoard),
`docs/COMFY_MEMORY_ADMISSION.md`. Evidencia: directorio QA citado anteriormente.

### Prueba funcional con modelos y GPU, 30-09-2026

El bucle stream_agent_loop real recibió herramientas nativas con un endpoint QA
persistido y Qwen2.5-3B-Instruct q8_0 en GPU3. El modelo eligió edit_image sin
máscara; dispatcher, Prospero y Comfy/Qwen-Image 2.1 en GPU1 completaron el
trabajo y el evento generated_image devolvió la URL correcta de la galería propia.
Total85,54s; herramienta81,90s. Job job_01M3R1B55R8G2W7Y5YX31ZSCRA,
galería fb667e0e-f586-4d59-a380-577b4e4698bf: retrato con sombrero verde,
PNG1024² inspeccionado. GPU1 pico100%. GPU3 pico92% corresponde al piloto
native anterior; el observador del fullstream empezó tarde y no capturó ese pico.
No se certificó navegador/UI. El primer intento sin herramientas nativas había
inventado un GalleryImageID, rechazado por ownership sin render.

Otra selección nativa real ejecutó inpaint SDXL con máscara propia en27,16s,
GPU1 pico100%, galería b3201cd9-1904-4394-964d-f9aefbeb0867. Original SHA
sin cambios. Sombrero azul estilizado con costuras visibles; no certificar calidad
fotográfica ni fullstream/UIinpaint. Evidencia verificable en
D:/LocalAI/tmp/prospero-live-edit-20260930/gpu-agent-1744f95029764b88bfbaeb4e2f04fcf5
y gpu-inpaint-4cf651186279480b86284bc988fbcc3c, incluidos PNG, eventos,
recibos y telemetría. No mocks de LLM, herramienta ni motor de imágenes.

Límite pendiente: el modelo inventó un host en el enlace de su respuesta textual,
aunque herramienta y SSE devolvieron correctamente /api/generated-image/{id}.png.
Dos renders Qwen consecutivos y SDXL terminaron sin /free ni liberación manual
con el perfil dedicado --disable-smart-memory. No garantía universal de memoria.
Servicios QA cerrados después; configuraciones/datos personales preservados.

### Chat completo en navegador, cancelación y reinicio, 30-09-2026

Studio real con Qwen2.5-3B q8, Prospero y ComfyUI 0.37 con Qwen-Image 2.1:
adjunto arrastrado, petición en castellano, aprobación, render de ~80 s y
resultado en directo y al reabrir. Se corrigieron nueve fallos (entrega SSE del
evento `generated_image`, IDs UUID de galería, restauración con `exit_code`
nulo, enlaces con host inventado, ID de galería del adjunto sin visión, aviso de
visión que activaba herramientas de administración, `edit_image` diferido al
catálogo, render duplicado en rondas posteriores y afirmación de edición sin
herramienta). Commits `a756c1f5` y `3cb3f911`; armonizar `bcfe6e8e`.

Cancelación acotada real: con un prompt ajeno ejecutándose en el mismo ComfyUI
y el de Faustus esperando detrás, `cancel_image` dejó la cola sin el prompt
propio; el ajeno terminó con `execution_success` y el trabajo quedó `cancelled`.
Recolección tras reinicio real: petición enviada, Faustus reiniciado a mitad de
render, y el arranque registró `reconciled studio image receipts: checked 1,
done 1`; la imagen apareció en Biblioteca → Imágenes (comprobado en navegador)
y la cancelada no. Prospero: `tests/test_scoped_comfy_cancel.py`; Faustus: 8
pruebas nuevas de cancelación/reconciliación y de la acción de herramienta.
