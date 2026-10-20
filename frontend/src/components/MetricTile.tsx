import { Info } from "lucide-react";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
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
    <Card className="flex flex-col gap-1 bg-background/60 p-3">
      <div className="flex items-center gap-1 text-[0.68rem] font-medium uppercase tracking-wide text-muted-foreground">
        <span>{label}</span>
        <Tooltip>
          <TooltipTrigger asChild>
            <button
              type="button"
              className="text-muted-foreground/70 transition-colors hover:text-foreground"
              aria-label={`About ${label}`}
            >
              <Info className="h-3 w-3" />
            </button>
          </TooltipTrigger>
          <TooltipContent className="max-w-xs text-xs leading-relaxed">
            {help}
          </TooltipContent>
        </Tooltip>
      </div>
      <span className="text-xl font-semibold text-emerald-400">{value}</span>
    </Card>
  );
}
