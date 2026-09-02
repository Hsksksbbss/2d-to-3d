import { useMemo, useRef, useState, useEffect } from "react";
import * as THREE from "three";
import { OrbitControls as OrbitControlsImpl } from "three/examples/jsm/controls/OrbitControls";
import { Canvas, useThree, useFrame } from "@react-three/fiber";
import { Grid, Html } from "@react-three/drei";
import { Button } from "@/components/ui/button";
import { VIEWER } from "@/constants/testIds";

const DS = THREE.DoubleSide;

/**
 * Build 3D scene data from the analysis result.
 *
 * All coords come from the analyzer already in METERS but with the y-axis
 * pointing "down" (image space). We flip y and center everything at the
 * origin before rendering.
 */
function useSceneData(plan, materials) {
  return useMemo(() => {
    if (!plan) return null;
    const a = plan.analysis;
    // compute footprint bounds from walls (or rooms if walls empty)
    const pts = [];
    a.walls.forEach((w) => {
      pts.push([w.x1 / a.px_per_m, w.y1 / a.px_per_m]);
      pts.push([w.x2 / a.px_per_m, w.y2 / a.px_per_m]);
    });
    a.rooms.forEach((r) => r.polygon.forEach((p) => pts.push(p)));
    if (pts.length === 0) return null;
    const xs = pts.map((p) => p[0]);
    const ys = pts.map((p) => p[1]);
    const minX = Math.min(...xs), maxX = Math.max(...xs);
    const minY = Math.min(...ys), maxY = Math.max(...ys);
    const cx = (minX + maxX) / 2;
    const cy = (minY + maxY) / 2;

    // helper: image space (x, y_down) in meters -> XZ world (x-cx, z=y-cy).
    // We use a room-shape mesh rotated by +π/2 around X, so shape point (px, py)
    // maps to world (px, 0, py) — no sign flip. All primitives (walls, rooms,
    // doors, labels) share this frame.
    const T = ([x, y]) => [x - cx, y - cy];

    // Group openings by wall_id and compute their parametric span along the
    // wall so we can render each wall as several sub-segments with gaps
    // (real openings) plus lintels/sills over the openings.
    const openingsByWall = new Map();
    const pushOpening = (op, kind) => {
      if (!op.wall_id) return;
      const arr = openingsByWall.get(op.wall_id) || [];
      arr.push({ ...op, kind });
      openingsByWall.set(op.wall_id, arr);
    };
    a.doors.forEach((d) => pushOpening(d, "door"));
    a.windows.forEach((w) => pushOpening(w, "window"));

    const wallH = materials.wallHeight;
    const doorH = 2.1;
    const winH = 1.2;
    const sillH = 0.9;

    const walls = [];
    a.walls.forEach((w) => {
      const [ax, az] = T([w.x1 / a.px_per_m, w.y1 / a.px_per_m]);
      const [bx, bz] = T([w.x2 / a.px_per_m, w.y2 / a.px_per_m]);
      const dx = bx - ax, dz = bz - az;
      const totalLen = Math.hypot(dx, dz);
      const angle = Math.atan2(dz, dx);
      if (totalLen < 0.05) return;

      // build gaps list (each with start & end t in [0..1] on this wall)
      const gaps = (openingsByWall.get(w.id) || [])
        .map((op) => {
          const wPart = Math.min(op.width_m || 0.9, totalLen * 0.9);
          const t = typeof op.wall_t === "number"
            ? op.wall_t
            : 0.5;
          const halfT = wPart / (2 * totalLen);
          return {
            t0: Math.max(0.02, t - halfT),
            t1: Math.min(0.98, t + halfT),
            kind: op.kind,
            width: wPart,
          };
        })
        .filter((g) => g.t1 > g.t0)
        .sort((a, b) => a.t0 - b.t0);

      // split into solid sub-segments in world space
      const points = [{ t: 0 }];
      gaps.forEach((g) => {
        points.push({ t: g.t0, gap: "start", g });
        points.push({ t: g.t1, gap: "end", g });
      });
      points.push({ t: 1 });

      let cursor = 0;
      for (let i = 0; i < points.length; i += 1) {
        if (points[i].gap === "start" || points[i].t === 1) {
          const t0 = cursor;
          const t1 = points[i].t;
          if (t1 - t0 > 0.005) {
            const segLen = (t1 - t0) * totalLen;
            const midT = (t0 + t1) / 2;
            walls.push({
              id: `${w.id}_${i}`,
              cx: ax + midT * dx,
              cz: az + midT * dz,
              length: segLen,
              angle,
              thickness: materials.wallThickness,
              height: wallH,
              y: wallH / 2,
              kind: "full",
            });
          }
        }
        if (points[i].gap === "end") {
          cursor = points[i].t;
        }
      }
      // headers / lintels above openings + sill below windows
      gaps.forEach((g) => {
        const midT = (g.t0 + g.t1) / 2;
        const gapLen = (g.t1 - g.t0) * totalLen;
        if (gapLen < 0.05) return;
        const cxg = ax + midT * dx;
        const czg = az + midT * dz;
        if (g.kind === "door") {
          const headerH = wallH - doorH;
          if (headerH > 0.05) {
            walls.push({
              id: `${w.id}_hdr_${midT.toFixed(3)}`,
              cx: cxg, cz: czg,
              length: gapLen,
              angle,
              thickness: materials.wallThickness,
              height: headerH,
              y: doorH + headerH / 2,
              kind: "lintel",
            });
          }
        } else {
          // window: sill + top lintel
          const topH = wallH - (sillH + winH);
          if (sillH > 0.05) {
            walls.push({
              id: `${w.id}_sill_${midT.toFixed(3)}`,
              cx: cxg, cz: czg,
              length: gapLen,
              angle,
              thickness: materials.wallThickness,
              height: sillH,
              y: sillH / 2,
              kind: "sill",
            });
          }
          if (topH > 0.05) {
            walls.push({
              id: `${w.id}_top_${midT.toFixed(3)}`,
              cx: cxg, cz: czg,
              length: gapLen,
              angle,
              thickness: materials.wallThickness,
              height: topH,
              y: sillH + winH + topH / 2,
              kind: "top",
            });
          }
        }
      });
    });

    const rooms = a.rooms.map((r) => {
      const poly2d = r.polygon.map(T);   // [x, z]
      const shape = new THREE.Shape();
      poly2d.forEach((p, i) => {
        if (i === 0) shape.moveTo(p[0], p[1]);
        else shape.lineTo(p[0], p[1]);
      });
      shape.closePath();
      // label position: always use polygon centroid for reliable placement
      const cx2 = poly2d.reduce((s, p) => s + p[0], 0) / poly2d.length;
      const cz2 = poly2d.reduce((s, p) => s + p[1], 0) / poly2d.length;
      const label = [cx2, cz2];
      return { id: r.id, name: r.name, shape, label,
               length_m: r.length_m, width_m: r.width_m, area_m2: r.area_m2 };
    });

    const doors = a.doors.map((d) => {
      const [x, z] = T([d.x, d.y]);
      return { ...d, x, z };
    });
    const windows = a.windows.map((w) => {
      const [x, z] = T([w.x, w.y]);
      return { ...w, x, z };
    });
    const stairs = (a.stairs || []).map((st) => {
      const pts = (st.polygon_m || []).map(T);
      if (pts.length < 3) return null;
      const xs = pts.map((p) => p[0]);
      const zs = pts.map((p) => p[1]);
      return {
        id: st.id,
        cx: (Math.min(...xs) + Math.max(...xs)) / 2,
        cz: (Math.min(...zs) + Math.max(...zs)) / 2,
        w: Math.max(...xs) - Math.min(...xs),
        d: Math.max(...zs) - Math.min(...zs),
        treads: st.treads,
      };
    }).filter(Boolean);

    // footprint
    const fpShape = new THREE.Shape();
    fpShape.moveTo(minX - cx, minY - cy);
    fpShape.lineTo(maxX - cx, minY - cy);
    fpShape.lineTo(maxX - cx, maxY - cy);
    fpShape.lineTo(minX - cx, maxY - cy);
    fpShape.closePath();

    return {
      walls, rooms, doors, windows, stairs,
      footprint: fpShape,
      extentX: maxX - minX,
      extentZ: maxY - minY,
    };
  }, [plan, materials.wallHeight, materials.wallThickness]);
}

