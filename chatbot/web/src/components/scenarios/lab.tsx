/* The Scenario lab workspace: try a change on the twin before making it on the street. Three
   tabs: Set up (the scenario, the period it learns from, the policy), Results (the KPI cards with
   their ranges, the split map's view, the charts, the assumptions and how much each moves the
   result, the backtest, the scenario's own sections, the limits, and the brief), and Saved runs.
   It takes the right column (a sheet on a phone); the inspector still opens over it. Esc or the
   close button goes back to Live and gives the keyboard back to whatever opened the lab. */
import { DownloadIcon, FileTextIcon, FlaskConicalIcon, LinkIcon, PlayIcon, RotateCcwIcon, XIcon } from "lucide-react"
import { createElement, useEffect, useRef, useState } from "react"

import {
  Assumptions, Backtest, Charts, CompareSelect, KpiCards, MapViewControls, Notes, ParamForm, SavedRuns, Tornado,
} from "@/components/scenarios/parts"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button, buttonVariants } from "@/components/ui/button"
import { Empty, EmptyDescription, EmptyHeader, EmptyTitle } from "@/components/ui/empty"
import { Field, FieldLabel } from "@/components/ui/field"
import { Kbd } from "@/components/ui/kbd"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Separator } from "@/components/ui/separator"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { useGeometry } from "@/lib/geometry"
import { typing } from "@/lib/hooks"
import { GAP, STRIP_H } from "@/lib/layout"
import { scenarioSections } from "@/lib/registries/scenarios"
import { openWorkspace } from "@/lib/registries/workspaces"
import {
  briefUrl, csvUrl, defOf, loadSaved, loadScenarios, resetValues, runScenario, setPeriod, setScenario,
  useLab, type LabTab,
} from "@/lib/scenarios/store"
import type { ScenarioRun } from "@/lib/registries/scenarios"
import { cn } from "@/lib/utils"

// What had the keyboard when the lab opened: leaving gives it back.
let opener: HTMLElement | null = null
export function rememberLabOpener() {
  const a = document.activeElement
  opener = a instanceof HTMLElement && a !== document.body ? a : null
}

function leave() {
  const inside = document.activeElement?.closest("[data-workspace]")
  openWorkspace("live")
  if (!inside) return
  const back = opener && document.contains(opener) ? opener : document.querySelector<HTMLElement>(".maplibregl-canvas")
  opener = null
  back?.focus({ preventScroll: true })
}

function Section({ title, children, id }: { title: string; children: React.ReactNode; id: string }) {
  return (
    <section aria-labelledby={id} className="flex flex-col gap-2.5">
      <h3 id={id} className="text-[13px] font-semibold">{title}</h3>
      {children}
    </section>
  )
}

function SetUp() {
  const defs = useLab((s) => s.defs)
  const periods = useLab((s) => s.periods)
  const scenario = useLab((s) => s.scenario)
  const period = useLab((s) => s.period)
  const values = useLab((s) => s.values[s.scenario] ?? {})
  const running = useLab((s) => s.running)
  const runError = useLab((s) => s.runError)
  const def = defOf(scenario)
  if (!def) return null
  return (
    <div className="flex flex-col gap-4">
      {defs.length > 1 && (
        <Field className="gap-1.5">
          <FieldLabel htmlFor="lab-scenario" className="text-[12.5px] font-normal text-muted-foreground">Scenario</FieldLabel>
          <Select value={scenario} onValueChange={(v) => setScenario(String(v))}>
            <SelectTrigger id="lab-scenario" size="sm" className="w-full"><SelectValue>{def.title}</SelectValue></SelectTrigger>
            <SelectContent>{defs.map((d) => <SelectItem key={d.id} value={d.id}>{d.title}</SelectItem>)}</SelectContent>
          </Select>
        </Field>
      )}
      <p className="text-[13px] leading-snug text-muted-foreground">{def.summary}</p>
      <Field className="gap-1.5">
        <FieldLabel htmlFor="lab-period" className="text-[12.5px] font-normal text-muted-foreground">Learn today's policy from</FieldLabel>
        <Select value={period} onValueChange={(v) => setPeriod(String(v))}>
          <SelectTrigger id="lab-period" size="sm" className="w-full">
            <SelectValue>{periods.find((p) => p.name === period)?.label ?? period}</SelectValue>
          </SelectTrigger>
          <SelectContent>{periods.map((p) => <SelectItem key={p.name} value={p.name}>{p.label}</SelectItem>)}</SelectContent>
        </Select>
      </Field>
      <ParamForm def={def} values={values} />
      {runError && (
        <Alert variant="destructive">
          <AlertTitle>The run didn't work</AlertTitle>
          <AlertDescription>{runError}</AlertDescription>
        </Alert>
      )}
      <div className="flex items-center gap-2">
        <Button className="flex-1 bg-sodium text-night hover:bg-sodium/90" onClick={() => void runScenario()} disabled={running}>
          {running ? <Spinner data-icon="inline-start" /> : <PlayIcon data-icon="inline-start" />}
          {running ? "Running on the twin's memory" : "Run"}
        </Button>
        <Tooltip>
          <TooltipTrigger render={<Button variant="outline" size="icon" aria-label="Back to today's policy" onClick={resetValues} disabled={running} />}>
            <RotateCcwIcon />
          </TooltipTrigger>
          <TooltipContent>Back to today's policy</TooltipContent>
        </Tooltip>
      </div>
      <p className="text-[12px] leading-snug text-muted-foreground">
        The assumptions (price, CO2 and the like) are on the results, with how much each moves the result.
      </p>
    </div>
  )
}

