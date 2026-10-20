import { Fragment, useCallback, useMemo, useState } from "react";
import {
  ChevronDown,
  ExternalLink,
  Hand,
  Info,
  PhoneCall,
  Settings2,
} from "lucide-react";

import {
  apiPost,
  callDashboardUrl,
  DASH,
  dispositionLabel,
  fmtDateTime,
  fmtMoney,
  fmtMs,
  fmtRelativeTime,
  fmtText,
  type CallRecord,
  type CallSummary,
  type DueRecord,
  type ToolCall,
} from "@/lib/api";
import {
  chatSegments,
  connectionRestarts,
  latencyPoints,
} from "@/lib/selectors";
import {
  useCallDetail,
  useCalls,
  useConfig,
  useDues,
} from "@/hooks";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Separator } from "@/components/ui/separator";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";

import { DispositionBadge } from "@/components/DispositionBadge";
import { LatencyChart } from "@/components/LatencyChart";
import { MetricTile } from "@/components/MetricTile";

// --- metric help text (preserves the old metrics-note explanatory content) --
const METRIC_HELP = {
  total:
    "End-to-end wall-clock time of the whole call. It partitions into Nova Sonic total time plus its true complement, non-Sonic time.",
  nova: "Nova Sonic total time: the sum of active response (speaking) windows.",
  nonnova:
    "Non-Sonic time: everything that is not Nova speaking (caller speech, silence, and transport). A bookkeeping residual equal to total minus Nova, NOT a transport-latency figure.",
  twilio:
    "Transport delay estimate: a per-frame mean derived from Twilio stream-relative timestamps (an estimate, not an authoritative network measurement). When the real Twilio Call resource is available it is shown in the Twilio insights panel below; advanced QoS (RTT/jitter/packet-loss) needs a paid add-on and is never fabricated.",
  ttft: "Time to first audio (TTFT): from the response start to the first audio frame of Nova's reply.",
  turns: "Number of turns in the conversation (one caller utterance or one Nova reply each).",
  barge: "Count of barge-ins (caller interrupting Nova mid-reply).",
  // FEAT-004: per-turn chat-bubble meta.
  callerReplyGap:
    "Caller\u2192reply gap: time from the caller finishing speaking to the agent beginning its reply. Shown only when a caller turn precedes the agent turn (blank for back-to-back agent turns). This is NOT the same thing as TTFT (time to first audio).",
  turnElapsed:
    "Turn elapsed: how long this turn occupied the conversation (to the next turn's start). An elapsed window, NOT latency.",
};

function Toast({
  toast,
}: {
  toast: { message: string; error: boolean } | null;
}) {
  if (!toast) return null;
  return (
    <div
      className={
        "mt-3 rounded-lg border px-4 py-2.5 text-sm " +
        (toast.error
          ? "border-destructive bg-destructive/15 text-red-300"
          : "border-emerald-500/50 bg-emerald-500/15 text-emerald-300")
      }
    >
      {toast.message}
    </div>
  );
}

export default function App() {
  const config = useConfig();
  const { dues, loading: duesLoading, error: duesError } = useDues();
  const { calls, loading: callsLoading, error: callsError, reload: reloadCalls } =
    useCalls();

  const [selectedCallId, setSelectedCallId] = useState<string | null>(null);
  const [live, setLive] = useState(false);
  const [toast, setToast] = useState<{ message: string; error: boolean } | null>(
    null
  );
  const [calling, setCalling] = useState<string | null>(null);

  const showToast = useCallback((message: string, error = false) => {
    setToast({ message, error });
    window.setTimeout(() => setToast(null), 5000);
  }, []);

  const { record, isLive } = useCallDetail(
    selectedCallId,
    live,
    reloadCalls
  );

  const placeCall = useCallback(
    async (accountId?: string) => {
      if (!accountId) return;
      setCalling(accountId);
      try {
        const result = await apiPost<{ call_sid?: string }>("/api/calls", {
          account_id: accountId,
        });
        showToast(
          `Outbound call initiated for ${accountId}: ${result.call_sid ?? ""}`
        );
        await reloadCalls();
        if (result.call_sid) {
          setSelectedCallId(result.call_sid);
          setLive(true);
        }
      } catch (e) {
        showToast(`Could not place call: ${(e as Error).message}`, true);
      } finally {
        setCalling(null);
      }
    },
    [reloadCalls, showToast]
  );

  const selectCall = useCallback((callId?: string) => {
    if (!callId) return;
    setSelectedCallId(callId);
    setLive(false);
  }, []);

  // Debtor name for the header: prefer the matching due record.
  const debtorName = useMemo(() => {
    if (!record?.account_id) return undefined;
    const due = dues.find((d) => d.account_id === record.account_id);
    return due?.debtor_name;
  }, [record?.account_id, dues]);

  return (
    <TooltipProvider delayDuration={150}>
      <div className="mx-auto max-w-[1200px] px-5 pb-16 pt-6">
        <Header
          config={config}
          twilioTrial={config?.twilio_trial_mode}
        />
        <Toast toast={toast} />

        <div className="mt-6 grid grid-cols-1 gap-5 lg:grid-cols-2">
          <DuesPanel
            dues={dues}
            calls={calls}
            loading={duesLoading}
            error={duesError}
            calling={calling}
            onCall={placeCall}
            onSelectCall={selectCall}
          />
          <CallsPanel
            calls={calls}
            loading={callsLoading}
            error={callsError}
            selectedCallId={selectedCallId}
            onSelect={selectCall}
            config={config}
          />
        </div>

        {selectedCallId && (
          <CallDetail
            callId={selectedCallId}
            record={record}
            isLive={isLive}
            debtorName={debtorName}
            config={config}
          />
        )}

        <Footer config={config} />
      </div>
    </TooltipProvider>
  );
}

