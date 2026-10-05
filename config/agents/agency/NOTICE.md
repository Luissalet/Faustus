# Especialistas de los Hoards

Adaptación de métodos de [Agency Agents](https://github.com/msitarzewski/agency-agents),
revisión `83294689da3832c0a9f223221148c411fd3eacc0`, bajo licencia MIT.
Copyright (c) 2025 AgentLand Contributors. La licencia íntegra está en LICENSE.txt.

Los 38 perfiles contienen procedimientos y contratos de entrega específicos de cada Hoard.
No se han trasladado objetivos numéricos genéricos, afirmaciones de experiencia o memoria,
instaladores ni credenciales de upstream. Son instrucciones para el ejecutor existente,
no motores, servicios nuevos ni mediciones de rendimiento.

`specialists.json` conserva el mapa, las fuentes fijadas por revisión y sus hashes.
Para regenerar desde el checkout exacto: `python scripts/build_hoard_specialists.py <checkout>`.
Los perfiles sólo acceden a las herramientas MCP ya conectadas y permitidas por la sesión.
Pueden modificar datos de esas herramientas cuando el encargo lo autoriza; sus reglas
de archivos locales no se presentan como restricciones del interior de un servidor MCP.
