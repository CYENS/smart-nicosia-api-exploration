/* One collector job on the admin page, as a shadcn Item that opens (Collapsible) onto its
   details. The row stays short: its switch, title, a one-line summary, then how it last ran,
   when it runs next and where its data comes from (a "From ..." line that opens the source).
   The details hold the rest: what it records in full, what the twin uses it for, the tables it
   writes, its last run, and its night window.

   Controls are never disabled while a change is saved: a disabled control drops the keyboard's
   focus. They ignore presses until the answer is back instead. */
import { ChevronDownIcon, RotateCcwIcon } from "lucide-react"
import { useState } from "react"
import { toast } from "sonner"

import { SourceLink } from "@/components/admin/source-link"
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription,
  AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible"
import { Field, FieldContent, FieldDescription, FieldLabel } from "@/components/ui/field"
import { Item, ItemActions, ItemContent, ItemDescription, ItemFooter, ItemMedia, ItemTitle } from "@/components/ui/item"
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Spinner } from "@/components/ui/spinner"
import { Switch } from "@/components/ui/switch"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import {
  ago, cadenceLabel, clockTime, number, requestsPerDay, resetJob, runJobNow, until, updateJob,
  useCollectors, type CollectorJob, type CollectorsReport, type JobSettings,
} from "@/lib/collectors"
import { cn } from "@/lib/utils"

export type Tone = "live" | "ok" | "idle" | "off" | "error"

// A status light, as the data sources panel draws them: sodium for live, signal red for failed.
const DOT: Record<Tone, string> = {
  live: "bg-sodium shadow-[0_0_6px_1px] shadow-sodium/50",
  ok: "bg-sodium/70",
  idle: "border border-muted-foreground/70 bg-transparent",
  off: "bg-muted-foreground/40",
  error: "bg-signal",
}

export function Dot({ tone, className }: { tone: Tone; className?: string }) {
  return <span aria-hidden="true" className={cn("inline-block size-2 shrink-0 rounded-full", DOT[tone], className)} />
}

function lastRun(job: CollectorJob): { tone: Tone; text: string } {
  if (job.state === "running") return { tone: "live", text: "Running now" }
  if (job.state === "queued") return { tone: "live", text: "Waiting to run" }
  const last = job.last
  if (!last?.started) return { tone: job.enabled ? "idle" : "off", text: "Never run" }
  const when = ago(last.started)
  if (last.status === "error") return { tone: "error", text: `Failed ${when}` }
  if (last.status === "partial") return { tone: "error", text: `Ran ${when}, some requests failed` }
  return { tone: job.enabled ? "ok" : "off", text: `Ran ${when}` }
}

// When the job runs next; nothing while collection is paused (the overview already says so).
function nextRun(job: CollectorJob, r: CollectorsReport): string | null {
  if (!job.enabled) return "Switched off"
  if (!r.collecting && r.decided_on_page) return null
  if (!r.collecting && !r.other_collector_recent) return "Runs once collection starts"
  if (job.waiting_for_dark) return "Waits for dark"
  const next = until(job.next_due)
  return next === "due now" ? "Due now" : `Next ${next}`
}

