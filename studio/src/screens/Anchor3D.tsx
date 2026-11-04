import { useEffect, useRef, useState } from 'react';
import { t } from '../i18n';

/**
 * A 3D deliverable opened at an object anchor (Hangar E22): the object is outlined and lit, the rest is dimmed and the camera
 * is framed on it. Loaded on demand (three.js, MIT) so the rest of the screen does not pay for it.
 */
export interface Anchor3DProps { url: string; ext: string; target: string; onState?: (s: { highlighted: number; total: number }) => void }

const norm = (s: string) => s.toLowerCase().replace(/[\s.:\[\]\\/]+/g, '_');

export default function Anchor3D({ url, ext, target }: Anchor3DProps) {
  const host = useRef<HTMLDivElement>(null);
  const [state, setState] = useState<{ highlighted: number; total: number } | null>(null);
  const [error, setError] = useState('');

  useEffect(() => {
    let disposed = false;
    let cleanup = () => {};
    (async () => {
      try {
        const THREE = await import('three');
        const { OrbitControls } = await import('three/examples/jsm/controls/OrbitControls.js');
        const buf = await (await fetch(url, { credentials: 'include' })).arrayBuffer();
        if (disposed || !host.current) return;
        let root: import('three').Object3D;
        const e = ext.toLowerCase();
        if (e === '.obj') {
          const { OBJLoader } = await import('three/examples/jsm/loaders/OBJLoader.js');
          root = new OBJLoader().parse(new TextDecoder().decode(buf));
        } else if (e === '.glb' || e === '.gltf') {
          const { GLTFLoader } = await import('three/examples/jsm/loaders/GLTFLoader.js');
          root = (await new Promise<{ scene: import('three').Object3D }>((ok, bad) => new GLTFLoader().parse(e === '.gltf' ? new TextDecoder().decode(buf) : buf, '', ok, bad))).scene;
        } else if (e === '.stl') {
          const { STLLoader } = await import('three/examples/jsm/loaders/STLLoader.js');
          const mesh = new THREE.Mesh(new STLLoader().parse(buf), new THREE.MeshStandardMaterial({ color: 0x9aa4b2 }));
          mesh.name = target;                                                                 // an STL holds one solid: that is the object
          root = mesh;
        } else if (e === '.3mf') {
          const { ThreeMFLoader } = await import('three/examples/jsm/loaders/3MFLoader.js');
          root = new ThreeMFLoader().parse(buf);
        } else {
          throw new Error(`No 3D viewer for ${e}`);
        }
        const wanted = norm(target);
        const meshes: import('three').Mesh[] = [];
        root.traverse((o) => { if ((o as import('three').Mesh).isMesh) meshes.push(o as import('three').Mesh); });
        const hasName = (o: import('three').Object3D | null): boolean => { for (let p = o; p; p = p.parent) if (p.name && norm(p.name) === wanted) return true; return false; };
        const hit = meshes.filter((m) => hasName(m));
        const box = new THREE.Box3();
        meshes.forEach((m) => {
          const mats = (Array.isArray(m.material) ? m.material : [m.material]).map((mat) => (mat as import('three').Material).clone());
          mats.forEach((mat) => {
            const std = mat as import('three').MeshStandardMaterial;
            if (hit.includes(m)) { if (std.emissive) { std.emissive = new THREE.Color(0xff5a36); std.emissiveIntensity = 0.55; } }
            else { mat.transparent = true; mat.opacity = hit.length ? 0.22 : 1; mat.depthWrite = !hit.length; }
          });
          m.material = Array.isArray(m.material) ? mats : mats[0];
        });
        const scene = new THREE.Scene();
        scene.background = new THREE.Color(0x14181f);
        scene.add(root, new THREE.HemisphereLight(0xffffff, 0x334155, 1.4));
        const sun = new THREE.DirectionalLight(0xffffff, 1.6); sun.position.set(3, 5, 4); scene.add(sun);
        hit.forEach((m) => {
          const helper = new THREE.BoxHelper(m, 0xff5a36); scene.add(helper);
          box.expandByObject(m);
        });
        if (!hit.length) box.setFromObject(root);
        const size = box.getSize(new THREE.Vector3()); const center = box.getCenter(new THREE.Vector3());
        const radius = Math.max(size.x, size.y, size.z, 1e-3);
        const w = host.current.clientWidth || 640, h = 360;
        const camera = new THREE.PerspectiveCamera(40, w / h, radius / 100, radius * 100);
        const dist = (radius / 2) / Math.tan((camera.fov * Math.PI) / 360) * 1.8;           // frame the highlighted object
        camera.position.copy(center).add(new THREE.Vector3(1, 0.8, 1).normalize().multiplyScalar(dist));
        camera.lookAt(center);
        const renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true });
        renderer.setSize(w, h); renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
        host.current.appendChild(renderer.domElement);
        const controls = new OrbitControls(camera, renderer.domElement);
        controls.target.copy(center); controls.update();
        let raf = 0;
        const draw = () => { renderer.render(scene, camera); };
        controls.addEventListener('change', () => { cancelAnimationFrame(raf); raf = requestAnimationFrame(draw); });
        draw();
        const s = { highlighted: hit.length, total: meshes.length };
        setState(s);
        cleanup = () => { cancelAnimationFrame(raf); controls.dispose(); renderer.dispose(); renderer.domElement.remove(); };
        if (disposed) cleanup();
      } catch (err) { if (!disposed) setError((err as Error).message); }
    })();
    return () => { disposed = true; cleanup(); };
  }, [url, ext, target]);

  return (
    <div data-testid="review-anchor-3d" data-highlighted={state?.highlighted ?? ''} data-total={state?.total ?? ''}>
      <div ref={host} className="fs-dlv__anchor-3d" />
      {error && <p className="fs-dlv__why" data-tone="warn" role="alert">{error}</p>}
      {state && state.highlighted === 0 && <p className="fs-dlv__muted">{t('That place is not in this version')}: {target}</p>}
    </div>
  );
}
