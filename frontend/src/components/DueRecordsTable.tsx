import { Fragment, useId, useMemo, useState } from "react";
import { ChevronDown, ExternalLink, PhoneCall, RefreshCw } from "lucide-react";
import {
  callDashboardUrl,
  DASH,
  dispositionLabel,
  fmtDateTime,
  fmtMoney,
  fmtMs,
  fmtRelativeTime,
  type AppConfig,
  type CallSummary,
  type DueRecord,
} from "@/lib/api";
import { DispositionBadge } from "@/components/DispositionBadge";
import { ColumnHeading } from "@/components/InfoHelp";
import { METRIC_HELP } from "@/lib/metric-help";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";

interface Props {
  dues: DueRecord[];
  calls: CallSummary[];
  duesLoading: boolean;
  callsLoading: boolean;
  duesError: string | null;
  callsError: string | null;
  selectedCallId: string | null;
  calling: string | null;
  config: AppConfig | null;
  onCall: (accountId?: string) => void;
  onSelectCall: (callId?: string) => void;
  onRefresh: () => Promise<void>;
}

function newestFirst(a: CallSummary, b: CallSummary) {
  const time = (call: CallSummary) => {
    const value = call.created_at ? Date.parse(call.created_at) : 0;
    return Number.isFinite(value) ? value : 0;
  };
  return time(b) - time(a);
}

function statusClasses(status?: string | null) {
  switch (status) {
    case "paid":
    case "promised_to_pay": return "border-emerald-500/40 bg-emerald-50 text-emerald-700 dark:bg-emerald-500/10 dark:text-emerald-300";
    case "disputed": return "border-red-500/40 bg-red-50 text-red-700 dark:bg-red-500/10 dark:text-red-300";
    case "callback": return "border-amber-500/40 bg-amber-50 text-amber-800 dark:bg-amber-500/10 dark:text-amber-300";
    default: return "border-slate-500/40 bg-slate-100 text-slate-600 dark:bg-slate-500/10 dark:text-slate-300";
  }
}

