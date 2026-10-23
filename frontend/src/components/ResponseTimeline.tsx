import { useRef, useState, type ReactNode } from "react";
import { Info } from "lucide-react";
import { fmtMs } from "@/lib/api";
import { METRIC_HELP } from "@/lib/metric-help";
import { responseTimeline, type ChatSegment } from "@/lib/selectors";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

const preciseMs = (value: number) => `${value.toLocaleString("en-US", { maximumFractionDigits: 1 })} ms`;

/** The whole label is a 24px target: hover/focus for a tip, tap for persistent help. */
function TimingHelp({ label, children, help }: { label: string; children?: ReactNode; help: ReactNode }) {
  const [pinned, setPinned] = useState(false);
  const [hovered, setHovered] = useState(false);
  const restoringFocus = useRef(false);
  return (
    <Popover open={pinned} onOpenChange={(open) => { setPinned(open); if (open) setHovered(false); }}>
      <Tooltip open={hovered && !pinned} onOpenChange={(open) => { if (!open || !restoringFocus.current) setHovered(open); }}>
        <TooltipTrigger asChild>
          <PopoverTrigger asChild>
            <button type="button" aria-label={`About ${label}`} className="inline-flex h-6 shrink-0 items-center gap-1 rounded-sm text-[0.65rem] text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
              {children ?? label}<Info aria-hidden="true" className="h-3 w-3 shrink-0" />
            </button>
          </PopoverTrigger>
        </TooltipTrigger>
        <TooltipContent className="max-w-[min(20rem,calc(100vw-2rem))] border bg-popover p-2.5 text-xs leading-relaxed text-popover-foreground">{help}</TooltipContent>
      </Tooltip>
      <PopoverContent aria-label={`About ${label}`} onCloseAutoFocus={() => {
        // Keep Radix's native focus restoration without reopening the tooltip.
        restoringFocus.current = true;
        setHovered(false);
        queueMicrotask(() => { restoringFocus.current = false; });
      }} className="max-h-[var(--radix-popover-content-available-height)] max-w-[calc(100vw-2rem)] space-y-1.5 overflow-y-auto text-xs leading-relaxed">
        <p className="font-semibold">{label}</p>
        <div className="text-muted-foreground">{help}</div>
      </PopoverContent>
    </Popover>
  );
}

export function ResponseTimeline({ segment }: { segment: ChatSegment }) {
  const values = responseTimeline(segment);
  if (values.text === null && values.audio === null) return null;
  const metrics = [
    { label: "TTFT", value: values.text, color: "bg-violet-600 dark:bg-violet-400", help: METRIC_HELP.textTtft },
    { label: "TTFA", value: values.audio, color: "bg-emerald-700 dark:bg-emerald-400", help: METRIC_HELP.ttfa },
  ];
  const total = values.duration;
  const scale = total !== null && total > 0 ? total : 1;
  const timelineHelp = <>{METRIC_HELP.responseTimeline}{" "}
    {total === null ? "The response end is missing or unmatched, so no duration axis is drawn." :
      total === 0 ? "Response start and end share the same recorded instant: 0 ms." :
        `This response lasted ${preciseMs(total)}.${values.audio !== null ? ` It continued for ${preciseMs(total - values.audio)} after first audio.` : ""}`}
    {" "}Values are rounded. A dash means unavailable, not zero.</>;
  return (
    <figure className="mt-1.5 border-t border-border pt-1" aria-label="Response timeline">
      <figcaption className="sr-only">TTFT and TTFA on the same response-duration axis</figcaption>
      <div className="grid grid-cols-[2.5rem_minmax(0,1fr)_3.25rem] items-center gap-x-1 text-[0.65rem]">
        {metrics.map((metric) => <div key={metric.label} className="contents">
          <TimingHelp label={metric.label} help={<>{metric.value !== null ? `0 ms → ${preciseMs(metric.value)}. ` : ""}{metric.help}</>}>{metric.label}</TimingHelp>
          <div aria-hidden="true" className="relative h-1.5 rounded-sm bg-border">
            {total !== null && total > 0 && <>
              {[0, 50, 100].map((left) => <span key={left} className="absolute top-0 h-full border-l border-border-strong" style={{ left: `${left}%`, transform: left === 100 ? "translateX(-1px)" : undefined }} />)}
              {metric.value !== null && <span className={`absolute left-0 top-0 h-full ${metric.color} ${metric.value === 0 ? "w-px" : "rounded-sm"}`} style={metric.value === 0 ? undefined : { width: `${(metric.value / scale) * 100}%` }} />}
            </>}
          </div>
          <span className="whitespace-nowrap text-right font-medium tabular-nums">{metric.value !== null ? preciseMs(metric.value) : fmtMs(null)}</span>
        </div>)}
        <span className="sr-only">Elapsed response time</span>
        <div className="col-start-2 flex min-h-6 items-center justify-between whitespace-nowrap text-[0.6rem] tabular-nums text-muted-foreground" aria-label={total !== null ? `Elapsed response time: 0 ms to ${preciseMs(total)}` : "Response end not recorded or unmatched"}>
          {total !== null ? <><span>0 ms</span><span>{preciseMs(total)}</span></> : <span>End unavailable</span>}
        </div>
        <div className="flex justify-end"><TimingHelp label="Response duration" help={timelineHelp}>Time</TimingHelp></div>
      </div>
    </figure>
  );
}

export function TimingGuide() {
  return (
    <div className="flex items-center">
      <TimingHelp label="Timing guide" help={<div className="space-y-2">
        <p>{METRIC_HELP.responseTimeline}</p>
        <p><strong>Reply gap:</strong> {METRIC_HELP.callerReplyGap}</p>
        <p><strong>Until next transcript:</strong> {METRIC_HELP.transcriptInterval}</p>
        <p>{METRIC_HELP.utterance}</p>
      </div>} />
    </div>
  );
}
