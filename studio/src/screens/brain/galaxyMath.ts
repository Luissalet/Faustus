/**
 * Camera maths for the galaxy view: an orbit camera, a perspective projection
 * and screen-space picking. Pure functions, no DOM, so they run in Node.
 */

export interface Camera {
  yaw: number;
  pitch: number;
  distance: number;
  target: [number, number, number];
}

export const FOV = Math.PI / 3;
const NEAR = 0.05;
const FAR = 100;

export function clampCamera(c: Camera): Camera {
  return {
    ...c,
    pitch: Math.max(-1.45, Math.min(1.45, c.pitch)),
    distance: Math.max(0.4, Math.min(12, c.distance)),
  };
}

export function eyeOf(c: Camera): [number, number, number] {
  const cp = Math.cos(c.pitch);
  return [
    c.target[0] + c.distance * cp * Math.sin(c.yaw),
    c.target[1] + c.distance * Math.sin(c.pitch),
    c.target[2] + c.distance * cp * Math.cos(c.yaw),
  ];
}

function sub(a: number[], b: number[]): [number, number, number] {
  return [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
}
function cross(a: number[], b: number[]): [number, number, number] {
  return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
}
function norm(a: number[]): [number, number, number] {
  const l = Math.hypot(a[0], a[1], a[2]) || 1;
  return [a[0] / l, a[1] / l, a[2] / l];
}
function dot(a: number[], b: number[]): number {
  return a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
}

/** Column-major view-projection matrix (what `gl.uniformMatrix4fv` takes). */
export function viewProjection(c: Camera, aspect: number): Float32Array {
  const eye = eyeOf(c);
  const f = norm(sub(c.target, eye));
  const s = norm(cross(f, [0, 1, 0]));
  const u = cross(s, f);
  const view = [
    s[0], u[0], -f[0], 0,
    s[1], u[1], -f[1], 0,
    s[2], u[2], -f[2], 0,
    -dot(s, eye), -dot(u, eye), dot(f, eye), 1,
  ];
  const k = 1 / Math.tan(FOV / 2);
  const proj = [
    k / aspect, 0, 0, 0,
    0, k, 0, 0,
    0, 0, (FAR + NEAR) / (NEAR - FAR), -1,
    0, 0, (2 * FAR * NEAR) / (NEAR - FAR), 0,
  ];
  const out = new Float32Array(16);
  for (let col = 0; col < 4; col += 1) {
    for (let row = 0; row < 4; row += 1) {
      let sum = 0;
      for (let i = 0; i < 4; i += 1) sum += proj[i * 4 + row] * view[col * 4 + i];
      out[col * 4 + row] = sum;
    }
  }
  return out;
}

export interface Projected {
  x: number;
  y: number;
  /** Clip-space w: the distance in front of the camera (<= 0 means behind). */
  w: number;
}

/** CSS-pixel position of a world point, `w <= 0` when it is behind the camera. */
export function project(vp: Float32Array, p: [number, number, number], width: number, height: number): Projected {
  const x = vp[0] * p[0] + vp[4] * p[1] + vp[8] * p[2] + vp[12];
  const y = vp[1] * p[0] + vp[5] * p[1] + vp[9] * p[2] + vp[13];
  const w = vp[3] * p[0] + vp[7] * p[1] + vp[11] * p[2] + vp[15];
  if (w <= 1e-6) return { x: NaN, y: NaN, w };
  return { x: ((x / w + 1) / 2) * width, y: ((1 - y / w) / 2) * height, w };
}

/** Point size in CSS pixels for a node of `size` at clip distance `w`. */
export function pointPixels(size: number, w: number, height: number): number {
  const px = (size * height * 0.02) / Math.max(w, 0.05);
  return Math.max(2.5, Math.min(34, px));
}

/**
 * The nearest visible node under (x, y) in CSS pixels, or -1. Ties go to the
 * node closer to the camera; a point's hit radius is its drawn radius plus a
 * few pixels of slack.
 */
export function pick(
  positions: [number, number, number][],
  sizes: number[],
  visible: boolean[],
  vp: Float32Array,
  width: number,
  height: number,
  x: number,
  y: number,
): number {
  let best = -1;
  let bestW = Infinity;
  for (let i = 0; i < positions.length; i += 1) {
    if (!visible[i]) continue;
    const p = project(vp, positions[i], width, height);
    if (!(p.w > 0)) continue;
    const reach = pointPixels(sizes[i], p.w, height) / 2 + 4;
    if (Math.hypot(p.x - x, p.y - y) <= reach && p.w < bestW) {
      best = i;
      bestW = p.w;
    }
  }
  return best;
}

export function fitCamera(radius: number): Camera {
  return { yaw: 0.6, pitch: 0.35, distance: Math.max(1.2, radius * 2.1), target: [0, 0, 0] };
}
