import { Button } from "@/components/ui/button";
import { VIEWER } from "@/constants/testIds";
import { EyeOff, Eye } from "lucide-react";

export default function RoomInfoPanel({ room, plan, isolated, onIsolate, onShowAll }) {
  if (!plan) return null;
  if (!room) {
    return (
      <div className="card p-5">
        <div className="mono text-xs uppercase tracking-[0.25em] text-[color:var(--muted)] mb-2">
          room info
        </div>
        <p className="text-sm text-[color:var(--muted)]">
          Click a room in the 3D view to see details.
        </p>
      </div>
    );
  }
  return (
    <div className="card p-5">
      <div className="mono text-xs uppercase tracking-[0.25em] text-[color:var(--muted)] mb-1">
        selected room
      </div>
      <h3 className="text-xl font-semibold mb-3">{room.name}</h3>
      <ul className="space-y-1.5 text-sm">
        <Row k="Length" v={`${room.length_m.toFixed(2)} m`} />
        <Row k="Width" v={`${room.width_m.toFixed(2)} m`} />
        <Row k="Area" v={`${room.area_m2.toFixed(2)} m²`} />
        <Row
          k="Dimensions source"
          v={room.dim_certain ? "OCR label" : "bounding box"}
        />
        {room.detected_dim_text && (
          <Row k="Label text" v={<span className="mono">{room.detected_dim_text}</span>} />
        )}
        <Row k="Confidence" v={`${Math.round(room.confidence * 100)}%`} />
      </ul>
      <div className="mt-4 flex flex-col gap-2">
        <Button
          className="pill-btn"
          variant={isolated ? "default" : "outline"}
          data-testid={VIEWER.isolateBtn}
          onClick={onIsolate}
        >
          {isolated ? (
            <><Eye className="w-4 h-4 mr-2" /> Exit isolate</>
          ) : (
            <><EyeOff className="w-4 h-4 mr-2" /> Isolate room</>
          )}
        </Button>
        <Button
          className="pill-btn"
          variant="ghost"
          data-testid={VIEWER.showAllBtn}
          onClick={onShowAll}
        >
          Clear selection
        </Button>
      </div>
    </div>
  );
}

function Row({ k, v }) {
  return (
    <li className="flex justify-between">
      <span className="text-[color:var(--muted)]">{k}</span>
      <span className="stat mono">{v}</span>
    </li>
  );
}
