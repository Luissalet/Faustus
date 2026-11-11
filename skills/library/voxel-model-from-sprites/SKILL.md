---
name: voxel-model-from-sprites
description: Turn sprites or artwork into a voxel model by choosing references and their camera views, picking a generation method, judging against the sprites, fixing and exporting. Use when pixel art or pictures must become a 3D voxel object.
version: 1.0.0
category: engineering
tags: [voxel, sprites, pixel-art, 3d, model, export, reference, silhouette, voxeles, sprites, modelado, pixel-art]
apps: [seurat]
status: published
source: imported
---

## When to Use

You have one or several pictures of an object or character, often sprites from different directions, and need a voxel model that matches them. It covers references, generation, judging and export. It does not cover animation rigging, and a single view cannot define the back of an object; hidden sides must be agreed, not invented silently. Axes: x right, y up, z towards the viewer; palette index 0 erases.

## Procedure

1. Read the app's tool list. Gather the pictures and decide for each the view it shows (front, side, back, top, three-quarter) and its scale. Crop to the subject, remove the background and keep pixel art unfiltered; resizing with smoothing destroys colours.
2. Choose the target size in voxels from the finest detail you must keep. One voxel per sprite pixel is a good start; a larger grid only invents detail.
3. Add the references (`reference_add`) with their camera views. Check each reference sits in the right view: a side sprite registered as front gives a model that is thin in the wrong direction.
4. For a quick shape, build instantly from the image (`build_from_image`): inflating or extruding a silhouette, or combining front and side silhouettes. Use this as a draft and as a control against which model-driven results are judged.
5. For a faithful model start a generation (`generate_start`) with the references and a size, and poll its status (`generate_status`). The default method lets the model write a voxel program, look at its renders and fix it in a loop; at maximum effort a round takes minutes, so wait. Fixed rounds or JSON operations exist when you want control over cost.
6. Judge by rendering the result from the same views as the sprites and comparing side by side: silhouette first, then colours, then small features (eyes, hands, outlines). Count mismatching pixels in your head by region and name the worst region, not just "looks off".
7. Fix by targeted edits (`model_ops`) on the wrong region, or rerun the generation with a note on what failed. Prefer small edits to regenerating, which may break parts that were right. Resolve hidden sides by symmetry or a stated choice and say which.
8. Export in the format the destination needs (a voxel file, a mesh, or a print format) and test that it opens, keeps the palette and has the intended orientation and scale.

## Pitfalls

- Wrong view labels on references.
- Anti-aliased or resized sprites that bring in colours with no meaning.
- Making the grid larger than the detail justifies.
- Accepting one angle that matches while the others do not.
- Inflation of flat sprites producing a blob; silhouettes from two views constrain much better.
- Exporting before checking orientation.

## Verification

- Every reference has a correct view, and renders from those views were compared.
- Silhouette matches in every provided view, and remaining differences are listed.
- Hidden sides are marked as chosen, not observed.
- The exported file loads and shows the same model, palette and orientation.
