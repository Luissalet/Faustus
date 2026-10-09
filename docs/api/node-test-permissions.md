# Pruebas Node con una petición natural

En un chat de proyecto se puede pedir «Corrige esto y ejecuta las pruebas» y
usar `node --test --experimental-strip-types tests/genealogy.test.mjs` sin
otorgar permiso general a Bash. Se amplía el matcher existente de pruebas;
las lecturas nativas y las consultas Git ya podían continuar tras leer
archivos. La opción «Trusted folder» permite además editar dentro del
proyecto, manteniendo los permisos de shell y de borrado separados.

La nueva rama requiere una petición del usuario que ordene ejecutar pruebas
y un workspace existente. Acepta `node` o `node.exe`, `--test`,
`--experimental-strip-types` y los reporters incorporados `spec`, `tap` y
`dot`. Los archivos elegidos deben quedar dentro del workspace. Se admiten
patrones de archivo no recursivos, comprobando sus coincidencias y enlaces
simbólicos. Las rutas usan barras `/`. Se conserva el recorte de salida
existente mediante head/tail; un encadenado sólo pasa si cada paso tiene por
sí mismo una autorización nativa, como una lectura o mostrar texto.

Opciones desconocidas, eval/print/require/import/loader, reporter externo,
destinos de reportes, redirecciones y archivos fuera del proyecto mantienen
la aprobación normal. `read_only` y herramientas deshabilitadas siguen
teniendo prioridad en el despacho; esta excepción no activa
`approval_gate_bypassed` ni crea un grant de carpeta.

Las pruebas ejecutan código del proyecto en el equipo. Validar sus
argumentos no confina ese código: puede acceder a otros archivos o a la red.
No se cambia `approve_workspace`, ni se lo requiere para este flujo.

`test_command` es otra capacidad existente: el usuario lo configura en
Project → Settings junto a `run_tests`; el harness lo ejecuta al terminar un
turno con mutaciones. Los comandos custom usan shell en el host. No se
concede a cualquier llamada del agente una exención por parecerse a ese
texto; una futura ejecución de checks configurados debe obtener exactamente
el comando del proyecto autenticado en el servidor, sin argumentos libres
del agente y con controles de herramienta/solo lectura.

Pruebas: `tests/test_node_test_request_gate.py` comprueba el gate armado,
rechazos, solo lectura, enlaces simbólicos y una lectura seguida del test
TypeScript real mediante el despacho nativo, sin modelos ni grants generales.