export function DueRecordsTable(props: Props) {
  const { dues, calls, duesLoading, callsLoading, duesError, callsError, onRefresh, config } = props;
  const [refreshing, setRefreshing] = useState(false);
  const unlinkedCalls = useMemo(() => {
    const accountIds = new Set(dues.map((due) => due.account_id).filter(Boolean));
    return calls.filter((call) => !call.account_id || !accountIds.has(call.account_id)).sort(newestFirst);
  }, [dues, calls]);
  const callingUnavailable = config?.twilio_configured === false
    ? "Calling is unavailable because Twilio is not configured."
    : config?.public_base_url_set === false
      ? "Calling is unavailable until the public callback URL is configured."
      : null;
  const refresh = async () => {
    setRefreshing(true);
    try { await onRefresh(); } finally { setRefreshing(false); }
  };

  return (
    <Card className="mt-6 overflow-hidden">
      <CardHeader className="gap-3 px-4 py-4 sm:flex-row sm:items-center sm:justify-between sm:px-5">
        <div>
          <div className="flex items-center gap-2.5">
            <CardTitle className="text-base">Due records</CardTitle>
            {!duesLoading && <Badge variant="secondary" className="tabular-nums">{dues.length}</Badge>}
          </div>
          <p className="mt-1 text-xs text-muted-foreground">Account balances and call history</p>
        </div>
        <Button variant="outline" size="sm" className="h-11 self-start sm:h-9" disabled={refreshing || duesLoading || callsLoading} onClick={() => void refresh()}>
          <RefreshCw aria-hidden="true" className={`mr-2 h-3.5 w-3.5 ${refreshing ? "motion-safe:animate-spin" : ""}`} />
          {refreshing ? "Refreshing…" : "Refresh"}
        </Button>
      </CardHeader>
      <CardContent className="p-0">
        {(duesError || callsError) && (
          <div role="alert" className="mx-4 mb-4 space-y-1 rounded-md border border-amber-500/40 bg-amber-50 px-3 py-2 text-sm text-amber-800 dark:bg-amber-500/10 dark:text-amber-300 sm:mx-5">
            {duesError && <p>Due records could not be refreshed. {duesError}</p>}
            {callsError && <p>Call history could not be refreshed. {callsError}</p>}
            <p className="text-xs">Available records remain visible. Use Refresh to try again.</p>
          </div>
        )}
        {callingUnavailable && <p role="status" className="mx-4 mb-4 text-sm text-amber-800 dark:text-amber-300 sm:mx-5">{callingUnavailable}</p>}
        <p className="border-t px-4 py-2 text-xs text-muted-foreground md:hidden">Scroll the table to see all columns.</p>
        <Table className="min-w-[1030px]" aria-label="Due records" aria-busy={duesLoading}>
          <TableHeader>
            <TableRow className="hover:bg-transparent">
              <TableHead scope="col" className="w-[170px] pl-5"><ColumnHeading label="Account" help="The reference number for this balance. Open it to see calls linked to this account." /></TableHead>
              <TableHead scope="col" className="min-w-[180px]"><ColumnHeading label="Debtor" help="The person or business who owes this balance." /></TableHead>
              <TableHead scope="col" className="text-right"><ColumnHeading label="Amount due" help="How much this account still owes, in the currency shown." /></TableHead>
              <TableHead scope="col"><ColumnHeading label="Due date" help="The date this balance was supposed to be paid." /></TableHead>
              <TableHead scope="col"><ColumnHeading label="Status" help="The latest saved state of the account, such as paid, disputed or promised to pay." /></TableHead>
              <TableHead scope="col" className="text-center"><ColumnHeading label="Calls" help="How many calls in the available history are linked to this account." /></TableHead>
              <TableHead scope="col"><ColumnHeading label="Latest call" help="How long ago the most recent recorded call began. Open the account for its exact date and time." /></TableHead>
              <TableHead scope="col" className="pr-5 text-right"><ColumnHeading label="Action" help="Call now starts a real phone call to the contact configured for this account." /></TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {duesLoading && Array.from({ length: 3 }, (_, index) => (
              <TableRow key={index}><TableCell colSpan={8} className="px-5 py-5"><Skeleton className="h-5 w-full" /></TableCell></TableRow>
            ))}
            {!duesLoading && dues.length === 0 && (
              <TableRow><TableCell colSpan={8} className="px-5 py-10 text-muted-foreground">
                {duesError ? "Due records are unavailable. Refresh to retry." : "No due records yet. Refresh after account records are added."}
              </TableCell></TableRow>
            )}
            {dues.map((due, index) => <DueRow key={due.account_id ?? index} due={due} {...props} callingUnavailable={callingUnavailable} />)}
          </TableBody>
        </Table>
        {!duesLoading && unlinkedCalls.length > 0 && (
          <section className="border-t px-4 py-4 sm:px-5" aria-labelledby="unlinked-calls-title">
            <h3 id="unlinked-calls-title" className="mb-1 text-sm font-medium">{duesError ? "Available calls" : "Calls without a due record"}</h3>
            <p className="mb-3 text-xs text-muted-foreground">{duesError ? "Account records are unavailable; these calls can still be reviewed." : "These calls do not match an account in the current due records."}</p>
            <CallHistory calls={unlinkedCalls} selectedCallId={props.selectedCallId} onSelect={props.onSelectCall} config={config} showAccount />
          </section>
        )}
      </CardContent>
    </Card>
  );
}

