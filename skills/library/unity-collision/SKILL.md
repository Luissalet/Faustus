---
name: unity-collision
description: Comprueba colisiones de Unity con contactos físicos, recorridos grabados y respuesta medida del controlador. Use when a Unity player bounces, snags on seams, clips through scenery or should slide or stop against a surface.
version: 1.0.0
category: engineering
tags: [unity, physics, collision, colision, rebote, gamedev]
status: published
source: imported
---

## When to Use

Un personaje rebota, se atasca, atraviesa una pared o frena donde debería
deslizar. Sirve para CharacterController y Rigidbody; primero identifica cuál
usa el proyecto. Blood Arrows mueve un CharacterController y decide por código
entre roce y hover: cambiar el material físico no prueba esa decisión.

## Procedure

1. Lee el motor instalado, diseño, capas, máscaras, colliders y controlador.
   Distingue el movimiento de simulación de la corrección de cámara. Consulta
   `unity` → `schema`, `scene`, `object` y `diagnostics` con la identidad exacta
   del proyecto. Describe la respuesta esperada con unidades, no con adjetivos.
2. Construye un caso mínimo sobre el prefab real. Reutiliza el constructor
   existente; `build_playtest` monta suelo, marcas de distancia y dos paredes
   en Blood Arrows. Su receta JSON conserva dimensiones e iluminación. Los
   archivos generados viven en su carpeta propia y no reemplazan escenas de
   juego. Repite la construcción y comprueba que no acumula paredes duplicadas.
3. Incluye aproximación tangencial y frontal. Un choque exactamente paralelo
   puede no tocar nunca la pared: usa un ángulo pequeño que produzca contacto
   sin ser perpendicular. Asegura espacio para acelerar y registra la preparación
   por separado. No invoques directamente el método de choque como única prueba.
4. Ejecuta `run_playtest` con `wall_parallel` y `wall_perpendicular`. Espera el
   resultado con `playtest_status`, sin repetir una petición de efecto incierto.
   Graba entrada, posiciones, velocidad, rumbo, estados y los callbacks reales
   de contacto: objeto, normal y punto. Una pared visible sin callback no prueba
   que haya habido colisión.
5. Proyecta la velocidad sobre normal y tangente. Para Blood Arrows la regla es
   deslizar en el encuentro suave y detenerse en hover en el frontal, sin rebote
   automático. Mide reversión de velocidad tras el primer contacto y desplazamiento
   lateral; separa pequeñas correcciones de penetración de un impulso sostenido.
   Los umbrales están escritos antes de mirar el resultado. Un escape voluntario
   con salto es otro caso, con su propia entrada y su evento.
6. Revisa más de un frame en el vídeo: contacto inicial, respuesta y salida.
   Prueba una junta y una arista si el cambio afecta barridos o selección de
   normales. Repite a la velocidad normal y a la relevante para el fallo;
   no atribuyas una prueba a 90 m/s al extremo de 5.000 m/s. Si un túnel oculta
   el cuerpo, distingue colisión de cámara y colisión del jugador.

## Pitfalls

Cambiar `Forward` o ejecutar un método privado en una sonda puede probar una
fórmula, pero no la detección física. Un cap de velocidad puede esconder un
rebote que todavía cambia el rumbo. Un frame corregido hacia fuera tampoco
demuestra por sí solo rebote de goma. Conserva contacto y secuencia de muestras
para decidir. No elimines colliders ni escondas planos para conseguir una toma
limpia del recorrido que se estaba verificando.

## Verification

Guarda receta, configuración del controlador, consola, contactos y vídeo con
el mismo identificador de recorrido. Comprueba que ambos casos tocaron las
paredes correctas y que se observó la respuesta esperada. Los tests unitarios
cubren cálculo y bordes; la prueba Play cubre la integración física. Abre el
informe con sus capturas y mira el clip completo. Un caso sin contacto, sin
muestras o con errores es un fallo visible, con tarea y reproducción exacta.
