"""Turn per-frame 3D body landmarks (from a video) into a Blender action on a Unity-style humanoid rig.

  blender -b rigged.blend -P landmarks_to_action.py -- --landmarks pose.json --rig Rig --action Walk \
      --out animated.blend --report motion.json

Input JSON: {"fps": 30, "frames": [{"LEFT_SHOULDER": [x, y, z], ...}, ...]} using MediaPipe's 33-point
names and its world coordinates (metres, origin at the hips, x towards the subject's left in the image,
y down, z away from the camera). Each limb bone is aimed along the line between its two landmarks, parent
first, so the rig keeps its own proportions; the hips follow the pelvis path scaled to the rig's height.
`--smooth N` applies an N-frame moving average to the landmarks first. The report lists, per bone, the
largest angle between the landmark direction and the posed bone (degrees), so a bad frame is visible.
"""
import argparse
import json
import math
import sys

import bpy
from mathutils import Matrix, Vector

# bone -> (from landmark, to landmark); the hips use the midpoint of both hip landmarks.
CHAIN = [
    ("Spine", ("HIPS", "SHOULDERS")),
    ("LeftUpperArm", ("LEFT_SHOULDER", "LEFT_ELBOW")), ("LeftLowerArm", ("LEFT_ELBOW", "LEFT_WRIST")),
    ("RightUpperArm", ("RIGHT_SHOULDER", "RIGHT_ELBOW")), ("RightLowerArm", ("RIGHT_ELBOW", "RIGHT_WRIST")),
    ("LeftUpperLeg", ("LEFT_HIP", "LEFT_KNEE")), ("LeftLowerLeg", ("LEFT_KNEE", "LEFT_ANKLE")),
    ("RightUpperLeg", ("RIGHT_HIP", "RIGHT_KNEE")), ("RightLowerLeg", ("RIGHT_KNEE", "RIGHT_ANKLE")),
]


def to_blender(p):
    """MediaPipe world (x left-in-image, y down, z away) -> Blender (x, y, z up), character facing -Y."""
    return Vector((p[0], p[2], -p[1]))


def point(frame, name):
    if name == "HIPS":
        return (to_blender(frame["LEFT_HIP"]) + to_blender(frame["RIGHT_HIP"])) / 2
    if name == "SHOULDERS":
        return (to_blender(frame["LEFT_SHOULDER"]) + to_blender(frame["RIGHT_SHOULDER"])) / 2
    return to_blender(frame[name])


def smooth(frames, window):
    if window <= 1:
        return frames
    keys, half, out = [k for k, v in frames[0].items() if isinstance(v, list)], window // 2, []
    for i in range(len(frames)):
        lo, hi = max(0, i - half), min(len(frames), i + half + 1)
        out.append({k: [sum(frames[j][k][c] for j in range(lo, hi)) / (hi - lo) for c in range(3)] for k in keys})
    return out


def main():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    p = argparse.ArgumentParser()
    p.add_argument("--landmarks", required=True)
    p.add_argument("--rig", default="Rig")
    p.add_argument("--action", default="Motion")
    p.add_argument("--smooth", type=int, default=1)
    p.add_argument("--out")
    p.add_argument("--report")
    a = p.parse_args(argv)
    data = json.load(open(a.landmarks, encoding="utf-8"))
    frames = smooth(data["frames"], a.smooth)
    scene, rig = bpy.context.scene, bpy.data.objects[a.rig]
    scene.render.fps = int(data.get("fps", 30))
    scene.frame_start, scene.frame_end = 1, len(frames)
    rig.animation_data_create()
    rig.animation_data.action = bpy.data.actions.new(a.action)
    bpy.context.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode="POSE")
    pose = rig.pose.bones
    names = [n for n, _ in CHAIN if n in pose]
    subject_height = max(point(frames[0], "SHOULDERS").z - min(point(frames[0], "LEFT_ANKLE").z, point(frames[0], "RIGHT_ANKLE").z), 1e-3) / .8
    rig_height = max(b.bone.tail_local.z for b in pose) - min(b.bone.head_local.z for b in pose)
    scale = rig_height / subject_height
    hips_rest = pose["Hips"].bone.head_local.copy()
    start_hips = point(frames[0], "HIPS")
    worst = {n: 0.0 for n in names}
    for i, frame in enumerate(frames, 1):
        scene.frame_set(i)
        hips = point(frame, "HIPS")
        pose["Hips"].matrix = Matrix.Translation(hips_rest + (hips - start_hips) * scale) @ pose["Hips"].bone.matrix_local.to_3x3().to_4x4()
        bpy.context.view_layer.update()
        for name, (src, dst) in CHAIN:
            if name not in pose:
                continue
            target = point(frame, dst) - point(frame, src)
            pb = pose[name]
            rest = pb.bone.matrix_local
            rest_dir = (rest.to_3x3() @ Vector((0, 1, 0))).normalized()
            turn = rest_dir.rotation_difference(target.normalized()).to_matrix()
            head = pb.parent.matrix @ (pb.parent.bone.matrix_local.inverted() @ rest.translation)
            pb.matrix = Matrix.Translation(head) @ (turn @ rest.to_3x3()).to_4x4()
            bpy.context.view_layer.update()
            got = (pb.matrix.to_3x3() @ Vector((0, 1, 0))).normalized()
            worst[name] = max(worst[name], math.degrees(got.angle(target.normalized(), 0)))
        for pb in pose:
            pb.keyframe_insert("location", frame=i)
            pb.keyframe_insert("rotation_quaternion" if pb.rotation_mode == "QUATERNION" else "rotation_euler", frame=i)
    bpy.ops.object.mode_set(mode="OBJECT")
    report = {"frames": len(frames), "fps": scene.render.fps, "action": a.action, "scale": round(scale, 4),
              "max_aim_error_degrees": {k: round(v, 3) for k, v in worst.items()}}
    report["ok"] = max(worst.values()) < 1.0
    if a.out:
        bpy.ops.wm.save_as_mainfile(filepath=a.out)
    if a.report:
        json.dump(report, open(a.report, "w", encoding="utf-8"), indent=1)
    print("MOTION", "ok" if report["ok"] else "FAIL", json.dumps(report))
    if not report["ok"]:
        sys.exit(1)


main()
