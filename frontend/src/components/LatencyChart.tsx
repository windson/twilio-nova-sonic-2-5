import {
  CartesianGrid,
  Scatter,
  ScatterChart,
  XAxis,
  YAxis,
  ZAxis,
} from "recharts";
import {
  ChartContainer,
  ChartTooltip,
  type ChartConfig,
} from "@/components/ui/chart";
import type { LatencyPoint } from "@/lib/selectors";
import { fmtMs } from "@/lib/api";

const chartConfig = {
  sonic: { label: "Sonic", color: "hsl(152 60% 50%)" },
  caller: { label: "Caller", color: "hsl(220 83% 65%)" },
} satisfies ChartConfig;

// Scatter / timeline of Sonic vs caller events over the call. x = ms since
// call start (from events_log t_ms), y = lane (Caller lane 0, Sonic lane 1).
// Notes mirror the reference design's annotations.
export function LatencyChart({ points }: { points: LatencyPoint[] }) {
  if (!points.length) {
    return (
      <p className="py-6 text-center text-sm italic text-muted-foreground">
        No timeline events recorded.
      </p>
    );
  }

  const sonic = points
    .filter((p) => p.lane === 1)
    .map((p) => ({ x: p.tMs, y: 1, z: 1, label: p.label }));
  const caller = points
    .filter((p) => p.lane === 0)
    .map((p) => ({ x: p.tMs, y: 0, z: 1, label: p.label }));

  return (
    <div className="space-y-3">
      <ul className="space-y-1 text-[0.7rem] text-muted-foreground">
        <li>
          <span className="font-semibold uppercase tracking-wide text-muted-foreground">
            Background: excluded from call latency
          </span>{" "}
          &ndash; No async side-effect writes
        </li>
        <li>call setup / first ops</li>
        <li>Sonic: stream open</li>
      </ul>
      <ChartContainer config={chartConfig} className="h-[220px] w-full">
        <ScatterChart margin={{ top: 10, right: 16, bottom: 20, left: 0 }}>
          <CartesianGrid strokeDasharray="3 3" />
          <XAxis
            type="number"
            dataKey="x"
            name="time"
            unit=" ms"
            tickLine={false}
            axisLine={false}
            tickMargin={8}
          />
          <YAxis
            type="number"
            dataKey="y"
            domain={[-0.5, 1.5]}
            ticks={[0, 1]}
            tickFormatter={(v: number) => (v === 1 ? "Sonic" : "Caller")}
            tickLine={false}
            axisLine={false}
            width={60}
          />
          <ZAxis type="number" dataKey="z" range={[60, 60]} />
          <ChartTooltip
            cursor={{ strokeDasharray: "3 3" }}
            content={({ active, payload }) => {
              if (!active || !payload || !payload.length) return null;
              const p = payload[0].payload as {
                x: number;
                y: number;
                label: string;
              };
              return (
                <div className="rounded-md border bg-popover px-2.5 py-1.5 text-xs shadow-md">
                  <div className="font-medium">{p.label}</div>
                  <div className="text-muted-foreground">
                    {p.y === 1 ? "Sonic" : "Caller"} &middot; {fmtMs(p.x)}
                  </div>
                </div>
              );
            }}
          />
          <Scatter name="Sonic" data={sonic} fill="var(--color-sonic)" />
          <Scatter name="Caller" data={caller} fill="var(--color-caller)" />
        </ScatterChart>
      </ChartContainer>
    </div>
  );
}
