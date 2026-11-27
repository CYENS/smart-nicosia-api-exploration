/* The admin page (/admin): every collector that fills the twin's memory, what it records and
   from where, and its controls. Start or pause all collection, keep or stop keeping what pages
   fetch, switch a job on or off, change how often it runs, run it now, put it back to its
   defaults. Composed from shadcn: Cards for the overview, an ItemGroup per group of jobs, Field
   for the switches, Alert for callouts, Empty for the empty and failed states. Words stay short:
   each row says where its data comes from in one "From ..." link, and Read more opens "How
   it's collected" (how-sheet.tsx). Changes need the admin password: without it the page reads
   but can't change anything. The data and the changes go through lib/collectors.ts. */
import { ArrowLeftIcon, ArrowRightIcon, CircleAlertIcon, CircleCheckIcon, LockIcon, LockOpenIcon, TriangleAlertIcon } from "lucide-react"
import { Fragment, useEffect, useState } from "react"
import { toast } from "sonner"

import { HowSheet } from "@/components/admin/how-sheet"
import { Dot, JobRow, type Tone } from "@/components/admin/job-row"
import { SourceLink } from "@/components/admin/source-link"
import { UnlockButton } from "@/components/admin/unlock"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription,
  AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card"
import { Empty, EmptyContent, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty"
import { Field, FieldContent, FieldDescription, FieldLabel } from "@/components/ui/field"
import { Item, ItemContent, ItemDescription, ItemGroup, ItemMedia, ItemSeparator, ItemTitle } from "@/components/ui/item"
import { Progress } from "@/components/ui/progress"
import { Skeleton } from "@/components/ui/skeleton"
import { Switch } from "@/components/ui/switch"
import { Toaster } from "@/components/ui/sonner"
import { TooltipProvider } from "@/components/ui/tooltip"
import {
  ago, clockTime, loadCollectors, lockChanges, number, setCollecting, setRecording, startPolling,
  useCollectors, type CollectorsReport,
} from "@/lib/collectors"

// Changes need the admin password. Locked: a button to enter it (kept for this tab only).
// Unlocked: a button to forget it. No password on the server: the page says it is read only.
function AdminLock({ r }: { r: CollectorsReport }) {
  if (!r.admin.configured) {
    return (
      <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
        <LockIcon className="size-3.5" aria-hidden="true" />
        Read only. Set CHATBOT_ADMIN_PASSWORD on the server to make changes.
      </p>
    )
  }
  if (r.admin.unlocked) {
    return (
      <Button variant="ghost" size="sm" onClick={() => void lockChanges().then(() => toast.success("Changes are locked again"))}>
        <LockOpenIcon data-icon="inline-start" />
        Lock changes
      </Button>
    )
  }
  return <UnlockButton what="Starting, pausing and changing collectors needs the admin password." />
}

function collectionState(r: CollectorsReport): { tone: Tone; title: string; text: string } {
  if (r.elsewhere_now) {
    return { tone: "live", title: "Another collector is at work", text: "Task Scheduler or Docker is collecting right now. This server waits its turn." }
  }
  if (r.collecting) {
    const p = r.last_pass_here
    const last = p ? ({
      "ran": `${p.jobs?.length ?? 0} ${p.jobs?.length === 1 ? "job" : "jobs"} ran`,
      "nothing due": "nothing was due",
      "elsewhere": "another collector was at work",
      "error": "the pass failed",
    })[p.outcome] : null
    return { tone: "live", title: "Collecting", text: p && last ? `Last check ${ago(p.at)}: ${last}.` : "Checks for due jobs every 30 seconds." }
  }
  if (r.decided_on_page) {
    return { tone: "off", title: "Paused", text: "Stays paused after a restart, for Task Scheduler and Docker too." }
  }
  return {
    tone: "idle", title: "Not collecting",
    text: r.other_collector_recent
      ? `Another collector ran ${ago(r.other_collector_last_run)}.`
      : "Only what open pages fetch is kept.",
  }
}

function CollectionCard({ r }: { r: CollectorsReport }) {
  const pending = useCollectors((s) => !!s.pending.collecting)
  const [confirm, setConfirm] = useState(false)
  const state = collectionState(r)

  async function set(on: boolean) {
    if (pending) return
    if (await setCollecting(on)) toast.success(on ? "Collecting: the first pass starts now" : "Collection is paused")
    else toast.error(useCollectors.getState().error ?? "The change wasn't saved.")
  }

  return (
    <Card>
      <CardHeader>
        <CardDescription>Collection</CardDescription>
        <CardTitle aria-live="polite" className="flex items-center gap-2">
          <Dot tone={state.tone} />
          {state.title}
        </CardTitle>
      </CardHeader>
      <CardContent className="flex-1 text-muted-foreground">{state.text}</CardContent>
      <CardFooter>
        {/* One button that changes its words, so the keyboard stays on it. */}
        <Button
          variant={r.collecting ? "outline" : "default"}
          disabled={!r.admin.unlocked}
          aria-disabled={pending || undefined}
          onClick={() => (r.collecting ? setConfirm(true) : void set(true))}
        >
          {r.collecting ? "Pause collecting" : "Start collecting"}
        </Button>
      </CardFooter>
      <AlertDialog open={confirm} onOpenChange={setConfirm}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Pause all collection?</AlertDialogTitle>
            <AlertDialogDescription>
              Every collector writing this memory stops, including Task Scheduler and Docker. Road travel times
              keep no history of their own, so what isn't recorded while paused is lost.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Keep collecting</AlertDialogCancel>
            <AlertDialogAction onClick={() => { setConfirm(false); void set(false) }}>Pause collecting</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </Card>
  )
}

function BudgetCard({ r }: { r: CollectorsReport }) {
  const { per_day, limit, last_24h, other_per_day } = r.budget
  const over = per_day > limit
  const running = r.collecting || r.elsewhere_now
  return (
    <Card>
      <CardHeader>
        <CardDescription>Requests to the city's API</CardDescription>
        <CardTitle className="font-readout">
          About {number(per_day)} a day{running ? "" : " once collecting"}
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-3 text-muted-foreground">
        <Progress
          value={Math.min(100, Math.round((per_day / limit) * 100))}
          aria-label={`${number(per_day)} of the ${number(limit)} requests a day the budget allows`}
        />
        {over ? (
          <Alert variant="destructive">
            <TriangleAlertIcon />
            <AlertTitle>Over the budget of {number(limit)} a day</AlertTitle>
            <AlertDescription>Slow some jobs down until the API's owners agree to more.</AlertDescription>
          </Alert>
        ) : (
          <p>Budget {number(limit)} a day. {number(last_24h)} sent in the last 24 hours.</p>
        )}
        {other_per_day > 0 && <p>Road travel times ({number(other_per_day)} a day) don't count.</p>}
      </CardContent>
    </Card>
  )
}

function MemoryCard({ r }: { r: CollectorsReport }) {
  const pending = useCollectors((s) => !!s.pending.recording)
  const rec = r.recorder
  const file = r.store.path.split(/[\\/]/).pop()

  async function set(on: boolean) {
    if (pending) return
    if (await setRecording(on)) toast.success(on ? "Keeping what pages fetch" : "No longer keeping what pages fetch")
    else toast.error(useCollectors.getState().error ?? "The change wasn't saved.")
  }

  return (
    <Card>
      <CardHeader>
        <CardDescription>The twin's memory</CardDescription>
        <CardTitle className="flex items-baseline gap-2" title={r.store.path}>
          <span className="truncate">{file}</span>
          {r.store.size_mb != null && <span className="font-readout text-muted-foreground">{number(r.store.size_mb)} MB</span>}
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {r.store.status !== "ok" && r.store.message && (
          <Alert>
            <CircleAlertIcon />
            <AlertTitle>{r.store.status === "missing" ? "Nothing recorded yet" : "Busy for a moment"}</AlertTitle>
            <AlertDescription>{r.store.message.replace(/^./, (c) => c.toUpperCase())}.</AlertDescription>
          </Alert>
        )}
        <Field orientation="horizontal" data-disabled={!rec.on || undefined}>
          <Switch id="recorder" checked={rec.recording} disabled={!rec.on || !r.admin.unlocked} onCheckedChange={(on) => void set(on)} />
          <FieldContent>
            <FieldLabel htmlFor="recorder">Keep what pages fetch</FieldLabel>
            <FieldDescription>
              {!rec.on
                ? "Off on this server (CHATBOT_RECORD=0)."
                : `At no extra cost.${rec.rows_written ? ` ${number(rec.rows_written)} rows since the server started.` : ""}`}
            </FieldDescription>
          </FieldContent>
        </Field>
      </CardContent>
    </Card>
  )
}

function Jobs({ r }: { r: CollectorsReport }) {
  return (
    <div id="jobs" className="flex flex-col gap-7">
      {r.groups.map((group) => {
        const jobs = r.jobs.filter((j) => j.group === group)
        if (!jobs.length) return null
        const gid = `group-${group.toLowerCase().replace(/[^a-z]+/g, "-")}`
        return (
          <section key={group} aria-labelledby={gid} className="flex flex-col gap-2">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h2 id={gid} className="font-medium">{group}</h2>
              {group === "Traffic and roads" && (
                <Button variant="link" size="sm" nativeButton={false} render={<a href="/#review" />} className="h-auto px-0">
                  Review the counting cameras
                  <ArrowRightIcon data-icon="inline-end" />
                </Button>
              )}
            </div>
            <ItemGroup className="gap-0 rounded-xl bg-card ring-1 ring-foreground/10">
              {jobs.map((j, i) => (
                <Fragment key={j.name}>
                  {i > 0 && <ItemSeparator className="my-0" />}
                  <JobRow job={j} report={r} />
                </Fragment>
              ))}
            </ItemGroup>
          </section>
        )
      })}
    </div>
  )
}

function Unrecorded({ r }: { r: CollectorsReport }) {
  if (!r.unrecorded.length) return null
  return (
    <section aria-labelledby="unrecorded-title" className="flex flex-col gap-2">
      <h2 id="unrecorded-title" className="font-medium">Shown live, not recorded</h2>
      <ItemGroup>
        {r.unrecorded.map((u) => (
          <Item key={u.source.id} role="listitem" variant="outline" className="items-start border-dashed">
            <ItemMedia><Dot tone="idle" /></ItemMedia>
            <ItemContent>
              <ItemTitle>{u.source.name}</ItemTitle>
              <ItemDescription className="line-clamp-none">{u.why}</ItemDescription>
              <div className="text-xs"><SourceLink source={u.source} /></div>
            </ItemContent>
          </Item>
        ))}
      </ItemGroup>
    </section>
  )
}

function RecentErrors({ r }: { r: CollectorsReport }) {
  return (
    <section aria-labelledby="errors-title" className="flex flex-col gap-2">
      <h2 id="errors-title" className="font-medium">Recent errors</h2>
      {r.recent_errors.length === 0 ? (
        <Empty className="border border-dashed py-6">
          <EmptyHeader>
            <EmptyMedia variant="icon"><CircleCheckIcon /></EmptyMedia>
            <EmptyTitle>No failed requests</EmptyTitle>
            <EmptyDescription>A failed request shows here, with the job that sent it.</EmptyDescription>
          </EmptyHeader>
        </Empty>
      ) : (
        <ItemGroup className="gap-0 rounded-xl bg-card ring-1 ring-foreground/10">
          {r.recent_errors.map((e, i) => (
            <Fragment key={`${e.at}-${i}`}>
              {i > 0 && <ItemSeparator className="my-0" />}
              <Item role="listitem" size="sm" className="items-start">
                <ItemMedia><Dot tone="error" /></ItemMedia>
                <ItemContent>
                  <ItemTitle>{e.title}<span className="font-normal text-muted-foreground" title={clockTime(e.at)}>{ago(e.at)}</span></ItemTitle>
                  <ItemDescription className="line-clamp-none break-words">{e.message}</ItemDescription>
                </ItemContent>
              </Item>
            </Fragment>
          ))}
        </ItemGroup>
      )}
    </section>
  )
}

function Loading() {
  return (
    <div aria-busy="true" aria-label="Loading the collectors" className="flex flex-col gap-6">
      <div className="grid gap-3 md:grid-cols-3">
        {[0, 1, 2].map((i) => <Skeleton key={i} className="h-44 rounded-xl" />)}
      </div>
      <Skeleton className="h-6 w-48" />
      <Skeleton className="h-72 rounded-xl" />
    </div>
  )
}

function Failed({ message }: { message: string | null }) {
  return (
    <Empty className="border border-dashed">
      <EmptyHeader>
        <EmptyMedia variant="icon"><CircleAlertIcon /></EmptyMedia>
        <EmptyTitle>The collectors couldn't be read</EmptyTitle>
        <EmptyDescription>{message ?? "The server didn't answer."} This page tries again every few seconds.</EmptyDescription>
      </EmptyHeader>
      <EmptyContent>
        <Button variant="outline" onClick={() => void loadCollectors()}>Try again now</Button>
      </EmptyContent>
    </Empty>
  )
}

export function CollectorsPage() {
  const { report, status, error } = useCollectors()

  useEffect(() => {
    document.title = "Collectors, Nicosia digital twin"
    return startPolling()
  }, [])

  return (
    <TooltipProvider delay={250}>
      <div className="h-full overflow-y-auto bg-night">
        <a
          href="#jobs"
          className="sr-only rounded-md bg-card px-3 py-2 focus:not-sr-only focus:absolute focus:top-2 focus:left-2"
        >
          Skip to the jobs
        </a>
        <header className="mx-auto flex max-w-5xl flex-col items-start gap-1 px-4 pt-5 sm:px-6">
          <Button variant="ghost" size="sm" nativeButton={false} render={<a href="/" />} className="-ml-2.5 text-muted-foreground">
            <ArrowLeftIcon data-icon="inline-start" />
            Back to the twin
          </Button>
          <div className="mt-2 flex w-full flex-wrap items-center justify-between gap-x-4 gap-y-2">
            <h1 className="text-2xl font-semibold tracking-tight">Collectors</h1>
            {report && <AdminLock r={report} />}
          </div>
          <p className="max-w-2xl text-sm text-muted-foreground">
            What the twin keeps in its memory, and how often. Changes apply within seconds.
          </p>
        </header>
        <main className="mx-auto flex max-w-5xl flex-col gap-8 px-4 pt-5 pb-16 sm:px-6">
          {status === "loading" && !report && <Loading />}
          {status === "error" && !report && <Failed message={error} />}
          {report && (
            <>
              <div className="grid gap-3 md:grid-cols-3">
                <CollectionCard r={report} />
                <BudgetCard r={report} />
                <MemoryCard r={report} />
              </div>
              <Jobs r={report} />
              <Unrecorded r={report} />
              <RecentErrors r={report} />
            </>
          )}
        </main>
        <HowSheet />
        <Toaster position="bottom-center" containerAriaLabel="Notifications" />
      </div>
    </TooltipProvider>
  )
}
