import { useEffect, useRef, useState } from "react";
import { BoxGeometry, CircleGeometry, Color, DirectionalLight, Group, HemisphereLight, Mesh, MeshBasicMaterial, MeshStandardMaterial, PCFSoftShadowMap, PerspectiveCamera, PolarGridHelper, Raycaster, RingGeometry, Scene, ShadowMaterial, Vector2, Vector3, WebGLRenderer } from "three";
import { ACCOUNT_COLOR, ACCOUNT_SHORT, pctOf, towerLayout } from "../../lib/overview.js";
import { inr } from "../../lib/format.js";

/** Desk hero (2026-11-18 "3D Desk"): one glowing tower per paper account, height = its value, slowly orbiting. Hover lifts a tower and
 *  highlights its label; click opens the account. Still when the viewer asks for reduced motion; renders nothing (onFail) without WebGL.
 *  Only this file imports three.js, and the Desk loads it lazily, so the other pages never download it. */
export default function AccountTowers({ accounts, onFail }) {
  const host = useRef(null);
  const towers = useRef([]);                      // [{ id, mesh, cap, target, lift }]
  const [labels, setLabels] = useState([]);       // [{ id, x, y }] screen positions of each tower top
  const [hover, setHover] = useState(null);
  const hoverRef = useRef(null);

  useEffect(() => {
    const el = host.current;
    let renderer;
    try {
      renderer = new WebGLRenderer({ antialias: true, alpha: true });
    } catch {
      onFail?.(); return undefined;
    }
    const still = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.shadowMap.enabled = true;
    renderer.shadowMap.type = PCFSoftShadowMap;
    el.appendChild(renderer.domElement);

    const scene = new Scene();
    const camera = new PerspectiveCamera(34, 1, 0.1, 100);
    scene.add(new HemisphereLight(0xffffff, 0xdfe6f0, 1.6));
    const sun = new DirectionalLight(0xffffff, 2.2);
    sun.position.set(5, 9, 6); sun.castShadow = true; sun.shadow.mapSize.set(1024, 1024);
    Object.assign(sun.shadow.camera, { left: -8, right: 8, top: 8, bottom: -8 });
    scene.add(sun);

    const floor = new Mesh(new CircleGeometry(7.5, 64), new ShadowMaterial({ opacity: 0.12 }));
    floor.rotation.x = -Math.PI / 2; floor.receiveShadow = true; scene.add(floor);
    const ring = new Mesh(new RingGeometry(6.2, 6.25, 96), new MeshBasicMaterial({ color: 0x94a3b8, transparent: true, opacity: 0.35 }));
    ring.rotation.x = -Math.PI / 2; ring.position.y = 0.002; scene.add(ring);
    const grid = new PolarGridHelper(6, 12, 5, 64, 0xcbd5e1, 0xe2e8f0);
    grid.material.transparent = true; grid.material.opacity = 0.55; scene.add(grid);

    const box = new BoxGeometry(1, 1, 1);
    const cap = new BoxGeometry(1.06, 0.08, 1.06);
    const mats = [];
    towers.current = [];
    const group = new Group(); scene.add(group);

    const raycaster = new Raycaster(), mouse = new Vector2(9, 9);
    const onMove = (e) => {
      const r = renderer.domElement.getBoundingClientRect();
      mouse.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
    };
    const onLeave = () => mouse.set(9, 9);
    const onClick = () => { if (hoverRef.current) window.location.hash = `#/paper/${hoverRef.current}`; };
    renderer.domElement.addEventListener("pointermove", onMove);
    renderer.domElement.addEventListener("pointerleave", onLeave);
    renderer.domElement.addEventListener("click", onClick);

    const size = () => {
      const w = el.clientWidth, h = el.clientHeight;
      renderer.setSize(w, h, false);
      camera.aspect = w / h; camera.updateProjectionMatrix();
    };
    const ro = new ResizeObserver(size); ro.observe(el); size();

    let raf = 0, t = 0, last = performance.now(), lastSig = "";
    const v = new Vector3();
    let visible = true, lastDraw = 0;
    const io = new IntersectionObserver(([e]) => { visible = e.isIntersecting; }); io.observe(el);
    const frame = (now) => {
      raf = requestAnimationFrame(frame);
      if (!visible || document.hidden || now - lastDraw < 33) return;   // <= 30 fps, nothing off-screen: leaves the page responsive
      lastDraw = now;
      const dt = Math.max(0, Math.min(0.05, (now - last) / 1000)); last = now;   // the first rAF time can be before `last`: a negative dt blew the towers up
      if (!still) t += dt * 0.12;
      const R = 13, a = 0.55 + t;
      camera.position.set(Math.sin(a) * R, 5.6, Math.cos(a) * R); camera.lookAt(0, 1.9, 0);

      raycaster.setFromCamera(mouse, camera);
      const hit = raycaster.intersectObjects(towers.current.map((x) => x.mesh))[0];
      const id = hit ? hit.object.userData.id : null;
      if (id !== hoverRef.current) { hoverRef.current = id; setHover(id); renderer.domElement.style.cursor = id ? "pointer" : "default"; }

      const out = [];
      for (const tw of towers.current) {
        const k = 1 - Math.pow(0.001, dt);                        // smooth approach, frame-rate independent
        tw.h += (tw.target - tw.h) * k;
        tw.lift += ((tw.id === hoverRef.current ? 0.35 : 0) - tw.lift) * k;
        tw.mesh.scale.y = tw.h; tw.mesh.position.y = tw.h / 2 + tw.lift;
        tw.cap.position.y = tw.h + tw.lift + 0.04;
        tw.mesh.material.emissiveIntensity = tw.id === hoverRef.current ? 0.55 : 0.22;
        v.set(tw.mesh.position.x, tw.h + tw.lift + 0.35, tw.mesh.position.z).project(camera);
        out.push({ id: tw.id, x: (v.x * 0.5 + 0.5) * el.clientWidth, y: Math.max(64, (-v.y * 0.5 + 0.5) * el.clientHeight) });   // label stays inside the card
      }
      const sig = out.map((o) => `${o.id}${Math.round(o.x)},${Math.round(o.y)}`).join();
      if (sig !== lastSig) { lastSig = sig; setLabels(out); }        // re-render React only when a label really moves
      renderer.render(scene, camera);
    };
    raf = requestAnimationFrame(frame);

    el.__build = (list) => {                                       // (re)build towers when the accounts change
      for (const tw of towers.current) { group.remove(tw.mesh); group.remove(tw.cap); }
      mats.splice(0).forEach((m) => m.dispose());
      towers.current = towerLayout(list).map(({ id, x, height }) => {
        const color = new Color(ACCOUNT_COLOR[id] ?? "#64748b");
        const m = new MeshStandardMaterial({ color, roughness: 0.32, metalness: 0.15, emissive: color, emissiveIntensity: 0.22 });
        const c = new MeshStandardMaterial({ color: color.clone().lerp(new Color(0xffffff), 0.55), roughness: 0.2, emissive: color, emissiveIntensity: 0.4 });
        mats.push(m, c);
        const mesh = new Mesh(box, m); mesh.position.set(x, 0, 0); mesh.castShadow = true; mesh.userData.id = id;
        const top = new Mesh(cap, c); top.position.set(x, 0, 0); top.castShadow = true;
        group.add(mesh, top);
        return { id, mesh, cap: top, target: height, h: still ? height : 0.05, lift: 0 };
      });
    };
    el.__build(accounts);

    return () => {
      cancelAnimationFrame(raf); ro.disconnect(); io.disconnect();
      renderer.domElement.removeEventListener("pointermove", onMove);
      renderer.domElement.removeEventListener("pointerleave", onLeave);
      renderer.domElement.removeEventListener("click", onClick);
      [box, cap, floor.geometry, ring.geometry, grid.geometry].forEach((g) => g.dispose());
      [floor.material, ring.material, grid.material, ...mats].forEach((m) => m.dispose());
      renderer.dispose(); renderer.domElement.remove();
    };
  }, []);                                                          // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => { host.current?.__build?.(accounts); }, [accounts.map((a) => `${a.id}:${Math.round(a.value)}`).join()]);   // eslint-disable-line

  return (
    <div className="towers" ref={host} role="img" aria-label={`3D view: ${accounts.map((a) => `${a.label} ${inr(a.value)}`).join(", ")}`}>
      {labels.map((l) => {
        const a = accounts.find((x) => x.id === l.id);
        if (!a) return null;
        const pc = pctOf(a.value, a.start);
        return (
          <a key={l.id} href={`#/paper/${l.id}`} className={`tower-tag${hover === l.id ? " on" : ""}`} style={{ left: l.x, top: l.y, "--c": ACCOUNT_COLOR[l.id] }}>
            <span>{ACCOUNT_SHORT[l.id]}</span><b>{inr(a.value)}</b><small className={pc >= 0 ? "up" : "down"}>{pc >= 0 ? "+" : ""}{pc.toFixed(1)}%</small>
          </a>);
      })}
    </div>
  );
}
