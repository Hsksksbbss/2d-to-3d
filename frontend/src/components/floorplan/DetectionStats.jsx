import { STATS } from "@/constants/testIds";

export default function DetectionStats({ stats }) {
  const items = stats
    ? [
        { k: "walls", v: stats.walls, t: STATS.walls },
        { k: "rooms", v: stats.rooms, t: STATS.rooms },
        { k: "doors", v: stats.doors, t: STATS.doors },
        { k: "windows", v: stats.windows, t: STATS.windows },
      ]
    : [];
  return (
    <div className="card p-5">
      <div className="mono text-xs uppercase tracking-[0.25em] text-[color:var(--muted)] mb-3">
        detections
      </div>
      {!stats ? (
        <p className="text-sm text-[color:var(--muted)]">
          Upload a plan to see detection counts.
        </p>
      ) : (
        <>
          <div className="grid grid-cols-2 gap-3">
            {items.map((it) => (
              <div
                key={it.k}
                data-testid={it.t}
                className="rounded-xl border border-[color:var(--line)] p-3"
              >
                <div className="mono text-[10px] uppercase tracking-widest text-[color:var(--muted)]">
                  {it.k}
                </div>
                <div className="text-3xl font-semibold stat mono">{it.v}</div>
              </div>
            ))}
          </div>
          <div className="mt-4 text-xs text-[color:var(--muted)]">
            Scale: <span className="mono stat">{stats.pxPerM.toFixed(1)} px/m</span>{" "}
            <span className="ml-2">
              (
              {stats.pxPerMConf >= 0.7 ? "calibrated from dimensions" : "fallback estimate"}
              )
            </span>
          </div>
        </>
      )}
    </div>
  );
}
