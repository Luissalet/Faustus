---
name: unity-recorded-playtest
description: Ejecuta y revisa recorridos Unity grabados con reglas de diseño, telemetría y fallos reproducibles. Use when delivering or validating a Unity gameplay, animation, collision, camera, model or lighting change.
version: 1.0.0
category: testing
tags: [unity, playtest, verification, pruebas, recorrido, video, gamedev]
status: published
source: imported
---

## When to Use

Antes de entregar un cambio de juego, animación, física, cámara, modelo o luz.
El proceso exige un recorrido grabado y mirado: compilar, describir una prueba
o ver una pose estática no acredita que funcione durante una partida.

## Procedure

1. Lee documentación, diseño, motor y herramientas existentes. Descubre
   `unity(action:schema)` y `project_profile`. Identifica los recorridos que
   ejercitan el cambio y escribe el resultado esperado antes de ejecutarlos.
   Conserva el comportamiento anterior: no rehagas el juego para facilitar
   la prueba y no dupliques el puente, entrada o constructores existentes.
2. Trabaja en una instancia propia del proyecto y un directorio de evidencia
   nuevo. En Blood Arrows el comando único es:
   `python Tools/playtest.py run --unity <Unity.exe> --output <carpeta-nueva>`.
   Arranca Unity en batchmode con gráficos, compila, construye el laboratorio,
   entra en Play, ejecuta recorridos y produce HTML, JSON, PNG y MP4. FFmpeg
   debe estar en PATH para generar los vídeos. No uses `-nographics` para
   afirmar que has comprobado imágenes; `--no-capture` sólo mide números.
3. Si el editor propio ya está abierto, usa `--attach` para ese proyecto exacto.
   Por MCP, las operaciones existentes se amplían con `build_playtest`,
   `run_playtest` y `playtest_status`. Esta última consulta la carpeta que se
   pasó al inicio. `starting` y `completed` son estados del trabajo, no votos
   de que el juego pase las reglas. No reenvíes una mutación tras un timeout:
   consulta identidad, trabajo pendiente y carpeta antes de decidir qué hacer.
4. Ejecuta la selección que toca y la suite completa si el cambio es transversal.
   El banco cubre recta, flecha35°, flecha90°, contacto paralelo, frontal,
   andar, correr y primer túnel de `Course.unity`. La preparación coloca al
   jugador y lo deja estabilizar; el recorrido usa entrada real y no fuerza
   métodos privados de respuesta, estados del Animator o resultados deseados.
5. Abre `index.html`, lee las reglas y reproduce cada `clip.mp4`. Mira el inicio,
   transiciones, contacto y final. Contrasta con `raw.json`: velocidad, almas,
   contactos, eventos, caderas, modelo, clips y huesos. Las capturas HDRP son
   de cámara, sin HUD overlay. El paso fijo facilita repetir física; no es una
   medición de FPS real. Registra aparte tiempos de pared y cualquier limitación.
6. Anota la revisión visual con identificador, archivo e instante y conserva
   el informe original. Un fallo automático no se convierte en aprobado por
   verse bien, ni un rojo visual desaparece porque pasen los números. Para cada
   defecto pendiente crea una tarea con reproducción y evidencia; no cambies
   umbrales, ocultes geometría o sustituyas datos para terminar en verde.

## Pitfalls

La grabación puede fallar aunque la simulación termine: comprueba archivos,
duración, resolución y muestras. No reutilices una carpeta de salida ni llames
«prueba final» a una ejecución de otra revisión. Un clip configurado sin tiempo
y huesos activos no demuestra animación. Un muro sin callback no demuestra
colisión. Una tarea de reparación debe preservar el rojo original.

## Verification

La entrega enlaza informe y manifiesto de integridad, fuente Git, comandos,
resultados automáticos y revisión visual propia. Indica qué falló y sus tareas,
qué escenas y rutas se probaron y qué quedó fuera. Amplía el MCP y documentación
del producto cuando la capacidad sea nueva. Para un build ejecutable usa
`build-player` y `run-player`; no confundas Play del editor con un binario que
se ha compilado y ejecutado. Para otra escena adapta su preparación explícita.