// Two runs compared with different assumptions: say which, since part of the gap comes from them.
function Differences({ run, compare }: { run: ScenarioRun; compare: ScenarioRun }) {
  const rows = run.result.assumptions
    .filter((a) => run.params[a.id] !== compare.params[a.id])
    .map((a) => `${a.label} ${String(run.params[a.id])} against ${String(compare.params[a.id])} ${a.unit}`)
  if (!rows.length) return null
  return <p className="text-[12.5px] leading-snug text-sodium">The runs differ in their assumptions too: {rows.join("; ")}.</p>
}

function Results({ toSetUp }: { toSetUp: () => void }) {
  const run = useLab((s) => s.run)
  const compare = useLab((s) => s.compare)
  const values = useLab((s) => (s.run ? s.values[s.run.scenario] ?? {} : {}))
  const running = useLab((s) => s.running)
  const [copied, setCopied] = useState(false)
  if (!run) {
    return (
      <Empty className="border border-dashed border-line py-8">
        <EmptyHeader>
          <EmptyTitle className="text-[14px]">No run open</EmptyTitle>
          <EmptyDescription className="text-[13px]">Set a policy and run it, or open a saved run.</EmptyDescription>
        </EmptyHeader>
        <Button variant="outline" size="sm" onClick={toSetUp}>Set up a run</Button>
      </Empty>
    )
  }
  const def = defOf(run.scenario)
  const sections = scenarioSections(run.scenario)
  const link = `${location.origin}${location.pathname}#/s/${run.id}${compare ? `+${compare.id}` : ""}`
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-col gap-1">
        <p className="text-[14px] leading-snug font-semibold">{compare ? `A: ${run.label}` : run.label}</p>
        {compare && <p className="text-[14px] leading-snug font-semibold text-[#6cb4ff]">B: {compare.label}</p>}
        {compare && <Differences run={run} compare={compare} />}
        <p className="text-[12.5px] text-muted-foreground">
          Against today's policy, learnt from {run.period.label.toLowerCase()}. Ranges span the assumptions' plausible values.
          {!run.saved && " This run isn't saved: the twin's memory couldn't be written."}
        </p>
      </div>
      {running && <p className="flex items-center gap-2 text-[12.5px] text-muted-foreground" aria-live="polite"><Spinner className="size-3.5" />Running again with the new assumption</p>}
      <KpiCards run={run} compare={compare} />
      <Separator />
      <Section title="On the map" id="lab-map"><MapViewControls run={run} compare={compare} /></Section>
      <Separator />
      <Section title="Over time" id="lab-charts"><Charts run={run} compare={compare} /></Section>
      <Separator />
      <Section title="Assumptions" id="lab-assumptions">
        <Tornado run={run} />
        {def && <Assumptions def={def} run={run} values={values} onCommit={() => void runScenario({}, run.scenario)} />}
      </Section>
      <Separator />
      <Section title="Backtest" id="lab-backtest"><Backtest run={run} /></Section>
      {sections.map((s) => (
        <div key={s.id} className="flex flex-col gap-4">
          <Separator />
          <Section title={s.title} id={`lab-sec-${s.id}`}>{createElement(s.Section, { run, compare })}</Section>
        </div>
      ))}
      <Separator />
      <Section title="What the model can't know" id="lab-notes"><Notes run={run} /></Section>
      <Separator />
      <Section title="Compare and share" id="lab-share">
        <CompareSelect run={run} />
        <div className="flex flex-wrap gap-2">
          <a className={buttonVariants({ variant: "outline", size: "sm" })} href={briefUrl(run, compare)} target="_blank" rel="noopener">
            <FileTextIcon data-icon="inline-start" />Decision brief
          </a>
          <a className={buttonVariants({ variant: "outline", size: "sm" })} href={csvUrl(run)} download>
            <DownloadIcon data-icon="inline-start" />Data (CSV)
          </a>
          <Button variant="outline" size="sm" onClick={() => {
            void navigator.clipboard?.writeText(link).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1800) })
          }}>
            <LinkIcon data-icon="inline-start" />{copied ? "Link copied" : "Copy link"}
          </Button>
        </div>
        <span className="sr-only" aria-live="polite">{copied ? "Link copied" : ""}</span>
      </Section>
    </div>
  )
}

