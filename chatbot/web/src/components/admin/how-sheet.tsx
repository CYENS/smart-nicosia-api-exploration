/* "How it's collected": one job in depth, in a side sheet opened by a row's Read more (or a
   /admin#job=<name> link). Two layers. First, plain words from collector/catalog.py: how it is
   collected, what it records, what it's for, where it comes from. Then the technical side, read
   by the server when the sheet opens, never typed twice: the job's own code (its docstring, file
   and line, linked to the running commit), the API calls, the cost and why, the settings in
   force, the tables it writes and its last runs. A test keeps what each job declares in step
   with what it really does (tests/test_collectors.py). */
import { ExternalLinkIcon } from "lucide-react"
import { useEffect, useState } from "react"

import { Dot, useJobControls, type Tone } from "@/components/admin/job-row"
import { SourceLink } from "@/components/admin/source-link"
import { Accordion, AccordionContent, AccordionItem, AccordionTrigger } from "@/components/ui/accordion"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Field, FieldContent, FieldDescription, FieldLabel } from "@/components/ui/field"
import { Separator } from "@/components/ui/separator"
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet"
import { Skeleton } from "@/components/ui/skeleton"
import { Switch } from "@/components/ui/switch"
import {
  ago, cadenceLabel, clockTime, closeHow, loadHow, number, useCollectors, useHow, type CollectorJob,
  type CollectorsReport, type HowItsCollected,
} from "@/lib/collectors"

// A docstring's wrapped lines read as paragraphs.
function paragraphs(text: string): string[] {
  return text.split(/\n\s*\n/).map((p) => p.replace(/\s*\n\s*/g, " ").trim()).filter(Boolean)
}

function shown(key: string, value: unknown): string {
  if (key === "every_minutes" && typeof value === "number") return cadenceLabel(value)
  if (typeof value === "boolean") return value ? "Yes" : "No"
  if (Array.isArray(value)) return value.join(", ")
  if (typeof value === "number") return number(value)
  return String(value ?? "")
}

const RUN_TONE: Record<string, Tone> = { ok: "ok", partial: "error", error: "error" }

function day(iso: string | null): string {
  return iso ? new Date(iso).toLocaleDateString("en-GB", { timeZone: "Asia/Nicosia", day: "numeric", month: "short", year: "numeric" }) : ""
}

function NightOnly({ job, report }: { job: CollectorJob; report: CollectorsReport }) {
  const { locked, change, dialog } = useJobControls(job, report)
  const id = `how-${job.name}-night`
  return (
    <Field orientation="horizontal" data-disabled={locked || undefined}>
      <Switch
        id={id}
        size="sm"
        checked={job.night_only}
        disabled={locked}
        onCheckedChange={(on) => change({ night_only: on }, on ? `${job.title} runs only after dark` : `${job.title} runs day and night`)}
      />
      <FieldContent>
        <FieldLabel htmlFor={id}>Only after dark</FieldLabel>
        <FieldDescription>For devices that report only while powered, such as street lights.</FieldDescription>
      </FieldContent>
      {dialog}
    </Field>
  )
}

function Technical({ h, job, report }: { h: HowItsCollected; job: CollectorJob | undefined; report: CollectorsReport | null }) {
  return (
    <Accordion defaultValue={["code"]} multiple>
      <AccordionItem value="code">
        <AccordionTrigger>In the code</AccordionTrigger>
        <AccordionContent className="flex flex-col gap-3">
          {h.doc && paragraphs(h.doc).map((p, i) => <p key={i}>{p}</p>)}
          <p className="text-muted-foreground">
            {h.code.path}, line {h.code.line}{h.code.commit ? `, at commit ${h.code.commit}` : ""}.
            {h.code.changed_since_commit && " This file has changed since that commit, so the link may show other lines."}
            {h.code.on_github === false && " That commit isn't on GitHub yet, so the link may not open."}
          </p>
          <div>
            <Button variant="outline" size="sm" nativeButton={false} render={<a href={h.code.url} target="_blank" rel="noreferrer" />}>
              View the code
              <ExternalLinkIcon data-icon="inline-end" />
              <span className="sr-only">(opens in a new tab)</span>
            </Button>
          </div>
        </AccordionContent>
      </AccordionItem>
      <AccordionItem value="calls">
        <AccordionTrigger>What it asks for</AccordionTrigger>
        <AccordionContent className="flex flex-col gap-3">
          <ul className="flex flex-col gap-1.5">
            {h.calls.map((c) => (
              <li key={c.endpoint} className="flex items-start gap-2">
                <Badge variant="outline">{c.method}</Badge>
                <span className="min-w-0 break-all">{c.url}</span>
              </li>
            ))}
          </ul>
          <p>
            About {number(h.cost.per_run)} {h.cost.per_run === 1 ? "request" : "requests"} a run and {number(h.cost.per_day)} a day
            {h.cost.counts_against_budget ? ", counted in the city's budget." : ", outside the city's budget."} {h.cost.why}
          </p>
        </AccordionContent>
      </AccordionItem>
      <AccordionItem value="settings">
        <AccordionTrigger>Settings</AccordionTrigger>
        <AccordionContent className="flex flex-col gap-4">
          <dl className="grid grid-cols-[minmax(0,1fr)_auto] gap-x-4 gap-y-1.5">
            {h.settings.filter((s) => s.key !== "night_only").map((s) => (
              <div key={s.key} className="contents">
                <dt className="text-muted-foreground">{s.label}</dt>
                <dd className="text-right font-readout">
                  {shown(s.key, s.value)}
                  {s.default != null && s.default !== s.value && (
                    <span className="text-muted-foreground"> (default {shown(s.key, s.default)})</span>
                  )}
                </dd>
              </div>
            ))}
          </dl>
          {job && report && <NightOnly job={job} report={report} />}
          <p className="text-muted-foreground">
            The rest are set in collector.toml, beside the code.
          </p>
        </AccordionContent>
      </AccordionItem>
      <AccordionItem value="tables">
        <AccordionTrigger>Where it's kept</AccordionTrigger>
        <AccordionContent className="flex flex-col gap-4">
          {h.store_note && <p className="text-muted-foreground">{h.store_note.replace(/^./, (c) => c.toUpperCase())}.</p>}
          {h.tables.map((t) => (
            <div key={t.name} className="flex flex-col gap-1">
              <p>
                <span className="font-medium">{t.name}</span>
                <span className="text-muted-foreground">
                  {", "}{number(t.rows)} rows{t.oldest ? `, from ${day(t.oldest)} to ${day(t.newest)}` : ""}
                </span>
              </p>
              {t.shared_with.length > 0 && <p className="text-muted-foreground">Shared with {t.shared_with.join(", ")}.</p>}
              <p className="text-xs text-muted-foreground">
                {t.columns.map((c) => `${c.name} (${c.type.toLowerCase()})`).join(", ")}
              </p>
            </div>
          ))}
        </AccordionContent>
      </AccordionItem>
      <AccordionItem value="runs">
        <AccordionTrigger>Last runs</AccordionTrigger>
        <AccordionContent>
          {h.runs.length === 0 ? (
            <p className="text-muted-foreground">It hasn't run on this memory yet.</p>
          ) : (
            <ul className="flex flex-col gap-2">
              {h.runs.map((r) => (
                <li key={r.started} className="flex flex-col gap-0.5">
                  <span className="flex items-center gap-2">
                    <Dot tone={RUN_TONE[r.status] ?? "idle"} />
                    <span title={clockTime(r.started)}>{ago(r.started)}</span>
                    <span className="text-muted-foreground">
                      {[r.seconds != null && `${r.seconds} s`, r.requests != null && `${number(r.requests)} requests`,
                        r.rows != null && `${number(r.rows)} rows`].filter(Boolean).join(", ")}
                    </span>
                  </span>
                  {r.message && <span className="pl-4 text-xs break-words text-muted-foreground">{r.message}</span>}
                </li>
              ))}
            </ul>
          )}
        </AccordionContent>
      </AccordionItem>
    </Accordion>
  )
}

