import { Info } from "lucide-react";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";

/** A tap/click help control that is also reachable by keyboard. */
export function InfoHelp({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <Popover>
      <PopoverTrigger asChild>
        <button type="button" aria-label={`About ${label}`} className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-md text-muted-foreground hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
          <Info aria-hidden="true" className="h-3.5 w-3.5" />
        </button>
      </PopoverTrigger>
      <PopoverContent aria-label={`About ${label}`} className="max-w-[calc(100vw-2rem)] space-y-2 text-xs font-normal leading-relaxed normal-case tracking-normal" align="center">
        <p className="font-semibold">{label}</p>
        <div className="text-muted-foreground">{children}</div>
      </PopoverContent>
    </Popover>
  );
}

export function ColumnHeading({ label, help }: { label: string; help: string }) {
  return <span className="inline-flex items-center gap-0.5"><span>{label}</span><InfoHelp label={label}>{help}</InfoHelp></span>;
}
