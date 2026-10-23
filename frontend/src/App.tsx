import { Fragment, useCallback, useEffect, useMemo, useState } from "react";
import {
  ChevronDown,
  Hand,
  Settings2,
} from "lucide-react";

import {
  apiPost,
  DASH,
  dispositionLabel,
  fmtMoney,
  fmtMs,
  fmtText,
  type CallRecord,
  type ToolCall,
} from "@/lib/api";
import {
  chatSegments,
  connectionRestarts,
  intelligenceFieldText,
  toolExecutionDetails,
  turnMetrics,
  utteranceSegments,
} from "@/lib/selectors";
import { METRIC_HELP } from "@/lib/metric-help";
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
import { TooltipProvider } from "@/components/ui/tooltip";

import { DueRecordsTable } from "@/components/DueRecordsTable";
import { DispositionBadge } from "@/components/DispositionBadge";
import { MetricTile } from "@/components/MetricTile";
import { ColumnHeading, InfoHelp } from "@/components/InfoHelp";
import { ResponseTimeline, TimingGuide } from "@/components/ResponseTimeline";
import { ThemeToggle } from "@/components/ThemeToggle";
import { SonicCost } from "@/components/SonicCost";

function Toast({
  toast,
}: {
  toast: { message: string; error: boolean } | null;
}) {
  if (!toast) return null;
  return (
    <div
      role={toast.error ? "alert" : "status"}
      className={
        "mt-3 rounded-lg border px-4 py-2.5 text-sm " +
        (toast.error
          ? "border-destructive bg-destructive/15 text-red-700 dark:text-red-300"
          : "border-emerald-500/50 bg-emerald-50 dark:bg-emerald-500/10 text-emerald-700 dark:text-emerald-300")
      }
    >
      {toast.message}
    </div>
  );
}

