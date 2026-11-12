---
name: unity-speed-feel
description: Ajusta la sensación de velocidad en Unity con cámara, escala, referencias visuales y recorridos medidos. Use when Unity flight or running feels slow, unreadable, unstable or excessively blurred despite correct numerical speed.
version: 1.0.0
category: engineering
tags: [unity, camera, speed, velocidad, flight, gamedev]
status: published
source: imported
---

## When to Use

La velocidad numérica es correcta pero el desplazamiento parece lento, el
escenario no da referencias o los efectos impiden ver. También cuando un giro
produce mareo o una captura presenta manchas que no corresponden al movimiento
real. El objetivo es legibilidad en movimiento, no aumentar la cifra del HUD.

## Procedure

1. Lee escala del mundo, unidades del controlador, velocidad de crucero y
   máximos, dimensiones del personaje y diseño de cámara. Descubre la versión
   del motor y los paquetes con `unity(action:project_profile)`. Usa el FOV
   efectivo de la cámara, no un valor de inspector que otra rutina sobrescribe.
2. Define recorridos comparables: recta, aceleración y giro. En Blood Arrows
   `straight`, `arrow35` y `arrow90` usan el controlador existente. Las marcas
   del laboratorio dan referencias de distancia. Conserva velocidad inicial,
   almas, consigna de entrada, resolución y receta. No añadas enemigos o premios
   para ocultar que el movimiento base se siente mal.
3. Registra por frame velocidad, posición, FOV, rumbo, bank y tiempos de
   simulación y pared. Mide distancia recorrida para contrastar el velocímetro.
   Diferencia velocidad física, multiplicador de viaje y cámara. En Blood Arrows
   la velocidad con rueda es una opción distinta de la velocidad por almas;
   las pruebas deben declarar cuál está activa y restaurar la configuración.
4. Mira primero un vídeo sin modificar parámetros. Anota cuándo se pierde la
   lectura del cuerpo o de la siguiente abertura. Comprueba referencias cercanas
   y lejanas: la perspectiva, escala y paralaje dan velocidad. Un suelo uniforme
   visto desde muy arriba puede parecer estático con una velocidad elevada.
5. Ajusta una familia de efectos cada vez, según el proyecto: curva de FOV,
   distancia y altura de cámara, tiempo de seguimiento, líneas, audio o blur.
   Conserva el juego y las preferencias. Un giro debe reorientar la dirección
   pedida, con cámara que permita entender dónde termina. El temblor de cámara
   no debe confundirse con un modelo que se balancea por el clip.
6. Repite el recorrido con idéntica entrada. Contrasta números y vídeo antes
   y después; comprueba tanto crucero como impulso. En un arnés de paso fijo,
   el tiempo de procesamiento no es una medición de FPS de producto. Conserva
   las duraciones de juego y de pared para que esa diferencia sea visible.

## Pitfalls

El render fuera del bucle normal de HDRP tiene historial temporal propio. Si
se piden capturas cada seis frames y sólo se actualiza ese historial entonces,
los vectores de movimiento pueden producir un borrón artificial. El banco
actualiza el render request en cada frame y guarda una imagen de cada seis.
No elimines motion blur del producto para disimular un error de grabación.
Una captura de la cámara tampoco incluye necesariamente el HUD de overlay:
declara el alcance y revisa ese HUD por la vía normal si se ha modificado.

## Verification

Abre el informe, reproduce los clips y mira inicio, crucero, apuntado y salida.
Comprueba velocidad y rumbo del controlador junto al FOV realmente observado.
Anota resolución, dispositivo gráfico, opciones y fuente Git. Conserva una toma
de día y una toma oscura cuando cambie la lectura del escenario. Si sólo hay
telemetría no declares que la sensación visual está comprobada. Entrega los
defectos observados con instante y archivo, sin sustituirlos por una valoración
general de que «se siente mejor».