// --- header -----------------------------------------------------------------

function Header({
  config,
  twilioTrial,
}: {
  config: ReturnType<typeof useConfig>;
  twilioTrial?: boolean;
}) {
  return (
    <header className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">
          AR Collections &middot; Nova Sonic 2.5
        </h1>
        <p className="mt-1 max-w-3xl text-sm text-muted-foreground">
          AI-first-initiated outbound collections calling. Twilio is telephony
          transport only; Amazon Bedrock Nova Sonic 2.5 does speech
          understanding, reasoning, and speech generation in the female voice{" "}
          <strong className="text-foreground">tiffany</strong>.
        </p>
      </div>
      <div className="flex shrink-0 items-center gap-2">
        {config?.nova_model_id && (
          <Badge variant="secondary" className="font-mono text-[0.7rem]">
            {config.nova_model_id}
          </Badge>
        )}
        {twilioTrial && (
          <Badge variant="outline" className="text-[0.7rem] text-amber-300">
            Twilio trial
          </Badge>
        )}
      </div>
    </header>
  );
}

// --- dues panel -------------------------------------------------------------

function statusClasses(status?: string | null): string {
  switch (status) {
    case "paid":
    case "promised_to_pay":
      return "border-emerald-500/40 bg-emerald-500/15 text-emerald-300";
    case "disputed":
      return "border-red-500/40 bg-red-500/15 text-red-300";
    case "callback":
      return "border-amber-500/40 bg-amber-500/15 text-amber-300";
    default:
      return "border-slate-500/40 bg-slate-500/15 text-slate-300";
  }
}

