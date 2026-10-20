import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import { dispositionLabel } from "@/lib/api";

// Per-outcome color variants mirroring web/styles.css disp-* classes.
function dispClasses(disposition: string): string {
  switch (disposition) {
    case "promise_to_pay":
    case "already_paid":
      return "border-emerald-500/40 bg-emerald-500/15 text-emerald-300";
    case "dispute_raised":
    case "inbound_callback_requested":
      return "border-amber-500/40 bg-amber-500/15 text-amber-300";
    case "refusal_to_pay":
      return "border-red-500/40 bg-red-500/15 text-red-300";
    default:
      return "border-slate-500/40 bg-slate-500/15 text-slate-300";
  }
}

export function DispositionBadge({
  disposition,
}: {
  disposition?: string | null;
}) {
  if (!disposition) {
    return (
      <Badge
        variant="outline"
        className="border-dashed italic font-normal text-muted-foreground"
      >
        not yet identified
      </Badge>
    );
  }
  return (
    <Badge variant="outline" className={cn("font-semibold", dispClasses(disposition))}>
      {dispositionLabel(disposition)}
    </Badge>
  );
}
