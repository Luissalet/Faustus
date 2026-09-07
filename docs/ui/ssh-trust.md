# Confianza SSH desde Studio

Implementado el 07-09-2026 en Cookbook → Servers, debajo de cada servidor remoto
guardado: **Verificar la identidad del servidor remoto**. No modifica las claves
del usuario ni acepta identidades por primera conexión.

1. Inspeccionar muestra huellas ofrecidas y guardadas, sin escribir confianza.
2. Un servidor nuevo requiere pegar una huella SHA256 obtenida por un canal
   independiente. Sólo una coincidencia habilita la acción humana de confiar.
3. Una clave cambiada bloquea el emparejamiento. Retirar la confianza requiere otra
   confirmación; después hay que inspeccionar y verificar otra vez. No hay reemplazo
   automático ni opción de desactivar la comprobación SSH.

El formulario distingue la **identidad del servidor** de la clave de acceso de
Faustus. Los cambios de dirección/puerto sin guardar deshabilitan estas operaciones.
Las respuestas tardías no sobreviven al desmontaje; hay plazo máximo, estados ES/EN
y recuperación explícita ante una modificación no confirmada.

Código: `studio/src/screens/cookbook/SshTrust.tsx`, `Servers.tsx`,
`studio/src/adapters/ssh-trust.ts`, `routes/cookbook_routes.py`.
Se reutilizan `/api/cookbook/ssh/fingerprint`, `/pair` y `/unpair` y el almacén
existente. Pair/unpair ahora exigen humano administrador y mismo origen, también
cuando el modelo intenta entrar con el token interno. La prueba SSH limpia su
proceso al agotar el tiempo o cancelar.

Validación: 47 pruebas Python de confianza/rutas, contrato JavaScript del adaptador,
TypeScript; revisión del componente real a 1920 y 390 px con transporte sintético,
huella no coincidente deshabilitada, emparejamiento simulado, clave cambiada sin
aceptación y cancelación del diálogo de revocación. No se contactó ni emparejó ningún
servidor real. La verificación contra un SSH real sigue siendo una prueba aparte.

`studio/checks/ssh-trust-preview.tsx` es sólo una fixture local con respuestas
sintéticas; no se importa desde la aplicación ni modifica confianza.
