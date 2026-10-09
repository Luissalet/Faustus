# Referencias seguras en las lecturas Git

Las lecturas de Git usan una lista de argumentos sin shell. Antes de #157,
`git_log` aceptaba una referencia como `--output=...`: Git interpretaba ese
argumento como una opción y escribía un archivo, incluso fuera del proyecto.
Una consulta clasificada como lectura podía tener así efectos de escritura.

Ahora el servicio común rechaza referencias y cursores que empiezan por guion
(también con espacios delante), valores vacíos obligatorios y caracteres NUL,
CR o LF. La validación se aplica al historial, los detalles y archivos de un
commit y su diff. Las herramientas nativas devuelven `git.invalid_revision`
con salida fallida; el API del panel devuelve HTTP 400 con la misma clase.
La referencia especial `all` sigue siendo el selector explícito de todas las
ramas del historial. Nombres de ramas, hashes y expresiones como
`HEAD~1..HEAD` conservan su comportamiento.

En `git_status` y `git_radar` los argumentos del agente se convierten en
límites numéricos o selección de repositorios; no se añaden opciones Git
arbitrarias. Los nombres de archivo de `git_diff` se validan dentro del repo
y se pasan después de `--`, por lo que un nombre parecido a una opción sigue
siendo un nombre de archivo. Se conserva el endurecimiento existente que
desactiva pager, fsmonitor y diff externo. Este cambio no pretende aislar
código de tests ejecutado en el equipo ni ampliar los permisos de escritura.

Pruebas: `tests/test_git_read_ref_safety.py` usa repositorios Git reales y
archivos sintéticos. Cubre el intento de escribir fuera del proyecto mediante
ref, cursor y commit, las dos variantes del diff, los servicios del panel y
sus rutas HTTP, y lecturas normales con paginación, ramas y rangos.
