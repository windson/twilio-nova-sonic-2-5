import { InfoHelp } from "@/components/InfoHelp";
import { Card } from "@/components/ui/card";

export interface MetricTileProps {
  label: string;
  value: string;
  help: string;
}

// A single metric tile with an info tooltip explaining what the number means.
// The help text preserves the explanatory content from the old metrics-note
// paragraph, now attached per-metric as tooltips.
export function MetricTile({ label, value, help }: MetricTileProps) {
  return (
    <Card className="flex flex-col gap-1 bg-background/60 p-3 shadow-none">
      <div className="flex items-center gap-1 text-[0.68rem] font-medium uppercase tracking-wide text-muted-foreground">
        <span>{label}</span>
        <InfoHelp label={label}>{help}</InfoHelp>
      </div>
      <span className="text-xl font-semibold text-foreground">{value}</span>
    </Card>
  );
}