export function JobRow({ job, report }: { job: CollectorJob; report: CollectorsReport }) {
  const pending = useCollectors((s) => !!s.pending[job.name])
  const [open, setOpen] = useState(false)
  const [overBudget, setOverBudget] = useState<{ patch: Partial<JobSettings>; done: string; perDay: number } | null>(null)
  const id = `job-${job.name}`
  const run = lastRun(job)
  const busy = job.state !== null
  const cadences = report.cadences.includes(job.every_minutes)
    ? report.cadences : [...report.cadences, job.every_minutes].sort((a, b) => a - b)

  async function commit(patch: Partial<JobSettings>, done: string) {
    if (await updateJob(job.name, patch)) toast.success(done)
    else toast.error(useCollectors.getState().error ?? "The change wasn't saved.")
  }

  // A change that takes the city's requests over the budget asks first.
  function change(patch: Partial<JobSettings>, done: string) {
    if (pending) return
    const next = { enabled: job.enabled, every_minutes: job.every_minutes, night_only: job.night_only, ...patch }
    const perDay = report.budget.per_day - job.requests_per_day + requestsPerDay(job, next, report.night_fraction)
    if (job.counts_against_budget && perDay > report.budget.limit && perDay > report.budget.per_day) {
      setOverBudget({ patch, done, perDay })
      return
    }
    void commit(patch, done)
  }

  async function runNow() {
    if (pending || busy) return
    if (await runJobNow(job.name)) toast.success(`${job.title} will run now`)
    else toast.error(useCollectors.getState().error ?? "It couldn't be started.")
  }

  async function reset() {
    if (pending) return
    if (await resetJob(job.name)) toast.success(`${job.title} is back to its defaults`)
    else toast.error(useCollectors.getState().error ?? "It couldn't be reset.")
  }

  const defaults = [
    job.changed.includes("enabled") && (job.defaults.enabled ? "on" : "off"),
    job.changed.includes("every_minutes") && cadenceLabel(job.defaults.every_minutes).toLowerCase(),
    job.changed.includes("night_only") && (job.defaults.night_only ? "only after dark" : "day and night"),
  ].filter(Boolean).join(", ")

  return (
    <Collapsible
      open={open}
      onOpenChange={setOpen}
      render={<Item role="listitem" data-job={job.name} className="items-start" />}
    >
      <ItemMedia>
        <Switch
          checked={job.enabled}
          onCheckedChange={(on) => change({ enabled: on }, on ? `${job.title} is on` : `${job.title} is off`)}
          aria-labelledby={`${id}-title`}
          aria-describedby={`${id}-status`}
        />
      </ItemMedia>
      <ItemContent className="min-w-60">
        <ItemTitle id={`${id}-title`} role="heading" aria-level={3}>
          {job.title}
          {job.changed.length > 0 && <Badge variant="outline">Changed</Badge>}
        </ItemTitle>
        <ItemDescription>{job.summary}</ItemDescription>
        <div id={`${id}-status`} className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
          <span className={cn("flex items-center gap-1.5", run.tone === "error" ? "text-signal" : "text-foreground")}>
            {job.state === "running" ? <Spinner aria-hidden="true" /> : <Dot tone={run.tone} />}
            {run.text}
          </span>
          {nextRun(job, report) && <span>{nextRun(job, report)}</span>}
          <SourceLink source={job.source} />
        </div>
      </ItemContent>
      <ItemActions className="flex-wrap">
        {job.changed.length > 0 && (
          <Tooltip>
            <TooltipTrigger render={<Button variant="ghost" size="sm" onClick={reset} aria-disabled={pending || undefined} />}>
              <RotateCcwIcon data-icon="inline-start" />
              Reset
            </TooltipTrigger>
            <TooltipContent>Put back the default: {defaults}</TooltipContent>
          </Tooltip>
        )}
        <Select
          value={String(job.every_minutes)}
          onValueChange={(v) => {
            const every = Number(v)
            if (every !== job.every_minutes) change({ every_minutes: every }, `${job.title} runs ${cadenceLabel(every).toLowerCase()}`)
          }}
        >
          <SelectTrigger size="sm" aria-label={`How often ${job.title} runs`} className="w-40">
            <SelectValue>{cadenceLabel(job.every_minutes)}</SelectValue>
          </SelectTrigger>
          <SelectContent>
            <SelectGroup>
              {cadences.map((m) => (
                <SelectItem key={m} value={String(m)}>{cadenceLabel(m)}</SelectItem>
              ))}
            </SelectGroup>
          </SelectContent>
        </Select>
        <Button variant="outline" size="sm" onClick={runNow} aria-disabled={pending || busy || undefined} aria-describedby={`${id}-title`}>
          {job.state === "running" && <Spinner data-icon="inline-start" />}
          {job.state === "running" ? "Running" : job.state === "queued" ? "Waiting" : "Run now"}
        </Button>
        <CollapsibleTrigger render={<Button variant="ghost" size="icon-sm" aria-label={`Details of ${job.title}`} />}>
          <ChevronDownIcon className={cn("transition-transform motion-reduce:transition-none", open && "rotate-180")} />
        </CollapsibleTrigger>
      </ItemActions>
      <CollapsibleContent render={<ItemFooter />}>
        <dl className="grid w-full gap-x-6 gap-y-2.5 rounded-lg bg-muted/40 p-3.5 text-sm sm:grid-cols-[10rem_1fr]">
          <dt className="text-muted-foreground">What it records</dt>
          <dd>{job.records}</dd>
          <dt className="text-muted-foreground">Used for</dt>
          <dd>{job.uses}</dd>
          <dt className="text-muted-foreground">Cost</dt>
          <dd>
            About {number(job.requests_per_day)} requests a day
            {job.counts_against_budget ? "" : ", outside the city's budget"}.{" "}
            {number(job.requests_24h)} in the last 24 hours
            {job.last && job.last.errors_24h > 0 && <span className="text-signal">, {job.last.errors_24h} failed {job.last.errors_24h === 1 ? "run" : "runs"}</span>}.
          </dd>
          <dt className="text-muted-foreground">Last run</dt>
          <dd>
            {job.last?.started ? (
              <>
                {clockTime(job.last.started)}
                {job.last.seconds != null && `, took ${job.last.seconds} s`}
                {job.last.rows != null && `, ${number(job.last.rows)} ${job.last.rows === 1 ? "row" : "rows"}`}
                {job.last.message && <span className={cn("block", job.last.status === "error" ? "text-signal" : "text-muted-foreground")}>{job.last.message}</span>}
              </>
            ) : "Not yet"}
          </dd>
          <dt className="text-muted-foreground">Writes to</dt>
          <dd>{job.tables.join(", ")}</dd>
          <dt className="sr-only">Night window</dt>
          <dd className="sm:col-span-2">
            <Field orientation="horizontal">
              <Switch
                id={`${id}-night`}
                size="sm"
                checked={job.night_only}
                onCheckedChange={(on) => change({ night_only: on }, on ? `${job.title} runs only after dark` : `${job.title} runs day and night`)}
              />
              <FieldContent>
                <FieldLabel htmlFor={`${id}-night`}>Only after dark</FieldLabel>
                <FieldDescription>For devices that report only while powered, such as street lights.</FieldDescription>
              </FieldContent>
            </Field>
          </dd>
        </dl>
      </CollapsibleContent>

      <AlertDialog open={overBudget !== null} onOpenChange={(o) => { if (!o) setOverBudget(null) }}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Go over the request budget?</AlertDialogTitle>
            <AlertDialogDescription>
              The twin would send the city's API about {number(overBudget?.perDay)} requests a day, over the
              {" "}{number(report.budget.limit)} agreed while the polling agreement is pending.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Keep it as it is</AlertDialogCancel>
            <AlertDialogAction onClick={() => { if (overBudget) void commit(overBudget.patch, overBudget.done); setOverBudget(null) }}>
              Change it anyway
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </Collapsible>
  )
}
