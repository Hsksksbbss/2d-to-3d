import { useCallback, useMemo, useState } from "react";
import axios from "axios";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import UploadPanel from "@/components/floorplan/UploadPanel";
import DetectionStats from "@/components/floorplan/DetectionStats";
import ThreeViewer from "@/components/floorplan/ThreeViewer";
import DebugView from "@/components/floorplan/DebugView";
import RoomInfoPanel from "@/components/floorplan/RoomInfoPanel";
import MaterialControls from "@/components/floorplan/MaterialControls";
import CalibrationPanel from "@/components/floorplan/CalibrationPanel";
import { TABS } from "@/constants/testIds";

const API = `${process.env.REACT_APP_BACKEND_URL}/api`;

const DEFAULT_MATERIALS = {
  wallColor: "#f0e6d2",
  floorColor: "#d9c39a",
  doorColor: "#a97142",
  windowColor: "#7fb1c9",
  wallHeight: 2.7,
  wallThickness: 0.1524,
  showFurniture: true,
};

export default function FloorPlanApp() {
  const [loading, setLoading] = useState(false);
  const [plan, setPlan] = useState(null);            // { id, image, analysis }
  const [materials, setMaterials] = useState(DEFAULT_MATERIALS);
  const [selectedRoomId, setSelectedRoomId] = useState(null);
  const [isolated, setIsolated] = useState(false);
  const [cameraPreset, setCameraPreset] = useState("iso");

  const applyManualScale = useCallback(({ pxPerM }) => {
    setPlan((cur) => {
      if (!cur) return cur;
      const oldPxPerM = cur.analysis.px_per_m;
      const ratio = oldPxPerM / pxPerM;
      // rescale distance-derived quantities
      const rescaledRooms = cur.analysis.rooms.map((r) => ({
        ...r,
        length_m: r.length_m * ratio,
        width_m: r.width_m * ratio,
        area_m2: r.area_m2 * ratio * ratio,
        polygon: r.polygon.map((p) => [p[0] * ratio, p[1] * ratio]),
      }));
      const rescaledDoors = cur.analysis.doors.map((d) => ({
        ...d, x: d.x * ratio, y: d.y * ratio, width_m: d.width_m * ratio,
      }));
      const rescaledWindows = cur.analysis.windows.map((w) => ({
        ...w, x: w.x * ratio, y: w.y * ratio, width_m: w.width_m * ratio,
      }));
      return {
        ...cur,
        analysis: {
          ...cur.analysis,
          px_per_m: pxPerM,
          px_per_m_confidence: 1.0,
          rooms: rescaledRooms,
          doors: rescaledDoors,
          windows: rescaledWindows,
        },
      };
    });
  }, []);

  const onUpload = useCallback(async (file) => {
    if (!file) return;
    setLoading(true);
    const form = new FormData();
    form.append("file", file);
    try {
      const { data } = await axios.post(`${API}/floorplan/analyze`, form, {
        headers: { "Content-Type": "multipart/form-data" },
        timeout: 120000,
      });
      setPlan(data);
      setSelectedRoomId(null);
      setIsolated(false);
      toast.success("Analysis complete", {
        description: `${data.analysis.rooms.length} rooms · ${data.analysis.walls.length} walls`,
      });
    } catch (e) {
      const msg = e?.response?.data?.detail || e.message;
      toast.error("Analysis failed", { description: String(msg) });
    } finally {
      setLoading(false);
    }
  }, []);

  const stats = useMemo(() => {
    if (!plan) return null;
    const a = plan.analysis;
    return {
      walls: a.walls.length,
      rooms: a.rooms.length,
      doors: a.doors.length,
      windows: a.windows.length,
      stairs: (a.stairs || []).length,
      pxPerM: a.px_per_m,
      pxPerMConf: a.px_per_m_confidence,
    };
  }, [plan]);

  const selectedRoom = useMemo(() => {
    if (!plan || !selectedRoomId) return null;
    return plan.analysis.rooms.find((r) => r.id === selectedRoomId) || null;
  }, [plan, selectedRoomId]);

  return (
    <div className="grain min-h-screen">
      <header className="border-b border-[color:var(--line)] bg-[color:var(--panel)]/60 backdrop-blur relative z-10">
        <div className="max-w-[1400px] mx-auto px-6 py-5 flex items-center justify-between">
          <div>
            <div className="mono text-xs uppercase tracking-[0.25em] text-[color:var(--muted)]">
              floorplan / three.js
            </div>
            <h1 className="text-2xl sm:text-3xl font-semibold tracking-tight">
              Plan → Space
            </h1>
          </div>
          <div className="mono text-xs text-[color:var(--muted)] hidden md:block">
            v0.1 · CV pipeline + OCR + 3D
          </div>
        </div>
      </header>

      <main className="max-w-[1400px] mx-auto px-6 py-6 relative z-10 grid grid-cols-1 lg:grid-cols-[340px_1fr] gap-6">
        <aside className="space-y-5">
          <UploadPanel loading={loading} onUpload={onUpload} />
          <DetectionStats stats={stats} />
          {plan && (
            <CalibrationPanel plan={plan} onCalibrate={applyManualScale} />
          )}
          <RoomInfoPanel
            room={selectedRoom}
            plan={plan}
            isolated={isolated}
            onIsolate={() => setIsolated((v) => !v)}
            onShowAll={() => { setIsolated(false); setSelectedRoomId(null); }}
          />
        </aside>

        <section>
          {!plan ? (
            <EmptyState />
          ) : (
            <TabsView
              plan={plan}
              materials={materials}
              setMaterials={setMaterials}
              selectedRoomId={selectedRoomId}
              setSelectedRoomId={setSelectedRoomId}
              isolated={isolated}
              cameraPreset={cameraPreset}
              setCameraPreset={setCameraPreset}
            />
          )}
        </section>
      </main>
    </div>
  );
}

