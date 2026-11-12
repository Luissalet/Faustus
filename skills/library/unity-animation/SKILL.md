---
name: unity-animation
description: Diagnostica y modifica animaciones de Unity con evidencia del Animator y de los huesos durante una partida. Use when a Unity clip does not play, a character slides, root motion drifts or a flight pose wobbles.
version: 1.0.0
category: engineering
tags: [unity, animation, animator, animacion, clips, humanoid, gamedev]
status: published
source: imported
---

## When to Use

Un personaje se queda en pose de reposo, un clip asignado no se reproduce,
los pies patinan o el cuerpo deriva o se balancea. También al sustituir un rig,
retocar transiciones o importar un movimiento nuevo. No confundir una hoja de
contacto del editor con una animación que funciona durante el juego.

## Procedure

1. Lee la versión real del motor y los paquetes con `unity`:
   `action:project_profile`, `project` igual a la carpeta absoluta del proyecto.
   Lee el diseño, el controlador del personaje y la configuración del importador.
   Identifica quién mueve la raíz, quién mueve el pivote visual y quién modifica
   los huesos después del Animator. No añadas un segundo controlador.
2. Descubre `unity(action:schema)`. En el editor abierto, consulta `scene` y
   `object` para el jugador, con la identidad exacta devuelta. Comprueba
   `runtimeAnimatorController`, avatar, tipo humanoide o genérico, root motion,
   parámetros, culling y referencias de prefab. Los identificadores de entidad
   son texto; no los conviertas en números. Si el controlador se reconstruye,
   conserva su identidad de asset y vuelve a abrir una escena limpia para
   comprobar referencias guardadas.
3. Formula una prueba de comportamiento: qué entrada inicia el clip, cuánto
   tarda en llegar al estado esperado y qué debe moverse. En Blood Arrows usa
   `run_playtest` con `routes:[walk,run,straight]` y una carpeta nueva. El banco
   usa el prefab existente, `PlayerInputReader`, controlador y Animator reales.
   La preparación puede colocar al personaje; el recorrido se hace con entrada,
   sin llamar `Animator.Play` para forzar el resultado que se quiere comprobar.
4. Durante Play registra estados, transiciones, clips y pesos de cada capa,
   tiempo normalizado y posiciones de huesos. Un clip en la lista del controller
   sólo demuestra configuración. El tiempo debe avanzar y los huesos deben
   cambiar. Para una mezcla, conserva todos los clips significativos y sus pesos.
   Mide después de `LateUpdate` de estabilizadores, IK y corrección de pies.
5. Separa espacios: posición del jugador en mundo, pivote corporal, modelo
   local y caderas relativas al pivote. Andar debe desplazar el controlador,
   sin acumular traslación del modelo en su padre. En vuelo estable, mide amplitud
   lateral, vertical y angular de las caderas, además de manos y pies. Envuelve
   los ángulos al cruzar 360° para no inventar un salto.
6. Cambia una causa concreta: avatar o bindings incorrectos, transición que
   nunca se cumple, root motion incompatible o clip sin hornear en el sitio.
   Mantén las otras entradas, duraciones y escena. Repite el mismo recorrido
   y compara los datos y el clip grabado. Mira inicio, transición y varios ciclos.

## Pitfalls

La previsualización del importador no ejecuta el grafo del juego. Una referencia
de Animator destruida en una escena abierta puede aparentar un fallo del clip.
Un reloj que avanza con huesos inmóviles puede esconder bindings rotos. No
desactives el estabilizador para conseguir amplitud animada: el tronco puede
estar quieto mientras las extremidades siguen vivas. Tampoco amplíes tolerancias
para convertir una regresión observada en verde.

## Verification

Conserva diagnóstico de compilación, recorrido de entrada, JSON por frame,
capturas y vídeo. En Blood Arrows, `Tools/playtest.py report --output <carpeta>`
evalúa las reglas y crea el informe; `playtest_status` sólo informa del estado
de ejecución. Mira los vídeos y anota cualquier artefacto visible. Reabre escena
y prefab para verificar persistencia. Si falla hoy, vincula una tarea al caso y
entrega el rojo con sus datos; no declares que basta con compilar.
