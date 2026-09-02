import { Slider } from "@/components/ui/slider";
import { Switch } from "@/components/ui/switch";
import { Label } from "@/components/ui/label";

const swatches = {
  wall: ["#f0e6d2", "#ffffff", "#e6dccb", "#c4d4c4", "#dbe4ec"],
  floor: ["#d9c39a", "#c8a67c", "#a67c52", "#8d6748", "#e5d8bd"],
  door: ["#a97142", "#7c4a1e", "#5b3a1a", "#c99a6b"],
  window: ["#7fb1c9", "#a8c7d8", "#c0d6df", "#5b8ba0"],
};

export default function MaterialControls({ materials, setMaterials }) {
  const set = (k, v) => setMaterials((m) => ({ ...m, [k]: v }));
  return (
    <div className="card p-6 space-y-6">
      <ColorRow label="Wall color" value={materials.wallColor}
                onChange={(v) => set("wallColor", v)} choices={swatches.wall} />
      <ColorRow label="Floor color" value={materials.floorColor}
                onChange={(v) => set("floorColor", v)} choices={swatches.floor} />
      <ColorRow label="Door color" value={materials.doorColor}
                onChange={(v) => set("doorColor", v)} choices={swatches.door} />
      <ColorRow label="Window color" value={materials.windowColor}
                onChange={(v) => set("windowColor", v)} choices={swatches.window} />

      <div>
        <div className="flex items-center justify-between mb-2">
          <Label>Wall height (default)</Label>
          <span className="mono stat text-sm">{materials.wallHeight.toFixed(2)} m</span>
        </div>
        <Slider
          value={[materials.wallHeight]}
          min={2.1} max={4.5} step={0.05}
          onValueChange={(v) => set("wallHeight", v[0])}
        />
      </div>
      <div>
        <div className="flex items-center justify-between mb-2">
          <Label>Wall thickness (default)</Label>
          <span className="mono stat text-sm">
            {(materials.wallThickness * 100).toFixed(1)} cm
          </span>
        </div>
        <Slider
          value={[materials.wallThickness]}
          min={0.06} max={0.3} step={0.005}
          onValueChange={(v) => set("wallThickness", v[0])}
        />
      </div>

      <div className="flex items-center justify-between">
        <Label>Show furniture (when detected)</Label>
        <Switch
          checked={materials.showFurniture}
          onCheckedChange={(v) => set("showFurniture", v)}
        />
      </div>

      <p className="text-xs text-[color:var(--muted)]">
        Wall height, door height, window height and wall thickness are
        <b> defaults</b> — they are not measured from the drawing.
      </p>
    </div>
  );
}

function ColorRow({ label, value, onChange, choices }) {
  return (
    <div>
      <Label>{label}</Label>
      <div className="mt-2 flex items-center gap-2 flex-wrap">
        {choices.map((c) => (
          <button
            key={c}
            aria-label={c}
            onClick={() => onChange(c)}
            style={{ background: c }}
            className={`w-7 h-7 rounded-full border transition-transform hover:scale-110 ${
              value === c ? "ring-2 ring-offset-2 ring-[color:var(--ink)]" : "border-[color:var(--line)]"
            }`}
          />
        ))}
        <input
          type="color"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          className="w-9 h-9 rounded cursor-pointer border border-[color:var(--line)]"
        />
      </div>
    </div>
  );
}
