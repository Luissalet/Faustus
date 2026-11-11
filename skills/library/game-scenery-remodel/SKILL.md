---
name: game-scenery-remodel
description: Rebuild low-poly game scenery (buildings, towers, ruins, props) as a clean-quad model with the detail its textures only draw, piece by piece, using the remodelling studio's tools. Use when a game model must become a high-resolution model for printing or rendering and its topology matters.
version: 1.0.0
category: engineering
tags: [3d, blender, mesh, modelado, malla, remodel, retopology, quads, game-assets, diorama, impresion-3d, texturas]
status: published
source: imported
---

## When to Use

A model ripped or exported from a game (OBJ with MTL, DAE, GLB/glTF, or a zip
of them) has to become a detailed model with good topology: real roof tiles,
panels, mouldings, rounded statues. The original stays the reference for
sizes and positions; nothing is sculpted by displacing a dense triangle mesh
unless the person asks for that route. Making the result watertight or
printable is a separate job: do not close, thicken or fuse meshes here unless
asked.

## Procedure

1. Find and open the model. `library_scan` lists what is in the person's
   folders (page through it; a pack often holds several models and the
   detailed one is not always the one named after the place). `project_create`
   opens it. Set the print size with `project_set` before anything else:
   every measure after that is in millimetres at that size.
2. Look before deciding. `model_look` with `result: before` from two or three
   angles, and zoomed with `region` on anything small (finials, statues,
   corner ornaments, brackets). Note what the textures draw that the mesh does
   not have: tiles, panel frames, bands, feathers, joints.
3. Read the plan. `remodel_plan` takes the model apart into connected pieces
   and groups equal shapes into types. For each type it gives the size, the
   materials, the kind it guessed and `allowed`: the only builders that shape
   fits. A kind outside `allowed` is refused; do not argue with it, the piece
   really is not that shape.
4. Choose a kind per type, largest and most visible first:
   - `roof`: a hipped roof seen whole. Curved eaves in the original are
     measured and reused; give `concave` or `corner_lift` only to override.
   - `tiled`: a roof with more to it (platform, hatch, dormer). It keeps its
     shape and gets tiles down its slopes.
   - `body`: a storey with posts, beams and one panel, lattice or door per bay.
   - `lathe`: anything turned. `sides` makes a prism (a hexagonal ring stays
     hexagonal); a ring keeps its hole; `flutes` for column shafts.
   - `sweep`: beams, hip ridges, rails, straight or curved.
   - `block`: slabs, plinths, steps. `stairs`: a ramp whose texture draws steps.
   - `organic`: a figure the game made of flat facets (statue, animal, carved
     ornament). It is rounded into small quads and takes its texture's drawing.
     Never for crystals, rock or anything meant to keep hard edges.
   - `keep`: everything irregular with hard edges (rock, terrain). `drop`:
     shadow planes, glows, sky cards.
   Send the choices with `remodel_set`. Leave a type alone when the guess is
   already right.
5. Build with `remodel_start` and wait with `job_status`. Read the counts it
   returns: quads, triangles, n-gons, and how many pieces ended in each kind.
6. Compare. `model_look` with `result: remodel` and `compare: true`, same
   angles as step 2, then zoomed on every small feature you noted. Check, in
   this order: silhouette and proportions against the original; pieces that
   vanished or were flattened; drawing from the texture that reads as noise
   (painted shading turned into relief) rather than as the motif.
7. Correct and rebuild. Typical corrections: a type whose texture is only
   shading gets `emboss: 0`; a figure that lost its edges goes back to `keep`;
   a roof whose tiles are too coarse or too fine gets `tiles` changed; a prism
   built round gets `sides`. Rebuild only after a batch of corrections, not
   after each one.
8. Deliver. `result_export` with `result: remodel` writes the `.blend` (one
   object per piece, collections by kind, bevels as live modifiers) and the
   quad OBJ next to the source. Tell the person what stayed as the original
   and why.

`remodel_start` with `direct: true` hands steps 4 to 7 to the studio's own
vision model. Use it for a first pass on a model with many types, then review
its choices in the plan as in step 6.

## Pitfalls

- Game models leave out faces nobody sees (the back of a roof, the far side
  of a ring). A square piece is completed by symmetry; anything else keeps
  the gap. Say so instead of hiding it.
- A single baked texture for the whole scene (an atlas) carries no reliable
  drawing per piece: its pieces are not embossed and figures in it are not
  detected. Choose `organic` by hand for those.
- Sloping ground and stepped terrain look like roofs and blocks. Keep them
  `keep`; the plan already refuses the builders that would flatten them.
- Wings, flags and leaves are single sheets in the game and stay sheets.
  Do not promise thickness.
- A high quad count is not quality. Compare pictures, not numbers.

## Verification

- The comparison pictures of step 6 exist for the whole model and for each
  small feature, and each difference from the original is either intended or
  reported.
- `remodel_start` returned without an error and its n-gon count is zero or
  explained.
- No type sits in a kind that removes detail the original had (a ring built
  solid, a figure dropped, terrain turned into a block).
- The exported `.blend` and OBJ paths are in the final answer, with the
  pieces that stayed as the original named.