export function HowSheet() {
  const name = useHow((s) => s.name)
  const report = useCollectors((s) => s.report)
  const job = report?.jobs.find((j) => j.name === name)
  const [h, setH] = useState<HowItsCollected | null>(null)
  const [error, setError] = useState<string | null>(null)

  // A /admin#job=<name> link opens its sheet, on load and when the address changes after.
  useEffect(() => {
    const follow = () => {
      const m = /^#job=([a-z_]+)$/.exec(location.hash)
      useHow.setState({ name: m ? m[1] : null })
    }
    follow()
    window.addEventListener("hashchange", follow)
    return () => window.removeEventListener("hashchange", follow)
  }, [])

  // Read afresh each time it opens, and again when the job's settings or last run change.
  const stamp = job ? `${job.every_minutes}|${job.enabled}|${job.night_only}|${job.last?.started ?? ""}` : ""
  useEffect(() => {
    if (!name) return
    let live = true
    loadHow(name)
      .then((d) => { if (live) { setH(d); setError(null) } })
      .catch((e: Error) => { if (live) setError(e.message) })
    return () => { live = false }
  }, [name, stamp])

  const current = h && h.name === name ? h : null

  return (
    <Sheet open={name !== null} onOpenChange={(o) => { if (!o) closeHow() }}>
      <SheetContent className="gap-0 data-[side=right]:w-full data-[side=right]:sm:max-w-xl">
        <SheetHeader className="border-b">
          <SheetTitle>{job?.title ?? current?.title ?? "How it's collected"}</SheetTitle>
          <SheetDescription>How it's collected</SheetDescription>
        </SheetHeader>
        <div className="flex min-h-0 flex-1 flex-col gap-5 overflow-y-auto p-4">
          {error && !current && (
            <Alert variant="destructive"><AlertDescription>{error}</AlertDescription></Alert>
          )}
          {!current && !error && (
            <div className="flex flex-col gap-3" aria-busy="true" aria-label="Loading">
              <Skeleton className="h-16" />
              <Skeleton className="h-8 w-2/3" />
              <Skeleton className="h-40" />
            </div>
          )}
          {current && (
            <>
              <section className="flex flex-col gap-3" aria-label="In plain words">
                <p className="text-base leading-relaxed">{current.how}</p>
                <dl className="grid gap-x-4 gap-y-1.5 sm:grid-cols-[8rem_1fr]">
                  <dt className="text-muted-foreground">What it records</dt>
                  <dd>{current.records}</dd>
                  <dt className="text-muted-foreground">Used for</dt>
                  <dd>{current.uses}</dd>
                </dl>
                <div className="text-xs"><SourceLink source={current.source} /></div>
              </section>
              <Separator />
              <section className="flex flex-col gap-1" aria-labelledby="how-technical">
                <h3 id="how-technical" className="font-medium">Technical</h3>
                <p className="text-xs text-muted-foreground">Read from the code, the settings and the memory as this opened.</p>
                <Technical h={current} job={job} report={report} />
              </section>
            </>
          )}
        </div>
      </SheetContent>
    </Sheet>
  )
}
