import { ChevronDown, ExternalLink } from "lucide-react";
import type { CallRecord } from "@/lib/api";
import { sonicCostView, sonicTokens, sonicUsd } from "@/lib/sonic-cost";
import { ColumnHeading, InfoHelp } from "@/components/InfoHelp";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";

function priceDate(value?: string) {
  if (!value || !/^\d{4}-\d{2}-\d{2}T/.test(value)) return null;
  const date = new Date(value);
  return Number.isFinite(date.getTime())
    ? date.toLocaleDateString("en-US", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" }) : null;
}

export function SonicCost({ record }: { record: CallRecord }) {
  const view = sonicCostView(record);
  const cost = record.sonic_cost;
  const checked = priceDate(cost?.pricing?.checked_at);
  const rows = view.state === "estimated" || view.state === "partial" ? cost?.breakdown ?? [] : [];
  return (
    <section aria-label="Nova 2.5 Sonic cost" className="rounded-lg border border-border-strong bg-card px-3 py-2">
      <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1">
        <div className="flex min-w-0 items-center">
          <h3 className="text-sm font-semibold">Nova 2.5 Sonic cost</h3>
          <InfoHelp label="Nova 2.5 Sonic cost">
            <p>Calculated only after the call disconnects: each recorded token count × its regional price. Audio and text have different prices.</p>
            <p className="mt-2">This is a public list-price estimate in US dollars, not an AWS invoice. It excludes Luna and other models, Twilio, infrastructure, discounts, and taxes. Older calls use the displayed price snapshot.</p>
          </InfoHelp>
        </div>
        <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
          <strong className="text-base tabular-nums">{view.amount}</strong>
          <span className="text-xs text-muted-foreground">{view.label}{cost?.region ? ` · ${cost.region}` : ""}</span>
        </div>
      </div>
      {view.explanation && <p className={`text-xs ${view.state === "partial" ? "text-amber-800 dark:text-amber-300" : "text-muted-foreground"}`}>{view.explanation}</p>}
      {view.showUsage && <div className="flex flex-wrap items-center gap-x-4 text-xs">
        <span className="inline-flex items-center"><span>{view.state === "estimated" ? "Input" : "Recorded input"}: <strong className="font-medium tabular-nums">{sonicTokens(cost?.input_tokens)}</strong></span><InfoHelp label="Input tokens">Units of audio and text sent to Nova during the call, including the caller's audio, prompt, and tool results. A partial record shows only the usage that was captured.</InfoHelp></span>
        <span className="inline-flex items-center"><span>{view.state === "estimated" ? "Output" : "Recorded output"}: <strong className="font-medium tabular-nums">{sonicTokens(cost?.output_tokens)}</strong></span><InfoHelp label="Output tokens">Units of audio and text produced by Nova. These are token counts, not seconds or a word count. A partial record may miss some usage.</InfoHelp></span>
      </div>}
      {rows.length > 0 && <Collapsible>
        <CollapsibleTrigger className="group inline-flex min-h-8 items-center gap-1 rounded text-xs font-medium text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
          Token breakdown<ChevronDown aria-hidden="true" className="h-3.5 w-3.5 transition-transform group-data-[state=open]:rotate-180" />
        </CollapsibleTrigger>
        <CollapsibleContent className="space-y-1.5 pb-1">
          <Table aria-label="Nova Sonic token cost breakdown" className="min-w-[470px] text-xs">
            <TableHeader><TableRow>
              <TableHead scope="col"><ColumnHeading label="Token type" help="Input is sent to Nova; output comes back from Nova. Audio is sound, while text includes written prompts, transcripts, and tool messages." /></TableHead>
              <TableHead scope="col"><ColumnHeading label="Tokens" help="How many units AWS recorded for this kind of input or output. These counts are taken from model usage, not guessed from the length of the call." /></TableHead>
              <TableHead scope="col"><ColumnHeading label="USD / 1M tokens" help="The public price in US dollars for one million tokens of this type in the call's AWS region." /></TableHead>
              <TableHead scope="col"><ColumnHeading label="Cost (USD)" help="Tokens divided by one million, multiplied by the price. Add these rows for the displayed estimate or observed-usage subtotal." /></TableHead>
            </TableRow></TableHeader>
            <TableBody>{rows.map((row) => <TableRow key={`${row.direction}-${row.modality}`}>
              <TableCell className="py-1.5">{row.modality === "audio" ? "Audio" : "Text"} {row.direction}</TableCell>
              <TableCell className="py-1.5 tabular-nums">{sonicTokens(row.tokens)}</TableCell>
              <TableCell className="py-1.5 tabular-nums">{sonicUsd(row.rate_per_million_usd)}</TableCell>
              <TableCell className="py-1.5 tabular-nums">{sonicUsd(row.cost_usd)}</TableCell>
            </TableRow>)}</TableBody>
          </Table>
          <div className="flex flex-wrap items-center gap-x-2 text-xs text-muted-foreground">
            <a href="https://aws.amazon.com/nova/pricing/" target="_blank" rel="noreferrer" className="inline-flex min-h-8 items-center gap-1 rounded underline underline-offset-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">AWS pricing<ExternalLink aria-hidden="true" className="h-3 w-3" /></a>
            {checked && <span>Checked {checked}</span>}
            <InfoHelp label="Price and usage source">
              <p>Prices: {cost?.pricing?.source || "AWS public pricing"}{cost?.pricing?.catalog_version ? `, catalog ${cost.pricing.catalog_version}` : ""}.</p>
              <p className="mt-2">Usage: {cost?.usage_source?.includes("cloudwatch") ? "recovered from this call's CloudWatch records" : "saved model usage for this call"}. {cost?.usage_complete ? "Complete recorded usage." : "Only part of the usage is available."}</p>
              {priceDate(cost?.pricing?.effective_at) && <p className="mt-2">Prices effective {priceDate(cost?.pricing?.effective_at)}. The same snapshot is used to estimate older calls.</p>}
            </InfoHelp>
          </div>
        </CollapsibleContent>
      </Collapsible>}
    </section>
  );
}