function CameraRig({ preset, extent, controlsRef }) {
  const { camera, gl } = useThree();
  // create controls once
  useEffect(() => {
    const c = new OrbitControlsImpl(camera, gl.domElement);
    c.enableDamping = true;
    c.dampingFactor = 0.08;
    controlsRef.current = c;
    return () => c.dispose();
  }, [camera, gl, controlsRef]);
  useFrame(() => controlsRef.current?.update());
  useEffect(() => {
    if (!extent) return;
    const d = Math.max(extent.x, extent.z) * 1.6;
    let pos = [d, d * 0.9, d];
    if (preset === "top") pos = [0, d * 1.5, 0.001];
    else if (preset === "front") pos = [0, extent.y * 0.8, d];
    else if (preset === "back") pos = [0, extent.y * 0.8, -d];
    else if (preset === "left") pos = [-d, extent.y * 0.8, 0];
    else if (preset === "right") pos = [d, extent.y * 0.8, 0];
    camera.position.set(...pos);
    camera.lookAt(0, extent.y / 2, 0);
    const c = controlsRef.current;
    if (c?.target) { c.target.set(0, extent.y / 2, 0); c.update?.(); }
  }, [preset, extent, camera, controlsRef]);
  return null;
}

export default function ThreeViewer({
  plan, materials, selectedRoomId, onSelectRoom, isolated,
  cameraPreset, setCameraPreset,
}) {
  const scene = useSceneData(plan, materials);
  const [hoverRoom, setHoverRoom] = useState(null);
  const controlsRef = useRef(null);
  const wrapRef = useRef(null);
  const [ready, setReady] = useState(false);
  useEffect(() => {
    const id = requestAnimationFrame(() => setReady(true));
    return () => cancelAnimationFrame(id);
  }, []);

  if (!scene) {
    return (
      <div className="card p-8 text-center text-[color:var(--muted)]">
        Not enough geometry to build a 3D model.
      </div>
    );
  }

  const extent = { x: scene.extentX, y: materials.wallHeight, z: scene.extentZ };

  return (
    <div>
      <div className="flex flex-wrap items-center gap-2 mb-3">
        <PresetBtn preset={cameraPreset} me="iso" onClick={setCameraPreset} tid={VIEWER.resetCam}>
          Reset
        </PresetBtn>
        <PresetBtn preset={cameraPreset} me="top" onClick={setCameraPreset} tid={VIEWER.topView}>Top</PresetBtn>
        <PresetBtn preset={cameraPreset} me="front" onClick={setCameraPreset} tid={VIEWER.frontView}>Front</PresetBtn>
        <PresetBtn preset={cameraPreset} me="back" onClick={setCameraPreset} tid={VIEWER.backView}>Back</PresetBtn>
        <PresetBtn preset={cameraPreset} me="left" onClick={setCameraPreset} tid={VIEWER.leftView}>Left</PresetBtn>
        <PresetBtn preset={cameraPreset} me="right" onClick={setCameraPreset} tid={VIEWER.rightView}>Right</PresetBtn>
        <span className="ml-auto text-xs text-[color:var(--muted)] mono">
          drag = rotate · shift-drag = pan · wheel = zoom
        </span>
      </div>

      <div className="canvas-wrap" data-testid={VIEWER.root} ref={wrapRef}>
        {ready && (
        <Canvas
          shadows
          camera={{ position: [10, 10, 10], fov: 45, near: 0.1, far: 500 }}
        >
          <color attach="background" args={["#efe8dc"]} />
          <ambientLight intensity={0.55} />
          <directionalLight
            position={[15, 25, 10]}
            intensity={1.1}
            castShadow
            shadow-mapSize-width={2048}
            shadow-mapSize-height={2048}
            shadow-camera-left={-30}
            shadow-camera-right={30}
            shadow-camera-top={30}
            shadow-camera-bottom={-30}
          />
          <hemisphereLight args={["#fff5e1", "#8a7a5c", 0.35]} />

          <Grid
            args={[80, 80]}
            cellSize={0.5}
            cellThickness={0.5}
            cellColor="#c9bda3"
            sectionSize={2}
            sectionThickness={1}
            sectionColor="#8a7a5c"
            fadeDistance={40}
            infiniteGrid
            position={[0, -0.001, 0]}
          />

          {/* floor slab (from footprint) */}
          <mesh receiveShadow rotation={[Math.PI / 2, 0, 0]} position={[0, 0, 0]}>
            <shapeGeometry args={[scene.footprint]} />
            <meshStandardMaterial color={materials.floorColor} roughness={0.85} side={DS} />
          </mesh>

          {/* per-room colored floor tint */}
          {scene.rooms.map((r, i) => {
            if (isolated && selectedRoomId && r.id !== selectedRoomId) return null;
            const selected = r.id === selectedRoomId;
            const hue = (i * 47) % 360;
            const tint = selected
              ? "#f6c26b"
              : `hsl(${hue}, 25%, ${72 + (i % 3) * 3}%)`;
            return (
              <group key={r.id}>
                <mesh
                  rotation={[Math.PI / 2, 0, 0]}
                  position={[0, 0.005 + (selected ? 0.002 : 0), 0]}
                  onClick={(e) => { e.stopPropagation(); onSelectRoom(r.id); }}
                  onPointerOver={(e) => { e.stopPropagation(); setHoverRoom(r.id); document.body.style.cursor = "pointer"; }}
                  onPointerOut={() => { setHoverRoom((h) => (h === r.id ? null : h)); document.body.style.cursor = "default"; }}
                >
                  <shapeGeometry args={[r.shape]} />
                  <meshStandardMaterial
                    color={tint}
                    transparent
                    opacity={selected ? 0.9 : 0.75}
                    roughness={0.9}
                    side={DS}
                  />
                </mesh>
                <Html position={[r.label[0], 0.01, r.label[1]]} center distanceFactor={12} zIndexRange={[1, 0]} occlude={false} portal={{ current: null }}>
                  <div
                    onClick={() => onSelectRoom(r.id)}
                    className="pointer-events-auto select-none px-2 py-0.5 rounded-md bg-white/90 text-[10px] mono whitespace-nowrap shadow-sm border border-[color:var(--line)]"
                    style={{ transform: "translate(-50%, -50%)" }}
                  >
                    <div className="font-semibold text-[11px]">{r.name}</div>
                    <div className="text-[color:var(--muted)]">
                      {r.length_m.toFixed(1)} × {r.width_m.toFixed(1)} m
                    </div>
                  </div>
                </Html>
              </group>
            );
          })}

          {/* walls (split at door/window openings; lintels/sills added) */}
          {scene.walls.map((w) => (
            <mesh
              key={w.id}
              castShadow
              receiveShadow
              position={[w.cx, w.y, w.cz]}
              rotation={[0, -w.angle, 0]}
            >
              <boxGeometry args={[w.length, w.height, w.thickness]} />
              <meshStandardMaterial
                color={materials.wallColor}
                roughness={0.9}
              />
            </mesh>
          ))}

          {/* door leaves — thin plate positioned inside the opening, rotated
              90° so it looks like an open door. Height matches door height. */}
          {scene.doors.map((d) => (
            <group
              key={d.id}
              position={[d.x, 0, d.z]}
              rotation={[0, -THREE.MathUtils.degToRad(d.orientation_deg), 0]}
            >
              <mesh position={[0, 1.05, (d.width_m || 0.9) / 2 - 0.04]}
                    castShadow rotation={[0, Math.PI / 2, 0]}>
                <boxGeometry args={[(d.width_m || 0.9) * 0.95, 2.05, 0.04]} />
                <meshStandardMaterial color={materials.doorColor} roughness={0.55} />
              </mesh>
              {/* door frame */}
              <mesh position={[0, 1.05, 0]}>
                <boxGeometry args={[(d.width_m || 0.9) + 0.06, 2.1, materials.wallThickness + 0.02]} />
                <meshStandardMaterial color="#4a3324" roughness={0.7} wireframe />
              </mesh>
            </group>
          ))}

          {/* window glass panes inside their openings */}
          {scene.windows.map((w) => (
            <mesh
              key={w.id}
              position={[w.x, 0.9 + 1.2 / 2, w.z]}
              rotation={[0, -THREE.MathUtils.degToRad(w.orientation_deg), 0]}
              castShadow
            >
              <boxGeometry args={[Math.max(0.6, w.width_m || 0.9), 1.15, 0.04]} />
              <meshStandardMaterial
                color={materials.windowColor}
                transparent
                opacity={0.35}
                roughness={0.05}
                metalness={0.2}
              />
            </mesh>
          ))}

          {/* stairs: stack of thin plates */}
          {scene.stairs.map((s) => {
            const nTreads = Math.max(4, Math.min(14, s.treads || 8));
            const treadDepth = Math.max(s.w, s.d) / nTreads;
            const treadWidth = Math.min(s.w, s.d);
            const horiz = s.w >= s.d;
            return (
              <group key={s.id} position={[s.cx, 0, s.cz]}>
                {Array.from({ length: nTreads }).map((_, i) => {
                  const h = 0.15 * (i + 1);
                  const off = -Math.max(s.w, s.d) / 2 + treadDepth * (i + 0.5);
                  return (
                    <mesh
                      key={i}
                      castShadow receiveShadow
                      position={[horiz ? off : 0, h / 2, horiz ? 0 : off]}
                    >
                      <boxGeometry
                        args={
                          horiz
                            ? [treadDepth * 0.95, h, treadWidth]
                            : [treadWidth, h, treadDepth * 0.95]
                        }
                      />
                      <meshStandardMaterial color="#b98a5b" roughness={0.7} />
                    </mesh>
                  );
                })}
              </group>
            );
          })}

          <CameraRig preset={cameraPreset} extent={extent} controlsRef={controlsRef} />
        </Canvas>
        )}
      </div>
    </div>
  );
}

function PresetBtn({ preset, me, onClick, children, tid }) {
  const active = preset === me;
  return (
    <Button
      variant={active ? "default" : "outline"}
      size="sm"
      data-testid={tid}
      onClick={() => onClick(me)}
      className={active ? "bg-[color:var(--ink)] text-white" : ""}
    >
      {children}
    </Button>
  );
}
