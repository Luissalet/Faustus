# Three.js and React Three Fiber

Resolve versions from the lockfile. Check the installed packages' peer dependency
ranges before adding Drei, Rapier or postprocessing; React/Fiber major versions
are coupled. Use the existing renderer and game loop rather than creating a
second canvas or duplicate animation loop for one effect.

Use Drei helpers when they replace camera, environment or loader plumbing.
Use Rapier for rigid bodies, colliders and joints with a fixed simulation step;
do not let frame-rate-dependent React state drive every physics tick. Dispose
geometries, materials, textures and effects when replacing a scene or unmounting.
Apply postprocessing and particle effects within a measured frame budget, testing
the weakest supported device. Three.quarks runtime and its hosted editor/services
have separate terms; a runtime dependency does not grant a hosted service.

For NPCs, separate perception, decision state and movement. Yuka's steering and
pathfinding patterns need collision/level integration, not just a moving mesh.
Keep authoritative multiplayer simulation on the local server when using Colyseus.
Verify with two actual clients, reconnect and adverse latency; synchronization
alone does not compensate for fast projectile latency. Local room hosting does
not require a cloud service. Persistent player data needs an explicit schema.
