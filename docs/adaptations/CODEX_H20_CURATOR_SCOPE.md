# H20 — aislamiento de mutaciones del curator

Fecha: 2026-09-30. Adaptación local de la separación de identidad/ámbito
analizada en [Codex, revisión b1e72963c3b71a9265a551e54beff078384efed9](https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9).
No incorpora código de ese repositorio ni exige modelos externos.

## Problema reproducido

El curator selecciona memorias visibles mediante `scoped_items`: una vista
puede incluir filas globales y privadas. Esa visibilidad no autoriza fusionar
ni deprimir registros de otra identidad. Antes de H20, dos sesiones privadas
del mismo propietario/proyecto se fusionaban (texto exacto y similar), y un
anti_pattern privado podía deprimir una regla global del mismo texto.

## Contrato aplicado

La identidad de mutación usa propietario, proyecto, declaración de `scope`,
`session_id` exclusivamente si el ámbito declarado es `session` o `session:*`,
nivel y ventana de vigencia. Una sesión de origen en una memoria de proyecto
no impide consolidar repeticiones del mismo ámbito. Un ámbito ausente hereda
el proyecto/global; no se infiere privacidad desde un identificador de origen.
Declaraciones de sesión inconsistentes se conservan separadas por etiqueta
y por identificador. La clave es una tupla, evitando colisiones de delimitadores.

Deduplicación exacta y aproximada comparten esta identidad y añaden estado;
conflictos usan la misma identidad y el texto original normalizado, excluyendo
estado para poder relacionar active y anti_pattern. Sólo ámbitos idénticos y
ventanas idénticas pueden deprimir una regla. Esto conserva historia incluso
cuando los períodos se solapan. La excepción temporal existente para el inicio
implícito igual a `created_at` sigue permitiendo inserciones ordinarias repetidas.

La fusión de evidencias, la inversión, maduración, poda, visibilidad y consultas
no cambian. No hay migración ni acceso a datos personales.

## Evidencia

`tests/test_memory_curator_scope.py` usa SQLite temporal y desactiva vectores.
Antes del cambio: 7 fallos y 4 controles correctos. Después: 14 casos de H20,
incluyendo sesiones distintas, ámbito proyecto frente a sesión, propietario,
proyecto, nivel, vigencia, privado/global visible, fusión de evidencias,
conflicto legítimo e idempotencia, y sesión usada sólo como origen.

Verificación con el Python del entorno del proyecto:

- H20 inicial + adversariales temporales + memory_engine: 109 pasan.
- H20 ampliado + aislamiento de propietario + adversariales temporales: 30 pasan.

## Límites pendientes

H20 limita a qué identidad se aplica cada mutación; no añade leases, bloqueo
por ámbito, transacción entre selección/fusión/borrado ni coordinación de
curators concurrentes. La atomicidad y recuperación de ejecuciones concurrentes
requieren un lote posterior. La selección visible puede seguir procesando
filas globales: su maduración/poda independiente conserva el comportamiento
anterior. El aislamiento de recuperación por sesión y las identidades de
tombstones están fuera de este cambio.

## Evaluación posterior de carrera (sin implementación)

2026-09-30: SQLite temporal y dos threads curator reprodujeron pérdida total de
dos duplicados del mismo propietario/proyecto. Una lectura anterior a feedback
eligió el superviviente A; otra posterior eligió B. Pausar ambos threads después
de sus saves reales y antes de sus deletes permitió que cada uno borrase el
superviviente del otro. Ambos informes devolvieron deduped=1 y total_active=1;
al reabrir el almacén quedaron cero filas, sin excepciones.

`memory_engine._db` protege y confirma cada operación mediante un RLock y una
conexión corta, pero no hace atómica la selección/fusión/borrado del curator.
`save_item` usa INSERT OR REPLACE. Un primer bloqueo compartido de toda curación
podría serializar curators del mismo proceso; no sería una transacción durable
ni cubriría varios procesos o todos los read/write externos. Una solución
transaccional requiere conexión compartida durante BEGIN IMMEDIATE y diferir
la desindexación hasta commit, además de revisar writers con snapshots previos.
La implementación queda pendiente; esta evaluación no modifica el store.
