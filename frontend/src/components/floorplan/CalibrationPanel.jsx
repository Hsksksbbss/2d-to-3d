import { useMemo, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { toast } from "sonner";

/**
 * Manual scale calibration.
 *
 * The user clicks two points on the original floor plan image and enters
 * the real-world distance between those points (meters). We compute a new
 * px/m and lift it back to the parent through onCalibrate({ pxPerM }).
 */
export default function CalibrationPanel({ plan, onCalibrate }) {
  const imgRef = useRef(null);
  const [points, setPoints] = useState([]);
  const [realMeters, setRealMeters] = useState("");
  const scaleCertain = plan?.analysis?.px_per_m_confidence >= 0.7;

  const distPx = useMemo(() => {
    if (points.length !== 2) return 0;
    const [a, b] = points;
    return Math.hypot(a.x - b.x, a.y - b.y);
  }, [points]);

  if (!plan) return null;

  const onClick = (e) => {
    const rect = imgRef.current.getBoundingClientRect();
    const scale = plan.analysis.image_width_px / rect.width;
    const px = (e.clientX - rect.left) * scale;
    const py = (e.clientY - rect.top) * scale;
    setPoints((p) => (p.length >= 2 ? [{ x: px, y: py }] : [...p, { x: px, y: py }]));
  };

  const apply = () => {
    const m = parseFloat(realMeters);
    if (!(m > 0) || distPx <= 0) {
      toast.error("Set 2 points and enter meters first");
      return;
    }
    const pxPerM = distPx / m;
    onCalibrate({ pxPerM });
    toast.success("Scale calibrated", {
      description: `${pxPerM.toFixed(1)} px/m`,
    });
  };

  return (
    <div className="card p-5">
      <div className="mono text-xs uppercase tracking-[0.25em] text-[color:var(--muted)] mb-1">
        scale
      </div>
      <h3 className="text-lg font-semibold mb-1">
        {scaleCertain ? "Scale calibrated" : "Calibrate scale"}
      </h3>
      <p className="text-xs text-[color:var(--muted)] mb-3">
        {scaleCertain
          ? "OCR detected building dimensions. You can still override manually."
          : "Click two points on the plan below and enter the real distance."}
      </p>

      <div className="relative border border-[color:var(--line)] rounded-md overflow-hidden">
        <img
          ref={imgRef}
          src={plan.image}
          alt="floor plan"
          className="w-full h-auto cursor-crosshair select-none"
          onClick={onClick}
          draggable={false}
        />
        {points.length > 0 && imgRef.current && (
          <svg
            className="absolute inset-0 pointer-events-none"
            viewBox={`0 0 ${plan.analysis.image_width_px} ${plan.analysis.image_height_px}`}
            preserveAspectRatio="none"
            width="100%"
            height="100%"
          >
            {points.map((p, i) => (
              <circle key={i} cx={p.x} cy={p.y} r="6" fill="#b45309" stroke="white" strokeWidth="2" />
            ))}
            {points.length === 2 && (
              <line
                x1={points[0].x} y1={points[0].y}
                x2={points[1].x} y2={points[1].y}
                stroke="#b45309" strokeWidth="3" strokeDasharray="6 4"
              />
            )}
          </svg>
        )}
      </div>

      <div className="mt-3 grid grid-cols-[1fr_auto] gap-2 items-end">
        <div>
          <Label className="text-xs">Real distance (m)</Label>
          <Input
            type="number"
            step="0.01"
            min="0.1"
            value={realMeters}
            onChange={(e) => setRealMeters(e.target.value)}
            placeholder="e.g. 5.4"
            data-testid="calibrate-meters"
          />
        </div>
        <Button
          onClick={apply}
          data-testid="calibrate-apply"
          className="pill-btn bg-[color:var(--accent)] hover:bg-[color:var(--accent)]/90 text-white"
        >
          Apply
        </Button>
      </div>
      <p className="mt-2 text-xs text-[color:var(--muted)] mono">
        {points.length}/2 points · pixel distance {distPx.toFixed(0)}
      </p>
      <button
        onClick={() => setPoints([])}
        className="mt-1 text-xs text-[color:var(--muted)] hover:underline"
      >
        clear points
      </button>
    </div>
  );
}