function DueRow({ due, calls, callsLoading, callsError, selectedCallId, calling, callingUnavailable, onCall, onSelectCall, config }: Props & { due: DueRecord; callingUnavailable: string | null }) {
  const [open, setOpen] = useState(false);
  const accountCalls = useMemo(() => calls.filter((call) => Boolean(call.account_id) && call.account_id === due.account_id).sort(newestFirst), [calls, due.account_id]);
  const historyId = useId();
  const label = due.account_id || "Unknown account";
  return (
    <Fragment>
      <TableRow className={open ? "bg-accent/60 hover:bg-accent/60" : ""}>
        <TableCell className="py-1.5 pl-3">
          <button type="button" aria-expanded={open} aria-controls={open ? historyId : undefined} aria-label={`${open ? "Hide" : "Show"} calls for ${label}`} className="inline-flex min-h-11 items-center gap-2 rounded px-2 text-left font-medium hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" onClick={() => setOpen(!open)}>
            <ChevronDown aria-hidden="true" className={`h-4 w-4 shrink-0 motion-safe:transition-transform ${open ? "" : "-rotate-90"}`} />
            {label}
          </button>
        </TableCell>
        <TableCell className="max-w-[260px] whitespace-normal break-words font-medium">{due.debtor_name || DASH}</TableCell>
        <TableCell className="whitespace-nowrap text-right tabular-nums">{fmtMoney(due.amount_due, due.currency)}</TableCell>
        <TableCell className="whitespace-nowrap text-muted-foreground">{due.due_date || DASH}</TableCell>
        <TableCell><Badge variant="outline" className={`whitespace-nowrap ${statusClasses(due.status)}`}>{due.status ? dispositionLabel(due.status) : DASH}</Badge></TableCell>
        <TableCell className="text-center tabular-nums">{callsLoading ? "…" : callsError && !accountCalls.length ? DASH : accountCalls.length}</TableCell>
        <TableCell className="whitespace-nowrap text-xs text-muted-foreground" title={fmtDateTime(accountCalls[0]?.created_at)}>{fmtRelativeTime(accountCalls[0]?.created_at) || DASH}</TableCell>
        <TableCell className="pr-5 text-right">
          <Button size="sm" className="h-11 whitespace-nowrap sm:h-9" disabled={calling !== null || !due.account_id || Boolean(callingUnavailable)} onClick={() => onCall(due.account_id)} aria-label={`Call now for ${label}`}>
            <PhoneCall aria-hidden="true" className="mr-1.5 h-3.5 w-3.5" />{calling === due.account_id ? "Calling…" : "Call now"}
          </Button>
        </TableCell>
      </TableRow>
      {open && (
        <TableRow className="bg-background hover:bg-background">
          <TableCell colSpan={8} className="p-0">
            <section id={historyId} aria-label={`Calls for ${label}`} className="m-3 rounded-lg border border-border-strong border-l-[3px] border-l-primary bg-card px-3 py-3 sm:ml-6">
              <div className="mb-3 flex items-center gap-2"><h3 className="text-xs font-semibold">Call history</h3><span className="text-xs text-muted-foreground">{label} · most recent first</span></div>
              {callsLoading ? <div role="status" className="space-y-2"><span className="text-xs text-muted-foreground">Loading call history…</span><Skeleton className="h-9 w-full" /></div>
                : accountCalls.length ? <CallHistory calls={accountCalls} selectedCallId={selectedCallId} onSelect={onSelectCall} config={config} />
                  : <p className="py-2 text-sm text-muted-foreground">{callsError ? "Call history is unavailable. Refresh to retry." : "No calls recorded for this account yet."}</p>}
            </section>
          </TableCell>
        </TableRow>
      )}
    </Fragment>
  );
}