// ASK 3: one collapsible per due/account row (collapsed by default) that
// expands to the calls associated with that account, joined client-side by
// account_id against the /api/calls list. Each nested call row shows WHEN it
// happened (absolute + relative), disposition, duration, and outcome.
function DueRow({
  due,
  calls,
  calling,
  onCall,
  onSelectCall,
}: {
  due: DueRecord;
  calls: CallSummary[];
  calling: string | null;
  onCall: (accountId?: string) => void;
  onSelectCall?: (callId?: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const accountCalls = useMemo(
    () =>
      calls
        .filter((c) => c.account_id && c.account_id === due.account_id)
        .slice()
        .sort((a, b) => {
          const ta = a.created_at ? new Date(a.created_at).getTime() : 0;
          const tb = b.created_at ? new Date(b.created_at).getTime() : 0;
          return tb - ta;
        }),
    [calls, due.account_id]
  );

  return (
    <Collapsible
      open={open}
      onOpenChange={setOpen}
      className="rounded-md border"
    >
      <div className="flex items-center gap-3 px-3 py-2.5">
        <CollapsibleTrigger className="flex min-w-0 flex-1 items-center gap-2 text-left">
          <ChevronDown
            className={
              "h-3.5 w-3.5 shrink-0 transition-transform " +
              (open ? "rotate-0" : "-rotate-90")
            }
          />
          <div className="grid min-w-0 flex-1 grid-cols-2 items-center gap-x-3 gap-y-0.5 sm:grid-cols-4">
            <span className="truncate font-medium">
              {due.account_id || ""}
              {accountCalls.length > 0 && (
                <span className="ml-1 rounded-full bg-muted px-1.5 text-[0.65rem] text-muted-foreground">
                  {accountCalls.length}
                </span>
              )}
            </span>
            <span className="truncate text-muted-foreground">
              {due.debtor_name || ""}
            </span>
            <span className="text-muted-foreground">
              {fmtMoney(due.amount_due, due.currency)}
            </span>
            <span>
              <Badge variant="outline" className={statusClasses(due.status)}>
                {due.status || ""}
              </Badge>
            </span>
          </div>
        </CollapsibleTrigger>
        <Button
          size="sm"
          disabled={calling === due.account_id}
          onClick={() => onCall(due.account_id)}
        >
          <PhoneCall className="mr-1 h-3.5 w-3.5" />
          {calling === due.account_id ? "Calling\u2026" : "Call now"}
        </Button>
      </div>
      <CollapsibleContent>
        <div className="border-t bg-muted/30 px-3 py-2">
          <div className="mb-1 text-xs text-muted-foreground">
            Due date: {due.due_date || DASH}
          </div>
          {accountCalls.length === 0 ? (
            <p className="py-1 text-xs italic text-muted-foreground">
              No calls recorded for this account yet.
            </p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>When</TableHead>
                  <TableHead>Disposition</TableHead>
                  <TableHead>Duration</TableHead>
                  <TableHead>Outcome</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {accountCalls.map((c) => {
                  const rel = fmtRelativeTime(c.created_at);
                  return (
                    <TableRow
                      key={c.call_sid}
                      className="cursor-pointer"
                      onClick={() => onSelectCall?.(c.call_sid)}
                    >
                      <TableCell className="whitespace-nowrap text-xs">
                        {fmtDateTime(c.created_at)}
                        {rel && (
                          <span className="ml-1 text-muted-foreground">
                            ({rel})
                          </span>
                        )}
                      </TableCell>
                      <TableCell>
                        <DispositionBadge disposition={c.disposition} />
                      </TableCell>
                      <TableCell className="text-xs">
                        {fmtMs(c.total_e2e_ms)}
                      </TableCell>
                      <TableCell className="text-xs">
                        {c.disposition
                          ? dispositionLabel(c.disposition)
                          : c.intelligence_status === "pending"
                            ? "Processing\u2026"
                            : DASH}
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          )}
        </div>
      </CollapsibleContent>
    </Collapsible>
  );
}

function DuesPanel({
  dues,
  calls,
  loading,
  error,
  calling,
  onCall,
  onSelectCall,
}: {
  dues: DueRecord[];
  calls: CallSummary[];
  loading: boolean;
  error: string | null;
  calling: string | null;
  onCall: (accountId?: string) => void;
  onSelectCall?: (callId?: string) => void;
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Due records</CardTitle>
        <CardDescription>
          Expand an account to see its calls, most recent first.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-2">
        {loading && <Skeleton className="h-10 w-full" />}
        {error && (
          <p className="text-center text-sm text-muted-foreground">
            Failed to load dues: {error}
          </p>
        )}
        {!loading && !error && dues.length === 0 && (
          <p className="text-center text-sm italic text-muted-foreground">
            No due records.
          </p>
        )}
        {dues.map((due) => (
          <DueRow
            key={due.account_id}
            due={due}
            calls={calls}
            calling={calling}
            onCall={onCall}
            onSelectCall={onSelectCall}
          />
        ))}
      </CardContent>
    </Card>
  );
}

// --- calls panel ------------------------------------------------------------

function CallsPanel({
  calls,
  loading,
  error,
  selectedCallId,
  onSelect,
  config,
}: {
  calls: ReturnType<typeof useCalls>["calls"];
  loading: boolean;
  error: string | null;
  selectedCallId: string | null;
  onSelect: (callId?: string) => void;
  config: ReturnType<typeof useConfig>;
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Past calls</CardTitle>
      </CardHeader>
      <CardContent>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Call SID</TableHead>
              <TableHead>Account</TableHead>
              <TableHead>Disposition</TableHead>
              <TableHead>Turns</TableHead>
              <TableHead>Total e2e</TableHead>
              <TableHead>Dashboard</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {loading && (
              <TableRow>
                <TableCell colSpan={6}>
                  <Skeleton className="h-5 w-full" />
                </TableCell>
              </TableRow>
            )}
            {error && (
              <TableRow>
                <TableCell colSpan={6} className="text-center text-muted-foreground">
                  Failed to load calls: {error}
                </TableCell>
              </TableRow>
            )}
            {!loading && !error && calls.length === 0 && (
              <TableRow>
                <TableCell colSpan={6} className="text-center italic text-muted-foreground">
                  No calls yet.
                </TableCell>
              </TableRow>
            )}
            {calls.map((call) => {
              const selected = call.call_sid === selectedCallId;
              return (
                <TableRow
                  key={call.call_sid}
                  onClick={() => onSelect(call.call_sid)}
                  className={
                    "cursor-pointer " + (selected ? "bg-primary/15" : "")
                  }
                >
                  <TableCell className="font-mono text-xs">
                    {call.call_sid || ""}
                  </TableCell>
                  <TableCell>{call.account_id || ""}</TableCell>
                  <TableCell>
                    {call.disposition ? (
                      <DispositionBadge disposition={call.disposition} />
                    ) : (
                      <span className="text-muted-foreground">{DASH}</span>
                    )}
                  </TableCell>
                  <TableCell>
                    {call.turn_count != null ? call.turn_count : DASH}
                  </TableCell>
                  <TableCell>{fmtMs(call.total_e2e_ms)}</TableCell>
                  <TableCell>
                    {call.call_sid && (
                      <CloudWatchLink
                        callSid={call.call_sid}
                        dashboardUrl={call.dashboard_url}
                        config={config}
                      />
                    )}
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  );
}

// ASK 4: per-call CloudWatch DASHBOARD deep link in a popover so the user can
// read exactly what it opens (region/log group) before following it. Prefers
// the backend-built dashboard_url and falls back to the client-side builder.
function CloudWatchLink({
  callSid,
  dashboardUrl,
  config,
}: {
  callSid: string;
  dashboardUrl?: string | null;
  config: ReturnType<typeof useConfig>;
}) {
  const url =
    dashboardUrl ||
    callDashboardUrl(callSid, config?.aws_region, config?.app_log_group);
  return (
    <Popover>
      <PopoverTrigger asChild onClick={(e) => e.stopPropagation()}>
        <button className="inline-flex items-center gap-1 text-xs text-primary hover:underline">
          Call dashboard <ExternalLink className="h-3 w-3" />
        </button>
      </PopoverTrigger>
      <PopoverContent
        className="w-80 text-xs"
        onClick={(e) => e.stopPropagation()}
      >
        <p className="mb-2 text-muted-foreground">
          Opens a CloudWatch dashboard scoped to this call (TTFT, turns, token
          usage, barge-ins, transport, timeline).
        </p>
        <dl className="mb-3 space-y-1">
          <div className="flex justify-between gap-2">
            <dt className="text-muted-foreground">Region</dt>
            <dd className="font-mono">{config?.aws_region || "us-east-1"}</dd>
          </div>
          <div className="flex justify-between gap-2">
            <dt className="text-muted-foreground">Log group</dt>
            <dd className="truncate font-mono">
              {config?.app_log_group || "/ecs/ar-collections-nova-sonic/app"}
            </dd>
          </div>
          <div className="flex justify-between gap-2">
            <dt className="text-muted-foreground">Scoped to call</dt>
            <dd className="truncate font-mono">{callSid}</dd>
          </div>
        </dl>
        <a
          href={url}
          target="_blank"
          rel="noopener"
          className="inline-flex items-center gap-1 font-medium text-primary hover:underline"
        >
          Open CloudWatch dashboard <ExternalLink className="h-3 w-3" />
        </a>
      </PopoverContent>
    </Popover>
  );
}

// --- call detail ------------------------------------------------------------

function CallDetail({
  callId,
  record,
  isLive,
  debtorName,
  config,
}: {
  callId: string;
  record: CallRecord | null;
  isLive: boolean;
  debtorName?: string;
  config: ReturnType<typeof useConfig>;
}) {
  const r = record;
  const status = r?.total_e2e_ms != null ? "COMPLETED" : "IN PROGRESS";
  const restarts = r ? connectionRestarts(r) : 0;
  const points = useMemo(() => (r ? latencyPoints(r) : []), [r]);
  const segments = useMemo(() => (r ? chatSegments(r) : []), [r]);

  return (
    <Card className="mt-5">
      <CardHeader>
        <div className="flex flex-wrap items-center gap-2">
          <CardTitle className="text-lg">Call attempt</CardTitle>
          <span className="font-mono text-xs text-muted-foreground">{callId}</span>
          {isLive && (
            <Badge className="animate-pulse bg-destructive text-destructive-foreground">
              live
            </Badge>
          )}
        </div>
        <CardDescription>
          Transcript, summary, and the overdue balances discussed in the call
        </CardDescription>
        <div className="mt-2 flex flex-wrap items-center gap-2">
          {debtorName && (
            <span className="text-sm font-medium">{debtorName}</span>
          )}
          {r?.account_id && !debtorName && (
            <span className="text-sm font-medium">{r.account_id}</span>
          )}
          <DispositionBadge disposition={r?.disposition} />
          <IntelStatusBadge status={r?.intelligence_status} />
        </div>
        <ScheduledRow record={r} />
      </CardHeader>

      <CardContent>
        <Tabs defaultValue="telemetry" className="w-full">
          <TabsList>
            <TabsTrigger value="telemetry">Call telemetry</TabsTrigger>
            <TabsTrigger value="transcript">Transcript</TabsTrigger>
            <TabsTrigger value="conversation">Conversation</TabsTrigger>
            <TabsTrigger value="intelligence">Intelligence</TabsTrigger>
          </TabsList>

          {/* --- Call telemetry ------------------------------------------- */}
          <TabsContent value="telemetry" className="space-y-5">
            <MetricTiles record={r} />

            <Separator />

            <div>
              <h3 className="mb-2 text-sm font-semibold">Summary</h3>
              <dl className="grid grid-cols-2 gap-x-6 gap-y-2 text-sm sm:grid-cols-3">
                <SummaryField label="Total duration" value={fmtMs(r?.total_e2e_ms)} />
                <SummaryField label="Status" value={status} />
                <SummaryField
                  label="Outcome"
                  value={r?.disposition ? dispositionLabel(r.disposition) : DASH}
                />
                <SummaryField label="Connection restarts" value={String(restarts)} />
                <SummaryField label="Transport" value="MEDIA_STREAMS" />
                <SummaryField
                  label="Model"
                  value={config?.nova_model_id || "amazon.nova-2-5-sonic"}
                />
              </dl>
            </div>

            <Separator />

            <TwilioInsightsPanel record={r} />

            <Separator />

            <div>
              <h3 className="mb-2 text-sm font-semibold">Turn-by-turn metrics</h3>
              <TurnLatencyTable record={r} />
              <AsyncToolCalls record={r} />
            </div>

            <Separator />

            <div>
              <h3 className="mb-2 text-sm font-semibold">Latency timeline</h3>
              <LatencyChart points={points} />
            </div>

            <RawEventLog record={r} />
          </TabsContent>

          {/* --- Transcript ----------------------------------------------- */}
          <TabsContent value="transcript">
            <TranscriptPanel segments={segments} />
          </TabsContent>

          {/* --- Conversation (chat + per-utterance table) ---------------- */}
          <TabsContent value="conversation" className="space-y-5">
            <ChatView record={r} />
            <Separator />
            <div>
              <h3 className="mb-1 text-sm font-semibold">Per-turn timeline</h3>
              <p className="mb-2 text-xs text-muted-foreground">
                One row per utterance: a caller utterance is one turn and a Nova
                Sonic reply is another, counted in spoken order with the duration
                of each.
              </p>
              <PerUtteranceTable record={r} />
            </div>
          </TabsContent>

          {/* --- Intelligence --------------------------------------------- */}
          <TabsContent value="intelligence">
            <IntelligenceBlocks record={r} />
          </TabsContent>
        </Tabs>
      </CardContent>
    </Card>
  );
}

function ScheduledRow({ record }: { record: CallRecord | null }) {
  // The record carries no explicit scheduled/started/ended wall-clock stamps,
  // so we label honestly: Scheduled and Started are unknown (em-dash); Ended is
  // derivable only once the call completed (total_e2e_ms present) but still has
  // no absolute timestamp, so it too is em-dashed rather than invented.
  void record;
  return (
    <div className="mt-2 flex flex-wrap gap-x-6 gap-y-1 text-xs text-muted-foreground">
      <span>Scheduled: {DASH}</span>
      <span>Started: {DASH}</span>
      <span>Ended: {DASH}</span>
    </div>
  );
}

function SummaryField({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col">
      <dt className="text-[0.68rem] uppercase tracking-wide text-muted-foreground">
        {label}
      </dt>
      <dd className="font-medium">{value}</dd>
    </div>
  );
}

function IntelStatusBadge({ status }: { status?: string | null }) {
  if (status === "pending") {
    return (
      <Badge variant="outline" className="text-primary">
        extracting&hellip;
      </Badge>
    );
  }
  if (status === "failed") {
    return (
      <Badge variant="outline" className="text-muted-foreground">
        extraction unavailable
      </Badge>
    );
  }
  if (status === "ready") {
    return (
      <Badge variant="outline" className="text-emerald-300">
        Completed
      </Badge>
    );
  }
  return null;
}

function MetricTiles({ record }: { record: CallRecord | null }) {
  const r = record;
  // ASK 6: the transport figure is an ESTIMATE (per-frame mean). Prefer the
  // accurate-named field and fall back to the deprecated alias.
  const transportEstimate =
    r?.transport_delay_estimate_ms ?? r?.twilio_latency_ms ?? null;
  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
      <MetricTile label="Total end-to-end" value={fmtMs(r?.total_e2e_ms)} help={METRIC_HELP.total} />
      <MetricTile label="Nova Sonic total" value={fmtMs(r?.nova_total_ms)} help={METRIC_HELP.nova} />
      <MetricTile label="Non-Sonic time" value={fmtMs(r?.non_nova_ms)} help={METRIC_HELP.nonnova} />
      <MetricTile
        label="Transport delay (est.)"
        value={fmtMs(transportEstimate)}
        help={METRIC_HELP.twilio}
      />
      <MetricTile label="TTFT" value={fmtMs(r?.ttft_ms)} help={METRIC_HELP.ttft} />
      <MetricTile
        label="Turn count"
        value={r?.turn_count != null ? String(r.turn_count) : DASH}
        help={METRIC_HELP.turns}
      />
      <MetricTile
        label="Barge-ins"
        value={r?.barge_in_count != null ? String(r.barge_in_count) : DASH}
        help={METRIC_HELP.barge}
      />
    </div>
  );
}

// ASK 6: honest Twilio insights panel. Shows the authentic Twilio Call-resource
// facts when enrichment has run (source === 'twilio_call_resource'), an explicit
// "pending" note before enrichment, or an "unavailable" note (with reason) when
// creds/sid were missing or the fetch failed. Advanced QoS (RTT/jitter/packet
// loss/MOS) is a paid add-on: we state the limitation rather than fabricate.
function TwilioInsightsPanel({ record }: { record: CallRecord | null }) {
  const ins = record?.twilio_insights;
  if (!ins) return null;
  const available = Boolean(ins.available);
  const source = ins.source || "pending";
  const fields = (ins.fields || {}) as Record<string, unknown>;

  const fieldRow = (label: string, key: string) => {
    const v = fields[key];
    if (v === null || v === undefined || v === "") return null;
    return <SummaryField key={key} label={label} value={String(v)} />;
  };

  return (
    <div>
      <h3 className="mb-1 flex items-center gap-2 text-sm font-semibold">
        Twilio insights
        <Badge
          variant="outline"
          className={
            available
              ? "border-emerald-500/40 text-emerald-300"
              : source === "pending"
                ? "border-amber-500/40 text-amber-300"
                : "border-slate-500/40 text-slate-300"
          }
        >
          {available
            ? "authentic Call resource"
            : source === "pending"
              ? "pending enrichment"
              : "unavailable"}
        </Badge>
      </h3>
      {!available && source === "pending" && (
        <p className="mb-2 text-xs text-muted-foreground">
          Real Twilio Call-resource data is fetched off the audio path after the
          call ends; it has not been enriched yet for this call.
        </p>
      )}
      {!available && source !== "pending" && (
        <p className="mb-2 text-xs text-muted-foreground">
          Authentic Twilio data is unavailable for this call
          {ins.error ? ` (${ins.error})` : ""}. No metrics are shown rather than
          fabricated.
        </p>
      )}
      {available && (
        <dl className="grid grid-cols-2 gap-x-6 gap-y-2 text-sm sm:grid-cols-3">
          {fieldRow("Status", "status")}
          {fieldRow("Direction", "direction")}
          {fieldRow("Duration (s)", "duration")}
          {fieldRow("From", "from_formatted") || fieldRow("From", "from")}
          {fieldRow("To", "to_formatted") || fieldRow("To", "to")}
          {fieldRow("Start", "start_time")}
          {fieldRow("End", "end_time")}
          {fieldRow("Price", "price")}
          {fieldRow("Answered by", "answered_by")}
          {fieldRow("Queue time", "queue_time")}
        </dl>
      )}
      <p className="mt-2 text-xs text-muted-foreground">
        {ins.advanced_qos_available
          ? "Advanced Voice Insights QoS metrics are available."
          : `Advanced QoS (RTT / jitter / packet loss / MOS) is not available${
              ins.advanced_qos_reason ? `: ${ins.advanced_qos_reason}` : ""
            }. These are never estimated or fabricated.`}
      </p>
    </div>
  );
}

// ASK 5: render the sync/async tool calls for a turn. Sync in-call tools show
// a timing; async (post-call) tools are visually marked as off-path (they add
// no call latency).
function ToolCalls({ calls }: { calls: ToolCall[] }) {
  if (!calls.length) return <>{DASH}</>;
  return (
    <div className="flex flex-wrap gap-1">
      {calls.map((c, i) => {
        const async = c.kind === "async" || c.off_path === true;
        const resolvedVia =
          async && c.resolved_via ? String(c.resolved_via) : null;
        return (
          <Badge
            key={i}
            variant="outline"
            className={
              "font-mono text-[0.65rem] " +
              (async
                ? "border-amber-500/40 text-amber-300"
                : "border-sky-500/40 text-sky-300")
            }
            title={
              async
                ? "async / off-path tool (post-call, adds no call latency)" +
                  (resolvedVia ? ` \u2014 resolved via ${resolvedVia}` : "")
                : c.t_ms != null
                  ? `sync in-call tool at ${fmtMs(c.t_ms)}`
                  : "sync in-call tool"
            }
          >
            {c.tool || "tool"} &middot; {async ? "async / off-path" : "sync"}
            {!async && c.t_ms != null ? ` \u00b7 ${fmtMs(c.t_ms)}` : ""}
            {resolvedVia ? ` \u00b7 via ${resolvedVia}` : ""}
          </Badge>
        );
      })}
    </div>
  );
}

// ASK 5: call-level async (post-call, off-path) tool calls. These are NOT part
// of any live turn and add no call latency, so they are listed separately
// beneath the turn table with an explicit off-path note.
function AsyncToolCalls({ record }: { record: CallRecord | null }) {
  const calls = record?.async_tool_calls || [];
  if (!calls.length) return null;
  return (
    <div className="mt-3">
      <p className="mb-1 text-xs font-medium text-muted-foreground">
        Async tool calls (post-call, off-path &mdash; add no call latency)
      </p>
      <ToolCalls calls={calls} />
    </div>
  );
}

// Per-turn metrics table from conversation_turns:
// # | Role | TTFT | Response wait | Turn elapsed | Tool. ASK 1 uses accurate
// labels (no "latency"); ASK 5 fills the Tool column with sync/async tool
// calls; ASK 7 marks interrupted assistant turns.
function TurnLatencyTable({ record }: { record: CallRecord | null }) {
  const turns = (record?.conversation_turns || [])
    .slice()
    .sort(
      (a, b) =>
        (a.start_ms || 0) - (b.start_ms || 0) || (a.index || 0) - (b.index || 0)
    );
  if (!turns.length) {
    return (
      <p className="py-4 text-center text-sm italic text-muted-foreground">
        No turns recorded.
      </p>
    );
  }
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead className="w-10">#</TableHead>
          <TableHead>Role</TableHead>
          <TableHead>TTFT</TableHead>
          <TableHead>Caller&rarr;reply gap</TableHead>
          <TableHead>Turn elapsed</TableHead>
          <TableHead>Tool</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {turns.map((t, i) => {
          const isAgent = t.speaker === "assistant";
          const ttft =
            isAgent && t.ttft_ms != null
              ? fmtMs(t.ttft_ms)
              : isAgent && t.ttft_source === "unavailable"
                ? "n/a (single response window)"
                : DASH;
          // FEAT-004: prefer the accurate caller->reply gap; the value is only
          // present when a genuine caller turn precedes an assistant turn
          // (null for assistant-after-assistant turns -> DASH).
          const callerReplyGap =
            t.caller_reply_gap_ms ?? t.response_wait_ms ?? t.latency_ms ?? null;
          const turnElapsed = t.turn_elapsed_ms ?? t.total_ms ?? null;
          return (
            <TableRow key={i}>
              <TableCell>{i + 1}</TableCell>
              <TableCell>
                <div className="flex flex-wrap items-center gap-1">
                  <Badge
                    variant="outline"
                    className={
                      isAgent
                        ? "border-emerald-500/40 text-emerald-300"
                        : "border-primary/40 text-primary"
                    }
                  >
                    {isAgent ? "AGENT" : "CALLER"}
                  </Badge>
                  {t.interrupted && (
                    <Badge
                      variant="outline"
                      className="border-amber-500/50 text-amber-300"
                      title="A barge-in interrupted this turn; the full intended reply may not have been delivered."
                    >
                      interrupted
                    </Badge>
                  )}
                </div>
              </TableCell>
              <TableCell>{ttft}</TableCell>
              <TableCell>
                {isAgent && callerReplyGap != null ? fmtMs(callerReplyGap) : DASH}
              </TableCell>
              <TableCell>{isAgent ? fmtMs(turnElapsed) : DASH}</TableCell>
              <TableCell>
                <ToolCalls calls={t.tool_calls || []} />
              </TableCell>
            </TableRow>
          );
        })}
      </TableBody>
    </Table>
  );
}

function TranscriptPanel({
  segments,
}: {
  segments: ReturnType<typeof chatSegments>;
}) {
  if (!segments.length) {
    return (
      <p className="py-6 text-sm italic text-muted-foreground">
        No conversation recorded.
      </p>
    );
  }
  return (
    <ScrollArea className="h-[420px] rounded-md border p-4">
      <div className="space-y-3 text-sm leading-relaxed">
        {segments.map((s, i) => (
          <p key={i}>
            <span
              className={
                "mr-2 font-semibold " +
                (s.speaker === "assistant" ? "text-emerald-300" : "text-primary")
              }
            >
              {s.speaker === "assistant" ? "Nova Sonic:" : "Caller:"}
            </span>
            <span className="whitespace-pre-wrap">{s.text}</span>
          </p>
        ))}
      </div>
    </ScrollArea>
  );
}

// ASK 7: an inline barge-in marker interleaved into the conversation timeline
// at the point a turn was interrupted. We surface the interruption honestly
// (GA exposes no delivered-vs-intended transcript) rather than implying the
// full intended utterance was delivered.
function BargeInMarker() {
  return (
    <div className="flex justify-center">
      <span className="inline-flex items-center gap-1 rounded-full border border-amber-500/50 bg-amber-500/10 px-2.5 py-0.5 text-[0.65rem] font-medium uppercase tracking-wide text-amber-300">
        <Hand className="h-3 w-3" /> caller barged in
      </span>
    </div>
  );
}

function ChatView({ record }: { record: CallRecord | null }) {
  const segments = record ? chatSegments(record) : [];
  if (!segments.length) {
    return (
      <p className="py-4 text-sm italic text-muted-foreground">
        No conversation recorded.
      </p>
    );
  }
  return (
    <div className="flex flex-col gap-2.5">
      {segments.map((seg, i) => {
        // FEAT-004: truthful meta labels. The large numbers are a gap/elapsed
        // window, NOT "latency", so they no longer contradict a small TTFT.
        const parts: string[] = [];
        if (seg.turnElapsedMs != null)
          parts.push(`turn elapsed ${fmtMs(seg.turnElapsedMs)}`);
        if (seg.speaker === "assistant") {
          if (seg.callerReplyGapMs != null)
            parts.push(`caller\u2192reply gap ${fmtMs(seg.callerReplyGapMs)}`);
          if (seg.ttftMs != null) {
            parts.push(`TTFT ${fmtMs(seg.ttftMs)}`);
          } else if (seg.ttftSource === "unavailable") {
            parts.push("TTFT n/a (single response window)");
          }
        }
        const isAgent = seg.speaker === "assistant";
        return (
          <Fragment key={i}>
            <div className={"flex " + (isAgent ? "justify-end" : "justify-start")}>
              <div
                className={
                  "max-w-[72%] rounded-xl border px-3 py-2 " +
                  (seg.interrupted
                    ? "border-amber-500/50 bg-amber-500/10"
                    : isAgent
                      ? "border-emerald-500/40 bg-emerald-500/10"
                      : "border-primary/40 bg-primary/10")
                }
              >
                <div className="flex items-center gap-1.5 text-[0.65rem] uppercase tracking-wide text-muted-foreground">
                  {isAgent ? "Nova Sonic" : "Caller"}
                  {seg.interrupted && (
                    <Badge
                      variant="outline"
                      className="border-amber-500/50 px-1 py-0 text-[0.6rem] text-amber-300"
                      title="A barge-in interrupted this reply; the text shown is what Nova intended, not necessarily what was delivered before the caller cut in."
                    >
                      interrupted
                    </Badge>
                  )}
                </div>
                <div className="whitespace-pre-wrap break-words text-sm">
                  {seg.text}
                </div>
                {parts.length > 0 && (
                  <div className="mt-1 text-[0.7rem] text-muted-foreground">
                    {parts.join(" \u00b7 ")}
                  </div>
                )}
              </div>
            </div>
            {seg.interrupted && <BargeInMarker />}
          </Fragment>
        );
      })}
    </div>
  );
}

// Per-UTTERANCE turn table from record.turns: Turn # | Speaker | Duration |
// Transcript (ported from web/app.js renderTurns).
function PerUtteranceTable({ record }: { record: CallRecord | null }) {
  const turns = record?.turns || [];
  if (!turns.length) {
    return (
      <p className="py-4 text-center text-sm italic text-muted-foreground">
        No turns recorded.
      </p>
    );
  }
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead className="w-16">Turn #</TableHead>
          <TableHead>Speaker</TableHead>
          <TableHead>Duration</TableHead>
          <TableHead>Transcript</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {turns.map((turn, i) => (
          <TableRow key={i}>
            <TableCell>{turn.index != null ? turn.index + 1 : i + 1}</TableCell>
            <TableCell>
              {turn.speaker === "assistant" ? "Nova Sonic" : "Caller"}
            </TableCell>
            <TableCell>{fmtMs(turn.duration_ms)}</TableCell>
            <TableCell className="font-mono text-xs">{turn.text || ""}</TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

// Call Disposition + Structured Data & Next-Best-Action blocks.
function IntelligenceBlocks({ record }: { record: CallRecord | null }) {
  const r = record;
  const sd = r?.structured_data || {};
  const pay = sd.payment_specifics || {};
  const ctx = sd.contextual_data || {};

  const triggerParts: string[] = [];
  if (r?.ptp_sms_status) {
    const smsLabel =
      {
        sent: "payment-link SMS sent",
        blocked_trial: "payment-link SMS blocked (trial account)",
        failed: "payment-link SMS failed",
      }[r.ptp_sms_status] || `payment-link SMS ${r.ptp_sms_status}`;
    triggerParts.push(
      r.ptp_sms_to ? `${smsLabel} (to ${r.ptp_sms_to})` : smsLabel
    );
  }
  if (r?.collection_paused) triggerParts.push("collection paused");
  if (r?.escalated) triggerParts.push("escalated to specialist");

  return (
    <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
      <Card className="bg-background/60">
        <CardHeader>
          <CardTitle className="flex items-center gap-1 text-sm">
            Call Disposition
            <HelpTip text="Standardized outcome tag that drives CRM updates and downstream automation (payment-link SMS, pause/escalate)." />
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <DispositionBadge disposition={r?.disposition} />
          <p className="text-xs text-muted-foreground">
            Downstream triggers:{" "}
            {triggerParts.length ? triggerParts.join(" \u00b7 ") : DASH}
          </p>
        </CardContent>
      </Card>

      <Card className="bg-background/60">
        <CardHeader>
          <CardTitle className="flex items-center gap-1 text-sm">
            Structured Data &amp; Next-Best-Action
            <HelpTip text="Extracted entities, payment details, behavioral insights, and the exact next step to move the debt toward resolution." />
            <IntelStatusBadge status={r?.intelligence_status} />
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-3 text-sm">
          <FieldGroup title="Payment specifics">
            <Field label="Promised amount">
              {pay.promised_amount != null
                ? fmtMoney(pay.promised_amount, r?.currency)
                : DASH}
            </Field>
            <Field label="Agreed date">{fmtText(pay.agreed_date)}</Field>
            <Field label="Preferred payment method">
              {fmtText(pay.preferred_payment_method)}
            </Field>
          </FieldGroup>
          <FieldGroup title="Contextual data">
            <Field label="Reason for delinquency">
              {fmtText(ctx.reason_for_delinquency)}
            </Field>
            <Field label="Customer sentiment">
              {fmtText(ctx.customer_sentiment)}
            </Field>
          </FieldGroup>
          <FieldGroup title="Next-best-action">
            <Field label="Automated directive" wide>
              {fmtText(r?.next_best_action)}
            </Field>
          </FieldGroup>
        </CardContent>
      </Card>
    </div>
  );
}

function FieldGroup({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div>
      <div className="mb-1 text-[0.68rem] uppercase tracking-wide text-muted-foreground">
        {title}
      </div>
      <dl className="space-y-1">{children}</dl>
    </div>
  );
}

function Field({
  label,
  children,
  wide,
}: {
  label: string;
  children: React.ReactNode;
  wide?: boolean;
}) {
  return (
    <div
      className={
        wide
          ? "flex flex-col gap-0.5"
          : "flex items-baseline justify-between gap-3 border-b border-dashed border-border/60 pb-1"
      }
    >
      <dt className="text-muted-foreground">{label}</dt>
      <dd className={wide ? "" : "text-right"}>{children}</dd>
    </div>
  );
}

function HelpTip({ text }: { text: string }) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          type="button"
          className="text-muted-foreground/70 hover:text-foreground"
          aria-label="More info"
        >
          <Info className="h-3.5 w-3.5" />
        </button>
      </TooltipTrigger>
      <TooltipContent className="max-w-xs text-xs leading-relaxed">
        {text}
      </TooltipContent>
    </Tooltip>
  );
}

// Collapsible raw event log (was the <details> "In-depth event log").
function RawEventLog({ record }: { record: CallRecord | null }) {
  return (
    <Collapsible className="rounded-md border">
      <CollapsibleTrigger className="group flex w-full items-center justify-between px-4 py-2.5 text-sm text-muted-foreground">
        In-depth event log
        <ChevronDown className="h-4 w-4 transition-transform group-data-[state=open]:rotate-180" />
      </CollapsibleTrigger>
      <CollapsibleContent>
        <ScrollArea className="h-[320px] border-t">
          <pre className="whitespace-pre-wrap break-words p-4 text-xs">
            {JSON.stringify(record?.events_log || [], null, 2)}
          </pre>
        </ScrollArea>
      </CollapsibleContent>
    </Collapsible>
  );
}

// --- footer -----------------------------------------------------------------

function Footer({ config }: { config: ReturnType<typeof useConfig> }) {
  if (!config) return null;
  const summary = [
    `model: ${config.nova_model_id}`,
    `region: ${config.aws_region}`,
    `twilio: ${config.twilio_configured ? "configured" : "NOT configured"}`,
    `public_base_url: ${config.public_base_url_set ? "set" : "missing"}`,
  ].join(" \u00b7 ");

  return (
    <footer className="mt-8 border-t border-border pt-4 text-sm text-muted-foreground">
      <Popover>
        <PopoverTrigger asChild>
          <button className="inline-flex items-center gap-1 hover:text-foreground">
            <Settings2 className="h-3.5 w-3.5" />
            {summary}
          </button>
        </PopoverTrigger>
        <PopoverContent className="w-96 text-xs">
          <h4 className="mb-2 font-semibold text-foreground">Configuration</h4>
          <dl className="space-y-1">
            <ConfigRow label="Nova model" value={config.nova_model_id} />
            <ConfigRow label="Extraction model" value={config.extraction_model_id} />
            <ConfigRow
              label="Code interpreter"
              value={
                config.code_interpreter
                  ? `${config.code_interpreter} (${config.code_interpreter_id ?? ""})`
                  : undefined
              }
            />
            <ConfigRow label="AWS region" value={config.aws_region} />
            <ConfigRow label="App log group" value={config.app_log_group} />
            <ConfigRow
              label="Twilio"
              value={config.twilio_configured ? "configured" : "NOT configured"}
            />
            <ConfigRow
              label="Twilio trial mode"
              value={config.twilio_trial_mode ? "on" : "off"}
            />
            <ConfigRow
              label="Public base URL"
              value={config.public_base_url_set ? "set" : "missing"}
            />
          </dl>
        </PopoverContent>
      </Popover>
    </footer>
  );
}

function ConfigRow({ label, value }: { label: string; value?: unknown }) {
  return (
    <div className="flex justify-between gap-3">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="truncate font-mono text-foreground">{fmtText(value)}</dd>
    </div>
  );
}
