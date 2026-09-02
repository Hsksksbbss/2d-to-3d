export default function DebugView({ plan }) {
  const dbg = plan.analysis.debug || {};
  const stages = [
    { key: "original", label: "1 · Original" },
    { key: "cleaned", label: "2 · Threshold" },
    { key: "text_mask", label: "3 · Text mask" },
    { key: "walls_mask", label: "4 · Walls mask" },
    { key: "walls_vector", label: "5 · Wall vectors" },
    { key: "rooms", label: "6 · Rooms" },
    { key: "doors", label: "7 · Doors" },
    { key: "windows", label: "8 · Windows" },
  ];
  return (
    <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
      {stages.map(({ key, label }) => (
        <div key={key} className="card p-3">
          <div className="mono text-xs uppercase tracking-[0.2em] text-[color:var(--muted)] mb-2">
            {label}
          </div>
          {dbg[key] ? (
            <img
              src={dbg[key]}
              alt={label}
              className="w-full h-auto rounded-md border border-[color:var(--line)]"
            />
          ) : (
            <div className="text-sm text-[color:var(--muted)]">not available</div>
          )}
        </div>
      ))}
      <div className="card p-4 md:col-span-2">
        <div className="mono text-xs uppercase tracking-[0.2em] text-[color:var(--muted)] mb-2">
          9 · dimensions parsed
        </div>
        {plan.analysis.dimensions.length === 0 ? (
          <div className="text-sm text-[color:var(--muted)]">
            No dimension strings recognised.
          </div>
        ) : (
          <ul className="text-sm space-y-1 mono">
            {plan.analysis.dimensions.map((d, i) => (
              <li key={i}>
                <span className="text-[color:var(--muted)]">•</span>{" "}
                {d.text}
                {d.width_ft && d.height_ft && (
                  <span className="text-[color:var(--accent-2)]">
                    {" "}
                    → {d.width_ft.toFixed(2)}&apos; × {d.height_ft.toFixed(2)}&apos;
                  </span>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