function CallHistory({ calls, selectedCallId, onSelect, config, showAccount = false }: { calls: CallSummary[]; selectedCallId: string | null; onSelect: (id?: string) => void; config: AppConfig | null; showAccount?: boolean }) {
  return (
    <Table className="min-w-[950px]" aria-label="Call history">
      <TableHeader>
        <TableRow className="hover:bg-transparent">
          <TableHead scope="col"><ColumnHeading label="Started" help="When the call began, shown in your device's local time zone." /></TableHead>
          <TableHead scope="col"><ColumnHeading label={showAccount ? "Call SID / Account" : "Call SID"} help="Twilio's unique reference for this phone call. Select it to open the transcript and details. The account reference is shown below when available." /></TableHead>
          <TableHead scope="col"><ColumnHeading label="Disposition" help="The saved outcome of the conversation, such as a promise to pay or a request for a callback." /></TableHead>
          <TableHead scope="col"><ColumnHeading label="Intelligence" help="Whether the call summary and suggested next step are ready. Needs review means some requested information was not captured successfully." /></TableHead>
          <TableHead scope="col" className="text-right"><ColumnHeading label="Turns" help={METRIC_HELP.turns} /></TableHead>
          <TableHead scope="col" className="text-right"><ColumnHeading label="Duration" help={METRIC_HELP.total} /></TableHead>
          <TableHead scope="col" className="pl-5"><ColumnHeading label="Dashboard URL" help="A link to this app's CloudWatch dashboard for detailed call diagnostics. The link panel explains how to filter it to this call." /></TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>{calls.map((call, index) => (
        <TableRow key={call.call_sid ?? index} data-state={call.call_sid === selectedCallId ? "selected" : undefined}>
          <TableCell className="whitespace-nowrap text-xs">
            <div>{fmtDateTime(call.created_at)}</div>
            <div className="mt-0.5 text-muted-foreground">{fmtRelativeTime(call.created_at) || "Start time unavailable"}</div>
          </TableCell>
          <TableCell>
            {call.call_sid ? <button type="button" className="inline-flex min-h-11 items-center rounded px-1 font-mono text-xs text-primary hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" onClick={() => onSelect(call.call_sid)} aria-label={`View call details for ${call.call_sid}`} aria-controls={selectedCallId ? "call-detail" : undefined}>{call.call_sid}</button> : DASH}
            {showAccount && <div className="text-xs text-muted-foreground">{call.account_id || "Account unavailable"}</div>}
          </TableCell>
          <TableCell><DispositionBadge disposition={call.disposition} /></TableCell>
          <TableCell className={`whitespace-nowrap text-xs ${["failed", "partial"].includes(call.intelligence_status ?? "") ? "text-amber-800 dark:text-amber-300" : "text-muted-foreground"}`}>{({ ready: "Ready", partial: "Needs review", pending: "Processing…", failed: "Unavailable", withheld_unconfirmed_termination: "Awaiting call completion" } as Record<string, string>)[call.intelligence_status ?? ""] ?? DASH}</TableCell>
          <TableCell className="text-right tabular-nums">{call.turn_count ?? DASH}</TableCell>
          <TableCell className="whitespace-nowrap text-right text-xs tabular-nums">{fmtMs(call.total_e2e_ms)}</TableCell>
          <TableCell className="pl-5">{call.call_sid ? <CallDashboardLink call={call} config={config} /> : DASH}</TableCell>
        </TableRow>
      ))}</TableBody>
    </Table>
  );
}

function CallDashboardLink({ call, config }: { call: CallSummary; config: AppConfig | null }) {
  const url = call.dashboard_url || callDashboardUrl(call.call_sid || "", config?.aws_region, config?.app_log_group);
  return (
    <Popover>
      <PopoverTrigger asChild><button type="button" className="inline-flex min-h-11 items-center gap-1.5 whitespace-nowrap rounded text-xs text-primary hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" aria-label={`Dashboard URL for ${call.call_sid}`}><ExternalLink aria-hidden="true" className="h-3.5 w-3.5" />CloudWatch</button></PopoverTrigger>
      <PopoverContent className="max-w-[calc(100vw-2rem)] text-xs" align="end">
        <p className="font-medium">Per-call CloudWatch dashboard</p>
        <p className="mt-2 text-muted-foreground">Open the dashboard, then paste this Call SID into its CALL_SID field to filter the widgets.</p>
        <code className="my-3 block break-all rounded bg-muted p-2 text-foreground">{call.call_sid}</code>
        <a href={url} target="_blank" rel="noopener noreferrer" className="inline-flex min-h-11 items-center gap-1.5 rounded font-medium text-primary hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">Open CloudWatch dashboard <ExternalLink aria-hidden="true" className="h-3.5 w-3.5" /></a>
      </PopoverContent>
    </Popover>
  );
}
