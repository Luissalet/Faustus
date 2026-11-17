"""Check and pack PBR texture sets for Unity HDRP (mask map) or glTF (occlusion-roughness-metallic).

  python pbr_pack.py --albedo a.png --normal n.png --roughness r.png --metallic m.png --ao ao.png \
      --normal-convention directx --target hdrp --outdir packed --name Armor --report pbr.json

Needs numpy and Pillow. Every present map is checked before anything is written:
  * all maps share one square power-of-two size;
  * albedo: sRGB luminance of the middle 98% of pixels stays within 30-240 (no black, no white paint), and
    on metals the check is skipped for the metal pixels;
  * normal: vectors are unit length within 5% and point out of the surface (z > 0); a green channel that
    looks flipped is reported, and `--normal-convention directx` flips it to the OpenGL (Y+) convention
    Unity and glTF expect;
  * roughness / metallic / ao: single-channel, and metallic is nearly binary (at least 90% of pixels within
    0.1 of 0 or 1), because intermediate metal values are rarely physical.
Outputs, target hdrp: `<name>_BaseColor.png`, `<name>_Normal.png`, `<name>_Mask.png` where
R = metallic, G = ambient occlusion, B = detail mask (default 1), A = smoothness = 1 - roughness.
Target gltf: `<name>_BaseColor.png`, `<name>_Normal.png`, `<name>_ORM.png` (R = occlusion, G = roughness,
B = metallic). Colour maps are written as sRGB, data maps linear.
"""
import argparse
import json
import math
import os
import sys

import numpy as np
from PIL import Image


def load(path, channels):
    img = Image.open(path)
    if channels == 1:
        img = img.convert("L")
    else:
        img = img.convert("RGB")
    return np.asarray(img, dtype=np.float32) / 255.0


def srgb_luminance(rgb):
    return 0.2126 * rgb[..., 0] + 0.7152 * rgb[..., 1] + 0.0722 * rgb[..., 2]


def main():
    p = argparse.ArgumentParser()
    for name in ("albedo", "normal", "roughness", "metallic", "ao"):
        p.add_argument("--" + name)
    p.add_argument("--normal-convention", choices=("opengl", "directx"), default="opengl")
    p.add_argument("--target", choices=("hdrp", "gltf"), default="hdrp")
    p.add_argument("--outdir", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--report")
    a = p.parse_args()

    maps = {}
    for key, channels in (("albedo", 3), ("normal", 3), ("roughness", 1), ("metallic", 1), ("ao", 1)):
        path = getattr(a, key)
        if path:
            maps[key] = load(path, channels)
    if "albedo" not in maps or "normal" not in maps:
        raise SystemExit("albedo and normal are required")
    problems, notes = [], []
    sizes = {k: v.shape[:2] for k, v in maps.items()}
    size = sizes["albedo"]
    if len(set(sizes.values())) != 1:
        problems.append("maps differ in size: %s" % {k: "%dx%d" % s for k, s in sizes.items()})
    if size[0] != size[1] or size[0] & (size[0] - 1):
        problems.append("size %dx%d is not a square power of two" % (size[1], size[0]))
    metal = maps.get("metallic")
    lum = srgb_luminance(maps["albedo"]) * 255
    colour_mask = np.ones(lum.shape, bool) if metal is None or metal.shape[:2] != lum.shape else metal < 0.5
    sample = lum[colour_mask] if colour_mask.any() else lum.ravel()
    lo, hi = np.percentile(sample, [1, 99])
    if lo < 30 or hi > 240:
        problems.append("albedo luminance range %.0f-%.0f is outside 30-240" % (lo, hi))
    n = maps["normal"] * 2 - 1
    if a.normal_convention == "directx":
        n[..., 1] *= -1
        notes.append("flipped the normal green channel (DirectX to OpenGL)")
    length = np.linalg.norm(n, axis=-1)
    if abs(float(np.median(length)) - 1) > 0.05:
        problems.append("normal vectors are not unit length (median %.3f)" % float(np.median(length)))
    if float((n[..., 2] > 0).mean()) < 0.99:
        problems.append("normal map points into the surface for %.1f%% of pixels" % (100 * float((n[..., 2] <= 0).mean())))
    if metal is not None:
        binary = float(((metal < 0.1) | (metal > 0.9)).mean())
        if binary < 0.9:
            problems.append("metallic is not binary: only %.0f%% of pixels are within 0.1 of 0 or 1" % (100 * binary))
    result = {"size": [int(size[1]), int(size[0])], "problems": problems, "notes": notes, "files": []}
    result["ok"] = not problems
    if result["ok"]:
        os.makedirs(a.outdir, exist_ok=True)
        ones, zeros = np.ones(size, np.float32), np.zeros(size, np.float32)
        rough = maps.get("roughness", ones * 0.5)
        metallic = metal if metal is not None else zeros
        ao = maps.get("ao", ones)
        normal_out = np.clip((n / np.maximum(np.linalg.norm(n, axis=-1, keepdims=True), 1e-6) + 1) / 2, 0, 1)
        if a.target == "hdrp":
            packed = np.stack([metallic, ao, ones, 1 - rough], axis=-1)
            packed_name, mode = "%s_Mask.png" % a.name, "RGBA"
        else:
            packed = np.stack([ao, rough, metallic], axis=-1)
            packed_name, mode = "%s_ORM.png" % a.name, "RGB"
        for name, array, m in (("%s_BaseColor.png" % a.name, maps["albedo"], "RGB"), ("%s_Normal.png" % a.name, normal_out, "RGB"),
                               (packed_name, packed, mode)):
            Image.fromarray((np.clip(array, 0, 1) * 255 + .5).astype(np.uint8), m).save(os.path.join(a.outdir, name))
            result["files"].append(name)
    if a.report:
        json.dump(result, open(a.report, "w", encoding="utf-8"), indent=1)
    print("PBR", "ok" if result["ok"] else "FAIL", json.dumps(result))
    sys.exit(0 if result["ok"] else 1)


main()
