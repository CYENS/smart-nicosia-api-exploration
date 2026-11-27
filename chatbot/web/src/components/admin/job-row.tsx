/* One collector job on the admin page, as a shadcn Item: its switch, title, a one-line summary,
   then how it last ran, when it runs next and where its data comes from (a "From ..." link that
   opens the source). How often it runs, Run now and Reset sit on the right, with Read more,
   which opens "How it's collected" (how-sheet.tsx) for everything else.

   The controls follow the admin lock: without the admin password they are shown but can't be
   used. While a change is saved they stay focusable (a disabled control drops the keyboard's
   focus) and ignore presses until the answer is back. */
import { RotateCcwIcon } from "lucide-react"
import { useState, type ReactNode } from "react"
import { toast } from "sonner"

import { SourceLink } from "@/components/admin/source-link"
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription,
  AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Item, ItemActions, ItemContent, ItemDescription, ItemMedia, ItemTitle } from "@/components/ui/item"
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Spinner } from "@/components/ui/spinner"
import { Switch } from "@/components/ui/switch"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import {
  ago, cadenceLabel, number, openHow, requestsPerDay, resetJob, runJobNow, until, updateJob,
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

/* A job's changes, shared by its row and its sheet: a change that takes the city's requests over
   the budget asks first (the returned `dialog` must be rendered), and every outcome is a toast. */
export function useJobControls(job: CollectorJob, report: CollectorsReport): {
  locked: boolean; pending: boolean
  change: (patch: Partial<JobSettings>, done: string) => void
  runNow: () => void; reset: () => void; dialog: ReactNode
} {
  const pending = useCollectors((s) => !!s.pending[job.name])
  const locked = !report.admin.unlocked
  const [overBudget, setOverBudget] = useState<{ patch: Partial<JobSettings>; done: string; perDay: number } | null>(null)

  async function commit(patch: Partial<JobSettings>, done: string) {
    if (await updateJob(job.name, patch)) toast.success(done)
    else toast.error(useCollectors.getState().error ?? "The change wasn't saved.")
  }

  function change(patch: Partial<JobSettings>, done: string) {
    if (pending || locked) return
    const next = { enabled: job.enabled, every_minutes: job.every_minutes, night_only: job.night_only, ...patch }
    const perDay = report.budget.per_day - job.requests_per_day + requestsPerDay(job, next, report.night_fraction)
    if (job.counts_against_budget && perDay > report.budget.limit && perDay > report.budget.per_day) {
      setOverBudget({ patch, done, perDay })
      return
    }
    void commit(patch, done)
  }

  async function runNow() {
    if (pending || locked || job.state !== null) return
    if (await runJobNow(job.name)) toast.success(`${job.title} will run now`)
    else toast.error(useCollectors.getState().error ?? "It couldn't be started.")
  }

  async function reset() {
    if (pending || locked) return
    if (await resetJob(job.name)) toast.success(`${job.title} is back to its defaults`)
    else toast.error(useCollectors.getState().error ?? "It couldn't be reset.")
  }

  const dialog = (
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
  )

  return { locked, pending, change, runNow: () => void runNow(), reset: () => void reset(), dialog }
}

export function JobRow({ job, report }: { job: CollectorJob; report: CollectorsReport }) {
  const { locked, pending, change, runNow, reset, dialog } = useJobControls(job, report)
  const id = `job-${job.name}`
  const run = lastRun(job)
  const next = nextRun(job, report)
  const cadences = report.cadences.includes(job.every_minutes)
    ? report.cadences : [...report.cadences, job.every_minutes].sort((a, b) => a - b)

  const defaults = [
    job.changed.includes("enabled") && (job.defaults.enabled ? "on" : "off"),
    job.changed.includes("every_minutes") && cadenceLabel(job.defaults.every_minutes).toLowerCase(),
    job.changed.includes("night_only") && (job.defaults.night_only ? "only after dark" : "day and night"),
  ].filter(Boolean).join(", ")

  return (
    <Item role="listitem" data-job={job.name} className="items-start">
      <ItemMedia>
        <Switch
          checked={job.enabled}
          disabled={locked}
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
          {next && <span>{next}</span>}
          <SourceLink source={job.source} />
        </div>
      </ItemContent>
      <ItemActions className="flex-wrap">
        {job.changed.length > 0 && !locked && (
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
          disabled={locked}
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
        <Button
          variant="outline" size="sm" onClick={runNow} disabled={locked}
          aria-disabled={pending || job.state !== null || undefined} aria-describedby={`${id}-title`}
        >
          {job.state === "running" && <Spinner data-icon="inline-start" />}
          {job.state === "running" ? "Running" : job.state === "queued" ? "Waiting" : "Run now"}
        </Button>
        <Button variant="ghost" size="sm" onClick={() => openHow(job.name)} aria-label={`Read more: how ${job.title} is collected`}>
          Read more
        </Button>
      </ItemActions>
      {dialog}
    </Item>
  )
}
