---
name: game-motion-from-video
description: Turn a video of a person moving into a Blender action on a humanoid rig, using local pose estimation and no hosted service. Use when a character needs a reference-based walk, gesture or attack and no motion library clip fits.
version: 1.0.0
category: engineering
tags: [motion-capture, animation, video, pose, mediapipe, blender, retarget, movimiento, captura, gamedev]
status: published
source: imported
---

## When to Use

An animation must follow real movement and you have a clip of someone doing
it. The rig must already follow the Unity-style bone names of
`game-asset-rigging`. Not for cleaning up an existing keyframed animation, and
not when the clip shows several people or a body mostly out of frame.

## Procedure

1. **Check the footage.** One person, whole body in frame, steady camera,
   light that separates the limbs, at most 60 s. Short clips of 2-10 s are
   best; loops need the first and last pose to match, which you check later.
2. **Extract landmarks.** `python extract_landmarks.py --video clip.mp4 --model pose_landmarker_lite.task --out pose.json`
   from `references/`. It needs `mediapipe` and `opencv-python` in a Python
   environment you may install into, and one model file from the MediaPipe
   project that you download once and keep outside the repo. It runs on the
   CPU. The report counts frames without a body; more than 10% of misses means
   the footage, not the script, must change.
3. **Retarget.** `blender -b rigged.blend -P landmarks_to_action.py -- --landmarks pose.json --rig Rig --action Walk --smooth 3 --out animated.blend --report motion.json`.
   Each limb bone is aimed along the line between its two landmarks, parent
   first, so the character keeps its own proportions; the hips follow the
   pelvis path scaled to the rig height. Landmarks are mapped to Blender
   axes (subject facing -Y, Z up, subject's left at +X). Smoothing of 3-5
   frames removes jitter; more blurs fast motion.
4. **Fix what video cannot give.** Foot sliding, feet through the floor,
   hands and head orientation are not measured here: add foot locking and
   hand/head rotation by hand in the action. Trim to the loop and set the
   action's frame range.
5. **Review in motion.** Play the action from front and side next to the
   source clip. Only then export (`game-unity-export`) with the action listed
   explicitly.

## Pitfalls

- A single camera gives noisy depth: forward and back movement is the least
  reliable axis, so expect to adjust it.
- Clothes and capes hide joints; the estimate lands on the visible body.
- The per-bone aim error in the report is a mathematical check of the
  retarget, not of the performance; it will read near zero on a bad clip.
- The script does not handle fingers, head turn, spine twist or root rotation.

## Verification

Run the retarget, save, reopen the `.blend` and read the bone direction at
three frames against the landmark directions. State the clip length, frame
rate, number of frames kept and the missed-frame count. A video you did not
play beside the result is not verified.
