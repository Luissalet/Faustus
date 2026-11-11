---
name: character-consistency-pack
description: Produce a consistent character with a model sheet, references, local training when needed and a portable pack, then use it across images and videos. Use when the same character must look the same in many generated pictures or clips.
version: 1.0.0
category: engineering
tags: [character, consistency, model-sheet, lora, image-generation, video, comfyui, ffmpeg, personaje, consistencia, ilustracion, video]
apps: [prospero]
status: published
source: imported
---

## When to Use

A character (original, mascot or persona) must appear repeatedly with the same face, build, outfit and style. This procedure covers defining it, building references, training only if needed and packaging. It uses a media studio that drives image, video and audio tools but hosts no model of its own. It does not cover likenesses of real people without their permission.

## Procedure

1. Read the app's tool list. Write the character description in fixed fields: age impression, face shape, eyes, hair, skin, build, signature clothing and colours, and what never changes (a scar, a pendant). Decide the style and keep it constant.
2. Generate a model sheet: the same character from front, three-quarter, side and back, with a neutral expression and a few expressions. Review each picture against the description; reject any with a different hair length, an extra finger or drifting colours. A sheet made of mostly accepted images is the foundation.
3. Pick the reference set: six to twelve images that agree with each other, varied in angle, expression and lighting, and with clean backgrounds. Remove duplicates; inconsistency inside the set becomes inconsistency in every output.
4. Test whether references alone are enough. Generate the character in three unfamiliar situations (new outfit, new setting, new angle) and compare with the sheet. If identity holds, stop here; training is costly and can overfit.
5. If identity drifts, train a small local adapter on the reference set. Use captions that describe what changes (pose, clothing) but not what is fixed. Train for as few steps as work, test at several strengths, and look for overfitting: the same pose in every output, the background copied from the training images.
6. Create the portable pack: the description, sheet, references, adapter files with their strength and the settings that produced the best test pictures, plus notes on what failed. Test the pack on a clean setup to see that it reproduces the look.
7. Use the pack for stills and clips. Keep seed, settings and prompts for each production so it can be resumed. For video, check the identity on the first, middle and last frames, and watch for flicker in hair and clothing; re-render the segments that drift.
8. Summarise what is in the pack and its limits (angles that fail, outfits not covered).

## Pitfalls

- A description too vague to decide whether an output is right.
- Training on inconsistent images and getting an average face.
- Over-trained adapters that cannot change pose or clothing.
- Judging video by a single frame.
- Style drift between sessions because settings were not saved.
- Using a real person's photos without consent.

## Verification

- The sheet and references agree on every fixed trait.
- Three new-situation tests were generated and compared.
- The pack reproduces the look on a clean run.
- Video checks were done on several frames, and failures are listed.
