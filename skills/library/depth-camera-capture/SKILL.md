---
name: depth-camera-capture
description: Capture colour, depth, body tracking and audio from a depth camera, check calibration and alignment, and export geometry for other apps. Use when a depth sensor must be tested, used for a scan or tracking, or its data passed to modelling or interaction tools.
version: 1.0.0
category: engineering
tags: [depth-camera, kinect, point-cloud, body-tracking, calibration, scanning, audio, geometry, camara-profundidad, escaner, calibracion, nube-de-puntos]
apps: [kinect-hoard]
status: published
source: imported
---

## When to Use

A depth camera is connected and you need colour and depth frames, tracked skeletons, a point cloud, a face or body scan, or audio, and you need the data to be correct enough for another program. This procedure covers setup, capture, quality checks and export. It does not make a depth sensor measure what physics prevents: shiny, black or sunlit surfaces return poor depth.

## Procedure

1. Read the app's tool list; there are many tools grouped by purpose (camera, body, scanner, face, audio, geometry, sessions, diagnostics). Start with diagnostics: is the device found, are the drivers and the USB connection adequate, do colour, depth and infrared streams deliver frames at the expected rate? Fix the connection before anything else, since a marginal USB link produces dropped frames that look like software bugs.
2. Set up the scene. Keep the subject within the sensor's working range, avoid direct sunlight and strongly reflective or very dark surfaces, and fix the camera on a stable mount. Record the position of the camera and the surfaces you expect to see.
3. Check alignment between colour and depth: capture a frame with a distinctive edge, such as a box corner, and overlay depth on colour. A shift or a stretched edge means the mapping or calibration is wrong; correct it or recalibrate before collecting more.
4. Check depth quality on a known flat wall or a box of known size: measure the distance and dimensions in the data and compare them with a tape measure. Note the noise at the working distance and the holes at edges. Use these numbers as your error bars.
5. For body tracking, confirm that joints stay stable on a still subject, and note what makes tracking fail (occlusion by furniture, arms crossed, lateral view). For audio, test the microphone array with a known source and observe direction and level.
6. For a live scan, move slowly around the subject or turn the subject slowly, keep it in range, and keep the background static. Prefer a loop that closes the scan; check the closing frame for a seam. Replay the capture and inspect the result from all sides for ghosting, doubled surfaces and moving limbs that were removed or smeared.
7. Export the geometry (point cloud or mesh) with the units and axes stated, and open it in the destination application to check scale and orientation. Where code is meant to be reused in another program, use the documented examples and check that they run on a clean setup.
8. Save the session with its settings, calibration and notes so the capture can be repeated or replayed.

## Pitfalls

- Blaming the software for a weak USB connection.
- Skipping alignment checks and building a scan on shifted colour.
- Scanning in sunlight.
- Trusting depth on glass, mirrors and black cloth.
- Moving the subject or background mid-scan.
- Exports with unstated units that come in a thousand times too large.

## Verification

- All required streams ran at the expected rate for the capture.
- A known object measured within the stated error.
- Colour and depth overlay without visible shift.
- The scan looks consistent from all sides, and defects are listed.
- The exported file opens elsewhere at the correct scale.
