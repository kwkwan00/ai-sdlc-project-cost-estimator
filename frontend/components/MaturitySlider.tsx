"use client";

const LEVEL_LABELS = [
  "No AI (manual)",
  "Exploring (some tools)",
  "Adopting (AI-assisted features)",
  "Integrated (AI handles major work)",
  "Advanced / Agentic",
];

interface Props {
  label: string;
  value: number; // 1..5
  onChange: (v: number) => void;
}

export function MaturitySlider({ label, value, onChange }: Props) {
  return (
    <div>
      <div className="flex justify-between text-sm">
        <span className="text-slate-700">{label}</span>
        <span className="font-mono text-slate-900">L{value}</span>
      </div>
      <input
        type="range"
        min={1}
        max={5}
        step={1}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        className="w-full accent-brand-600"
      />
      <p className="help">{LEVEL_LABELS[value - 1]}</p>
    </div>
  );
}
