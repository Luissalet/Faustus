---
name: shared-project-files
description: Work on files of a shared project that several apps open at the same paths, handling revisions and derived results without rewriting originals. Use when more than one tool or person edits the same project folder and nothing may be silently overwritten.
version: 1.0.0
category: engineering
tags: [shared-files, projects, revisions, derived-results, cache, anchors, collaboration, files, proyectos, archivos, revisiones, colaboracion]
apps: [atlas]
status: published
source: imported
---

## When to Use

A project holds live files in one place and several apps, assistants or people open exactly those paths. This procedure covers reading and changing such files, tracking revisions, creating results derived from them and pointing at exact places in a file. The storage is local; membership in a project is cooperative and the operating system does not enforce who may touch what, so your care is the protection.

## Procedure

1. Read the app's tool list. Identify the project, its members and the native folder of each app. Work inside the folder that belongs to your task, and never move an existing project or folder on your own initiative.
2. Before using a file, check its current revision and who changed it last. If another app is working on it, read its task context and avoid editing the same file at once; ask or choose another file.
3. Treat originals as read-only inputs. When you need to transform one (convert, summarise, extract, clean), write the result to a new file or as a derived result, never over the source. Name outputs so that the source and the operation can be read from the name.
4. When you must change a file that others rely on, create a new revision and describe the change in one line. Check what the previous revision was so it can be recovered. Do not delete revisions to save space.
5. For derived results, record the exact source hashes and the recipe: the steps, parameters, and the model and its revision if a model was involved. A result without these cannot be reused safely, because the same inputs with a different recipe give a different output. Reuse a derived result only if both source hashes and recipe match.
6. To refer to a place in a file (a text span, spreadsheet cells, a PDF page and rectangle, a code symbol, an image region, a time range, a part of a 3D model), create an anchor pinned to that revision. After a later revision, check that the anchor still resolves to the same content; if it moved or vanished, say so instead of reusing the old position.
7. Record task context so the next app or person picks up where you stopped: what was done, which files were read, written and derived, and what remains.
8. Report the revisions created, derived results made, anchors added and any file you decided not to touch.

## Pitfalls

- Overwriting an original because the new file "is better".
- Reusing a derived result after its source changed.
- Anchors treated as permanent across revisions.
- Assuming project membership prevents conflicting edits.
- Deleting old revisions or caches to tidy up.
- Moving a project folder, which breaks the paths other apps hold.

## Verification

- Original files have unchanged hashes.
- Every new or changed file has a revision note.
- Every derived result lists source hashes and a recipe.
- Anchors you rely on resolve on the current revision.
- A task context entry exists for the work done.