export function ScenarioLab() {
  const g = useGeometry()
  const status = useLab((s) => s.status)
  const loadError = useLab((s) => s.loadError)
  const tab = useLab((s) => s.tab)
  const opening = useLab((s) => s.opening)
  const title = useRef<HTMLHeadingElement>(null)
  useEffect(() => { void loadScenarios(); void loadSaved() }, [])
  useEffect(() => { title.current?.focus({ preventScroll: true }) }, [])
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !typing(e) && !g.inspecting && document.activeElement?.closest("[data-workspace]")
        && !document.activeElement?.closest("[role=listbox],[role=slider]")) leave()
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [g.inspecting])

  if (g.inspecting) return null
  const style = g.desktop
    ? { top: STRIP_H + GAP, right: GAP, width: g.workspaceW, maxHeight: `calc(100% - ${STRIP_H + 2 * GAP + g.layersBottom}px)` }
    : { left: 8, right: 8, bottom: 8, height: g.sheetH }
  const setTab = (t: LabTab) => useLab.setState({ tab: t })

  return (
    <aside data-workspace aria-labelledby="lab-title" className={cn("panel-float absolute flex flex-col overflow-hidden rounded-xl", g.desktop ? "z-10" : "z-20")} style={style}>
      <div className="flex h-14 shrink-0 items-center gap-3 border-b border-line pr-2 pl-4">
        <FlaskConicalIcon className="size-[18px] text-sodium" aria-hidden="true" />
        <div className="min-w-0 flex-1 leading-tight">
          <h2 ref={title} tabIndex={-1} id="lab-title" className="text-[14.5px] font-semibold outline-none">Scenario lab</h2>
          <p className="truncate text-[12.5px] text-muted-foreground">Try a change before making it on the street</p>
        </div>
        <Tooltip>
          <TooltipTrigger render={<Button variant="ghost" size="icon-sm" aria-label="Close the Scenario lab" onClick={leave} />}>
            <XIcon />
          </TooltipTrigger>
          <TooltipContent>Back to Live <Kbd>Esc</Kbd></TooltipContent>
        </Tooltip>
      </div>
      {status === "error" ? (
        <div className="p-4">
          <Alert variant="destructive">
            <AlertTitle>The lab can't load its scenarios</AlertTitle>
            <AlertDescription>{loadError}. Check that the server is running, then open the lab again.</AlertDescription>
          </Alert>
        </div>
      ) : status !== "ready" ? (
        <div className="flex flex-col gap-3 p-4" aria-label="Loading the scenarios">
          <Skeleton className="h-8 w-full" /><Skeleton className="h-24 w-full" /><Skeleton className="h-24 w-full" />
        </div>
      ) : (
        <Tabs value={tab} onValueChange={(v) => setTab(v as LabTab)} className="flex min-h-0 flex-1 flex-col gap-0">
          <TabsList className="mx-4 mt-3 w-[calc(100%-2rem)] shrink-0">
            <TabsTrigger value="setup">Set up</TabsTrigger>
            <TabsTrigger value="results">Results</TabsTrigger>
            <TabsTrigger value="saved">Saved runs</TabsTrigger>
          </TabsList>
          {opening && <p className="flex items-center gap-2 px-4 pt-3 text-[12.5px] text-muted-foreground" aria-live="polite"><Spinner className="size-3.5" />Opening the run</p>}
          <TabsContent value="setup" className="min-h-0 overflow-y-auto p-4"><SetUp /></TabsContent>
          <TabsContent value="results" className="min-h-0 overflow-y-auto p-4"><Results toSetUp={() => setTab("setup")} /></TabsContent>
          <TabsContent value="saved" className="min-h-0 overflow-y-auto p-4"><SavedRuns /></TabsContent>
        </Tabs>
      )}
    </aside>
  )
}
