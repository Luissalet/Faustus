# Funciones incompletas

Actualizado: 08-09-2026. Sólo carencias comprobadas; borrar la entrada al resolverla.

| Área | Parte que falta | Código |
|---|---|---|
| Scripts de skills en workflows | Asociaciones explícitas de credenciales para scripts que las requieren; actualmente se rechazan. | [skills.py](src/workflows/skills.py), `run` |
| Memoria | Conectar la selección/explicación adicional de `MemoryView`, comprobando solapamientos con Context Engine. La memoria actual sí funciona. | [memory_view.py](src/memory_view.py) |
| State Mirror | Reconstrucción completa y verificable del estado materializado desde el historial. El seguimiento y la resolución de conflictos ya funcionan. | [persistence.py](src/state_mirror/persistence.py), `reducers.py` |

Los pendientes de prueba están en [PENDIENTES.md](PENDIENTES.md).
Los controles de UI aún por completar están en [OBJETIVOS_UI.md](docs/ui/OBJETIVOS_UI.md).
Este índice no convierte propuestas de los documentos de inspiración en nuevas tareas.