export default function App() {
  const config = useConfig();
  const { dues, loading: duesLoading, error: duesError, reload: reloadDues } = useDues();
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

  const { record, isLive, loading: detailLoading, error: detailError, reload: reloadDetail } = useCallDetail(
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

  useEffect(() => {
    if (!selectedCallId) return;
    const detail = document.getElementById("call-detail");
    detail?.focus({ preventScroll: true });
    detail?.scrollIntoView({ behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
  }, [selectedCallId]);

  // Debtor name for the header: prefer the matching due record.
  const debtorName = useMemo(() => {
    if (!record?.account_id) return undefined;
    const due = dues.find((d) => d.account_id === record.account_id);
    return due?.debtor_name;
  }, [record?.account_id, dues]);

  return (
    <TooltipProvider delayDuration={150}>
      <div className="mx-auto max-w-[1600px] px-4 pb-16 pt-6 sm:px-6 lg:px-8">
        <Header
          config={config}
          twilioTrial={config?.twilio_trial_mode}
        />
        <Toast toast={toast} />

        <DueRecordsTable
          dues={dues}
          calls={calls}
          duesLoading={duesLoading}
          callsLoading={callsLoading}
          duesError={duesError}
          callsError={callsError}
          selectedCallId={selectedCallId}
          calling={calling}
          config={config}
          onCall={placeCall}
          onSelectCall={selectCall}
          onRefresh={async () => { await Promise.all([reloadDues(), reloadCalls()]); }}
        />

        {selectedCallId && (
          <CallDetail
            callId={selectedCallId}
            record={record}
            isLive={isLive}
            loading={detailLoading}
            error={detailError}
            onRetry={reloadDetail}
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
  const languageNames: Record<string, string> = { en: "English", fr: "French", es: "Spanish" };
  const languages = [...new Set(config?.conversation_languages ?? [])].flatMap((code) => languageNames[code] ? [languageNames[code]] : []);
  return (
    <header className="flex flex-col gap-3 border-b border-border-strong pb-4 sm:flex-row sm:items-start sm:justify-between">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">
          AR Collections &middot; Nova Sonic 2.5
        </h1>
        <p className="mt-1 max-w-3xl text-sm text-muted-foreground">
          Outbound account collections with Twilio and Amazon Bedrock.
        </p>
        {languages.length > 0 && <p className="mt-1 flex flex-wrap items-center text-xs text-muted-foreground">Conversation languages: {languages.join(" · ")}<InfoHelp label="Conversation languages">Calls start in English. The caller can switch to French or Spanish while keeping the same voice and conversation. This changes the spoken conversation; the dashboard stays in English.</InfoHelp></p>}
      </div>
      <div className="flex shrink-0 flex-wrap items-center gap-2">
        {config?.nova_model_id && (
          <Badge variant="secondary" className="font-mono text-[0.7rem]">
            {config.nova_model_id}
          </Badge>
        )}
        {twilioTrial && (
          <Badge variant="outline" className="text-[0.7rem]">
            Twilio
          </Badge>
        )}
        <ThemeToggle />
      </div>
    </header>
  );
}

// --- call detail ------------------------------------------------------------

function CallDetail({
  callId,
  record,
  isLive,
  loading,
  error,
  onRetry,
  debtorName,
  config,
}: {
  callId: string;
  record: CallRecord | null;
  isLive: boolean;
  loading: boolean;
  error: string | null;
  onRetry: () => void;
  debtorName?: string;
  config: ReturnType<typeof useConfig>;
}) {
  const r = record;
  const status = r?.total_e2e_ms != null ? "COMPLETED" : "IN PROGRESS";
  const restarts = r ? connectionRestarts(r) : 0;
  const segments = useMemo(() => (r ? chatSegments(r) : []), [r]);

  return (
    <Card id="call-detail" role="region" aria-labelledby="call-detail-title" tabIndex={-1} className="mt-5 scroll-mt-5 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
      <CardHeader>
        <div className="flex flex-wrap items-center gap-2">
          <CardTitle id="call-detail-title" className="text-lg">Call attempt</CardTitle>
          <span className="break-all font-mono text-xs text-muted-foreground">{callId}</span>
          {isLive && (
            <Badge className="motion-safe:animate-pulse bg-destructive text-destructive-foreground">
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

      <CardContent className="p-3 sm:p-4">
        {error && <div role="alert" className="mb-4 flex flex-wrap items-center justify-between gap-3 rounded-md border border-amber-500/40 bg-amber-50 dark:bg-amber-500/10 px-3 py-2 text-sm text-amber-800 dark:text-amber-300"><p>Call details could not be refreshed. {error}{isLive ? " Retrying while the call is active." : ""}</p><Button variant="outline" size="sm" disabled={loading} onClick={onRetry}>Retry</Button></div>}
        {!r ? <div role="status" className="space-y-3 py-4 text-sm text-muted-foreground">{loading ? "Loading call details…" : "No call details available."}{loading && <><Skeleton className="h-10 w-full" /><Skeleton className="h-32 w-full" /></>}</div> : <Tabs defaultValue="telemetry" className="w-full">
          <TabsList className="h-auto max-w-full flex-wrap justify-start gap-1">
            <TabsTrigger value="telemetry" className="min-h-11 sm:min-h-8">Call telemetry</TabsTrigger>
            <TabsTrigger value="transcript" className="min-h-11 sm:min-h-8">Transcript</TabsTrigger>
            <TabsTrigger value="conversation" className="min-h-11 sm:min-h-8">Conversation</TabsTrigger>
            <TabsTrigger value="intelligence" className="min-h-11 sm:min-h-8">Intelligence</TabsTrigger>
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

            <SonicCost record={r} />

            <TwilioInsightsPanel record={r} />

            <Separator />

            <div>
              <h3 className="mb-2 text-sm font-semibold">Turn-by-turn metrics</h3>
              <TurnLatencyTable record={r} />
              <AsyncToolCalls record={r} />
            </div>

            <RawEventLog record={r} />
          </TabsContent>

          {/* --- Transcript ----------------------------------------------- */}
          <TabsContent value="transcript">
            <TranscriptPanel segments={segments} />
          </TabsContent>

          {/* --- Conversation (chat + per-utterance table) ---------------- */}
          <TabsContent value="conversation" className="space-y-2">
            <TimingGuide />
            <ChatView record={r} />
            <Separator />
            <div>
              <h3 className="mb-1 text-sm font-semibold">Per-turn timeline</h3>
              <p className="mb-2 text-xs text-muted-foreground">
                One row per finished utterance, in transcript order. TTFA is
                measured from the SDK's observed response start to first
                audio. Until next transcript is the time until the next finished
                transcript arrives, not speaking time.
              </p>
              <PerUtteranceTable record={r} />
            </div>
          </TabsContent>

          {/* --- Intelligence --------------------------------------------- */}
          <TabsContent value="intelligence">
            <IntelligenceBlocks record={r} />
          </TabsContent>
        </Tabs>}
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
  if (status === "partial") {
    return <Badge variant="outline" className="border-amber-500/40 text-amber-800 dark:text-amber-300">needs review</Badge>;
  }
  if (status === "withheld_unconfirmed_termination") {
    return <Badge variant="outline" className="text-amber-800 dark:text-amber-300">awaiting call completion</Badge>;
  }
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
      <Badge variant="outline" className="text-emerald-700 dark:text-emerald-300">
        Completed
      </Badge>
    );
  }
  return null;
}

function MetricTiles({ record }: { record: CallRecord | null }) {
  const r = record;
  // The transport figure is an ESTIMATE (per-frame mean). Prefer the
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
      <MetricTile label="TTFA · first audio" value={fmtMs(r?.ttfa_ms ?? r?.ttft_ms)} help={METRIC_HELP.ttfa} />
      <MetricTile label="Text TTFT" value={fmtMs(r?.text_ttft_ms)} help={METRIC_HELP.textTtft} />
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

// Honest Twilio insights panel. Shows the authentic Twilio Call-resource
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
              ? "border-emerald-500/40 text-emerald-700 dark:text-emerald-300"
              : source === "pending"
                ? "border-amber-500/40 text-amber-800 dark:text-amber-300"
                : "border-slate-500/40 text-slate-600 dark:text-slate-300"
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

// Execution labels come from instrumentation, never from the call phase alone.
function ToolCalls({ calls, phaseHint = "in_call", showAssociation = false }: { calls: ToolCall[]; phaseHint?: "in_call" | "post_call"; showAssociation?: boolean }) {
  if (!calls.length) return <>{DASH}</>;
  return (
    <div className="flex flex-wrap gap-1.5">
      {calls.map((call, index) => {
        const execution = toolExecutionDetails(call, phaseHint);
        const parts = [call.tool || "tool", execution.label];
        if (call.status) parts.push(call.status);
        if (call.completion_semantics === "teardown_scheduled") parts.push("teardown scheduled");
        if (call.duration_ms != null) parts.push(fmtMs(call.duration_ms));
        else if (call.t_ms != null) parts.push(`at ${fmtMs(call.t_ms)}`);
        if (call.resolved_via) parts.push(`via ${call.resolved_via}`);
        if (showAssociation && call.association?.endsWith("_estimate")) parts.push("estimated turn match");
        return (
          <Badge key={call.tool_use_id || index} variant="outline" className={"max-w-full whitespace-normal break-words font-mono text-[0.65rem] " + (call.status === "error" || call.status === "cancelled" ? "border-amber-500/40 text-amber-800 dark:text-amber-300" : execution.verifiedAsync ? "border-emerald-500/40 text-emerald-700 dark:text-emerald-300" : "border-slate-500/40 text-slate-600 dark:text-slate-300")} title={execution.title}>
            {parts.join(" · ")}
          </Badge>
        );
      })}
    </div>
  );
}

function AsyncToolCalls({ record }: { record: CallRecord | null }) {
  const inCall = record ? turnMetrics(record).unassignedTools : [];
  const postCall = record?.async_tool_calls || [];
  if (!inCall.length && !postCall.length) return null;
  return (
    <div className="mt-4 space-y-3">
      {inCall.length > 0 && <div><h4 className="mb-1.5 text-xs font-medium text-muted-foreground">In-call tools without a turn match</h4><p className="mb-1.5 text-xs text-muted-foreground">These jobs were recorded, but their timestamps could not be matched to an assistant utterance.</p><ToolCalls calls={inCall} /></div>}
      {postCall.length > 0 && <div><h4 className="mb-1.5 text-xs font-medium text-muted-foreground">Post-call tools</h4><ToolCalls calls={postCall} phaseHint="post_call" /></div>}
    </div>
  );
}

function TurnLatencyTable({ record }: { record: CallRecord | null }) {
  const turns = record ? turnMetrics(record).turns : [];
  if (!turns.length) {
    return (
      <p className="py-4 text-center text-sm italic text-muted-foreground">
        No turns recorded.
      </p>
    );
  }
  return (
    <Table aria-label="Turn-by-turn metrics" className="min-w-[960px]">
      <TableHeader>
        <TableRow>
          <TableHead scope="col" className="w-16"><ColumnHeading label="Turn" help="The order in which this utterance was recorded. Each caller or agent utterance gets its own row, including consecutive utterances from one speaker." /></TableHead>
          <TableHead scope="col"><ColumnHeading label="Speaker" help="Who said these words: the caller or the Nova voice agent." /></TableHead>
          <TableHead scope="col"><ColumnHeading label="TTFA" help={METRIC_HELP.ttfa} /></TableHead>
          <TableHead scope="col" className="w-[35%]"><ColumnHeading label="Text" help={METRIC_HELP.utterance} /></TableHead>
          <TableHead scope="col"><ColumnHeading label="Until next transcript" help={METRIC_HELP.transcriptInterval} /></TableHead>
          <TableHead scope="col" className="w-[22%]"><ColumnHeading label="Tools" help={METRIC_HELP.tools} /></TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {turns.map((t, i) => {
          const isAgent = t.speaker === "assistant";
          return (
            <TableRow key={i}>
              <TableCell>{i + 1}</TableCell>
              <TableCell>
                <div className="flex flex-wrap items-center gap-1">
                  <Badge
                    variant="outline"
                    className={
                      isAgent
                        ? "border-emerald-500/40 text-emerald-700 dark:text-emerald-300"
                        : "border-primary/40 text-primary"
                    }
                  >
                    {isAgent ? "AGENT" : "CALLER"}
                  </Badge>
                  {t.interrupted && (
                    <Badge
                      variant="outline"
                      className="border-amber-500/50 text-amber-800 dark:text-amber-300"
                      title="A barge-in interrupted this turn; the full intended reply may not have been delivered."
                    >
                      interrupted
                    </Badge>
                  )}
                </div>
              </TableCell>
              <TableCell className="whitespace-nowrap tabular-nums">{fmtMs(t.ttfaMs)}</TableCell>
              <TableCell className="min-w-[240px] max-w-[560px] whitespace-pre-wrap break-words [overflow-wrap:anywhere]">{t.text || DASH}</TableCell>
              <TableCell className="whitespace-nowrap tabular-nums">{fmtMs(t.transcriptIntervalMs)}</TableCell>
              <TableCell className="min-w-[200px] max-w-[300px] whitespace-normal break-words [overflow-wrap:anywhere]"><ToolCalls calls={t.toolCalls} showAssociation /></TableCell>
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
                (s.speaker === "assistant" ? "text-emerald-700 dark:text-emerald-300" : "text-primary")
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

// An inline barge-in marker interleaved into the conversation timeline
// at the point a turn was interrupted. We surface the interruption honestly
// (GA exposes no delivered-vs-intended transcript) rather than implying the
// full intended utterance was delivered.
function BargeInMarker() {
  return (
    <div className="flex justify-center">
      <span className="inline-flex items-center gap-1 rounded-full border border-amber-500/50 bg-amber-50 dark:bg-amber-500/10 px-2.5 py-0.5 text-[0.65rem] font-medium uppercase tracking-wide text-amber-800 dark:text-amber-300">
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
    <div className="flex flex-col gap-2.5 rounded-lg border bg-background/50 p-1.5 sm:p-2.5">
      {segments.map((seg, i) => {
        const parts: string[] = [];
        if (seg.transcriptIntervalMs != null)
          parts.push(`Until next transcript ${fmtMs(seg.transcriptIntervalMs)}`);
        if (seg.speaker === "assistant") {
          parts.push(`Reply gap (observed) ${fmtMs(seg.callerReplyGapMs)}`);
        }
        const isAgent = seg.speaker === "assistant";
        return (
          <Fragment key={i}>
            <div className={"flex " + (isAgent ? "justify-end" : "justify-start")}>
              <div
                className={
                  "min-w-0 max-w-full rounded-xl border px-3 py-2 sm:max-w-[85%] lg:max-w-[76%] " +
                  (seg.interrupted
                    ? "border-amber-400 bg-amber-50 dark:bg-amber-500/10"
                    : isAgent
                      ? "border-emerald-600/50 bg-emerald-50 dark:bg-emerald-500/10"
                      : "border-primary/50 bg-blue-50 dark:bg-primary/10")
                }
              >
                <div className="flex items-center gap-1.5 text-[0.65rem] uppercase tracking-wide text-muted-foreground">
                  {isAgent ? "Nova Sonic" : "Caller"}
                  {seg.interrupted && (
                    <Badge
                      variant="outline"
                      className="border-amber-500/50 px-1 py-0 text-[0.6rem] text-amber-800 dark:text-amber-300"
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
                {isAgent && <ResponseTimeline segment={seg} />}
              </div>
            </div>
            {seg.interrupted && <BargeInMarker />}
          </Fragment>
        );
      })}
    </div>
  );
}

function PerUtteranceTable({ record }: { record: CallRecord | null }) {
  const turns = record ? utteranceSegments(record) : [];
  if (!turns.length) {
    return (
      <p className="py-4 text-center text-sm italic text-muted-foreground">
        No turns recorded.
      </p>
    );
  }
  return (
    <Table aria-label="Per-turn timeline" className="min-w-[640px]">
      <TableHeader>
        <TableRow>
          <TableHead scope="col" className="w-16"><ColumnHeading label="Turn" help="The order in which this finished utterance was recorded. Each caller or agent utterance gets its own row." /></TableHead>
          <TableHead scope="col"><ColumnHeading label="Speaker" help="Who said these words: the caller or the Nova voice agent." /></TableHead>
          <TableHead scope="col"><ColumnHeading label="TTFA" help={METRIC_HELP.ttfa} /></TableHead>
          <TableHead scope="col"><ColumnHeading label="Until next transcript" help={METRIC_HELP.transcriptInterval} /></TableHead>
          <TableHead scope="col" className="w-[50%]"><ColumnHeading label="Text" help={METRIC_HELP.utterance} /></TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {turns.map((turn, i) => (
          <TableRow key={i}>
            <TableCell>{turn.index != null ? turn.index + 1 : i + 1}</TableCell>
            <TableCell>
              {turn.speaker === "assistant" ? "Nova Sonic" : "Caller"}
            </TableCell>
            <TableCell className="whitespace-nowrap tabular-nums">{fmtMs(turn.ttfaMs)}</TableCell>
            <TableCell className="whitespace-nowrap tabular-nums">{fmtMs(turn.transcriptIntervalMs)}</TableCell>
            <TableCell className="min-w-[220px] max-w-[640px] whitespace-pre-wrap break-words [overflow-wrap:anywhere]">{turn.text || DASH}</TableCell>
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
  const metadata = r?.intelligence_metadata;
  const legacyIncomplete = !metadata && r?.intelligence_status === "ready" &&
    (!ctx.reason_for_delinquency || !ctx.customer_sentiment || !r.next_best_action);
  const fieldText = (field: string, value?: string | null) => intelligenceFieldText(r, field, value);

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
            <InfoHelp label="Call disposition">The saved outcome of the conversation. This may be used to update the account or choose a follow-up action.</InfoHelp>
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <DispositionBadge disposition={r?.disposition} />
          {r?.termination?.telephone_completion_confirmed === false && (
            <p role="status" className="text-xs text-amber-800 dark:text-amber-300">
              Call ending could not be confirmed. Intelligence and follow-up actions are on hold.
            </p>
          )}
          {r?.termination?.outcome === "technical_failure" && (
            <p role="status" className="text-xs text-amber-800 dark:text-amber-300">The call ended because of a connection or service failure.</p>
          )}
          <p className="text-xs text-muted-foreground">
            Downstream triggers:{" "}
            {triggerParts.length ? triggerParts.join(" \u00b7 ") : DASH}
          </p>
        </CardContent>
      </Card>

      <Card className="bg-background/60">
        <CardHeader>
          <CardTitle className="flex flex-wrap items-center gap-1 text-sm">
            Structured Data &amp; Next-Best-Action
            <InfoHelp label="Intelligence">Payment details and context found in the call, followed by a suggested next step. Missing details are labelled below so an empty field does not look like a confirmed answer.</InfoHelp>
            <IntelStatusBadge status={r?.intelligence_status} />
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-3 text-sm">
          <p className="break-words text-xs text-muted-foreground">Analysis model: {metadata?.model_id || "Not recorded for this call"}</p>
          {r?.intelligence_status === "partial" && <p role="status" className="text-xs text-amber-800 dark:text-amber-300">Some information could not be captured completely. Review the labelled fields and transcript before following up.</p>}
          {legacyIncomplete && <p role="status" className="text-xs text-amber-800 dark:text-amber-300">This saved analysis has gaps in its context or recommendation. The record does not explain why those fields are empty; consult the transcript.</p>}
          {r?.intelligence_status === "pending" && <p role="status" className="text-xs text-muted-foreground">The call is still being analysed. The fields below may update shortly.</p>}
          {r?.intelligence_status === "failed" && <p role="status" className="text-xs text-amber-800 dark:text-amber-300">The analysis could not be completed. Use the transcript to review this call.</p>}
          <FieldGroup title="Payment specifics">
            <Field label="Promised amount">
              {pay.promised_amount != null
                ? fmtMoney(pay.promised_amount, r?.currency)
                : fieldText("payment_specifics.promised_amount")}
            </Field>
            <Field label="Agreed date">{fieldText("payment_specifics.agreed_date", pay.agreed_date)}</Field>
            <Field label="Preferred payment method">
              {fieldText("payment_specifics.preferred_payment_method", pay.preferred_payment_method)}
            </Field>
          </FieldGroup>
          {r?.payment_date_resolution && (
            <div className="space-y-1 text-xs">
              {r.payment_date_resolution.requires_review && <p role="status" className="text-amber-800 dark:text-amber-300">{paymentDateReviewExplanation(r.payment_date_resolution.reason)}</p>}
              {r.payment_date_resolution.phrase && <p className="break-words text-muted-foreground">Caller’s date phrase: “{r.payment_date_resolution.phrase}”</p>}
              {r.payment_date_resolution.reference_date && <p className="text-muted-foreground">Date reference: {r.payment_date_resolution.reference_date}{r.payment_date_resolution.reference_timezone ? ` · ${r.payment_date_resolution.reference_timezone}` : ""}</p>}
            </div>
          )}
          <FieldGroup title="Contextual data">
            <Field label="Reason for delinquency">
              {fieldText("contextual_data.reason_for_delinquency", ctx.reason_for_delinquency)}
            </Field>
            <Field label="Customer sentiment">
              {fieldText("contextual_data.customer_sentiment", ctx.customer_sentiment)}
            </Field>
          </FieldGroup>
          <FieldGroup title="Next-best-action">
            <Field label="Suggested next step" wide>
              {fieldText("next_best_action", r?.next_best_action)}
            </Field>
          </FieldGroup>
        </CardContent>
      </Card>
    </div>
  );
}

function paymentDateReviewExplanation(reason?: string | null): string {
  const explanations: Record<string, string> = {
    ambiguous_week_of_month: "The week wording is ambiguous. Confirm the exact day and date the caller means.",
    tentative_date: "The caller's date is tentative. Confirm a firm payment date before acting on it.",
    empty_phrase: "No payment-date phrase was captured. Confirm an exact date with the caller.",
    no_payment_date: "No payment date was confirmed in the conversation.",
    invalid_calendar_date: "The stated date is not a valid calendar date. Confirm the intended date.",
    past_date: "The calculated date precedes the call. Confirm the intended future date.",
    invalid_date_expression: "The date wording could not be interpreted. Confirm an exact calendar date.",
    model_unresolved: "Date calculation did not resolve the caller's wording. Confirm an exact date.",
    invalid_model_output: "Date calculation did not return a valid date. Confirm the intended date before acting.",
    empty_model_program: "Date calculation did not complete. Confirm the intended date before acting.",
    incomplete_model_program: "Date calculation did not finish. Confirm the intended date before acting.",
    code_generation_failed: "Date calculation encountered a service error. Confirm the intended date before acting.",
    code_interpreter_failed: "Date calculation encountered a service error. Confirm the intended date before acting.",
    code_interpreter_execution_error: "Date calculation failed. Confirm the intended date before acting.",
    unconfirmed_model_iso_date: "The extracted date was not verified against the caller's wording. Confirm the intended date.",
  };
  return (reason && explanations[reason]) || "No verified payment date was recorded. The calculation details are unavailable for this record; confirm an exact date with the caller.";
}

function FieldGroup({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div className="rounded-md border bg-card p-3">
      <div className="mb-2 border-b pb-2 text-[0.68rem] font-semibold uppercase tracking-wide text-muted-foreground">
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
          : "flex items-baseline justify-between gap-3 border-b border-dashed pb-1 last:border-0"
      }
    >
      <dt className="text-muted-foreground">{label}</dt>
      <dd className={wide ? "break-words" : "min-w-0 break-words text-right"}>{children}</dd>
    </div>
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
          <button className="inline-flex max-w-full items-start gap-2 rounded text-left text-xs leading-relaxed hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
            <Settings2 className="h-3.5 w-3.5" />
            {summary}
          </button>
        </PopoverTrigger>
        <PopoverContent className="w-96 max-w-[calc(100vw-2rem)] text-xs">
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