/**
 * Custom "tabs" that keep the 3D Canvas persistently mounted so drei/R3F
 * doesn't crash on unmount (events.connect(null) -> addEventListener null).
 * Inactive tab content is hidden with CSS, not unmounted.
 */
function TabsView({
  plan, materials, setMaterials, selectedRoomId, setSelectedRoomId,
  isolated, cameraPreset, setCameraPreset,
}) {
  const [tab, setTab] = useState("three");
  const items = [
    { v: "three", label: "3D View", tid: TABS.three },
    { v: "rooms", label: "Rooms", tid: TABS.rooms },
    { v: "materials", label: "Materials", tid: TABS.materials },
    { v: "debug", label: "Debug pipeline", tid: TABS.debug },
  ];
  return (
    <div>
      <div className="inline-flex items-center gap-1 rounded-lg border border-[color:var(--line)] bg-[color:var(--panel)] p-1">
        {items.map((it) => (
          <button
            key={it.v}
            data-testid={it.tid}
            role="tab"
            aria-selected={tab === it.v}
            onClick={() => setTab(it.v)}
            className={
              "px-3 py-1.5 text-sm rounded-md transition-colors " +
              (tab === it.v
                ? "bg-[color:var(--ink)] text-white"
                : "text-[color:var(--muted)] hover:bg-black/5")
            }
          >
            {it.label}
          </button>
        ))}
      </div>
      <div className="mt-4" style={{ display: tab === "three" ? "block" : "none" }}>
        <ThreeViewer
          plan={plan}
          materials={materials}
          selectedRoomId={selectedRoomId}
          onSelectRoom={setSelectedRoomId}
          isolated={isolated}
          cameraPreset={cameraPreset}
          setCameraPreset={setCameraPreset}
        />
      </div>
      <div className="mt-4" style={{ display: tab === "rooms" ? "block" : "none" }}>
        <RoomsTable
          plan={plan}
          onSelect={(id) => { setSelectedRoomId(id); setCameraPreset("top"); setTab("three"); }}
        />
      </div>
      <div className="mt-4" style={{ display: tab === "materials" ? "block" : "none" }}>
        <MaterialControls materials={materials} setMaterials={setMaterials} />
      </div>
      <div className="mt-4" style={{ display: tab === "debug" ? "block" : "none" }}>
        <DebugView plan={plan} />
      </div>
    </div>
  );
}

function EmptyState() {
  return (
    <div className="card p-10 text-center">
      <div className="mono text-xs uppercase tracking-[0.25em] text-[color:var(--muted)]">
        step 1
      </div>
      <h2 className="text-3xl sm:text-4xl font-semibold mt-2 mb-2">
        Upload a floor plan
      </h2>
      <p className="text-[color:var(--muted)] max-w-md mx-auto">
        PNG or JPG. Text labels like <span className="mono">KITCHEN</span> and
        dimensions like <span className="mono">14&apos;-0&quot; X 12&apos;-0&quot;</span> are
        detected and used to calibrate scale.
      </p>
    </div>
  );
}

function RoomsTable({ plan, onSelect }) {
  const rooms = plan.analysis.rooms;
  if (!rooms.length) {
    return <div className="card p-6 text-[color:var(--muted)]">No rooms detected.</div>;
  }
  return (
    <div className="card overflow-hidden">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-[color:var(--muted)] mono text-xs uppercase">
            <th className="p-3">Room</th>
            <th className="p-3">Dim (m)</th>
            <th className="p-3">Area (m²)</th>
            <th className="p-3">Source</th>
            <th className="p-3">Conf</th>
            <th className="p-3"></th>
          </tr>
        </thead>
        <tbody>
          {rooms.map((r) => {
            const scaleCertain = plan.analysis?.px_per_m_confidence >= 0.7;
            const showDims = r.dim_certain || scaleCertain;
            return (
              <tr key={r.id} className="border-t border-[color:var(--line)]">
                <td className="p-3 font-medium">{r.name}</td>
                <td className="p-3 stat mono">
                  {showDims
                    ? `${r.length_m.toFixed(2)} × ${r.width_m.toFixed(2)}`
                    : <span className="text-[color:var(--muted)]">unavailable</span>}
                </td>
                <td className="p-3 stat mono">
                  {showDims
                    ? r.area_m2.toFixed(2)
                    : <span className="text-[color:var(--muted)]">unavailable</span>}
                </td>
                <td className="p-3">
                  {r.dim_certain ? (
                    <span className="text-[color:var(--accent-2)]">from label</span>
                  ) : scaleCertain ? (
                    <span className="text-[color:var(--muted)]">bbox estimate</span>
                  ) : (
                    <span className="text-[color:var(--muted)]">calibrate scale</span>
                  )}
                </td>
                <td className="p-3 stat mono">{Math.round(r.confidence * 100)}%</td>
                <td className="p-3">
                  <Button
                    variant="outline"
                    size="sm"
                    data-testid={`select-room-${r.id}`}
                    onClick={() => onSelect(r.id)}
                  >
                    focus
                  </Button>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
