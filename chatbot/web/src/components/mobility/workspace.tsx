/* The Mobility workspace: what is moving in the city now. Buses on the road per line, the
   slowest roads against their free flow and the busiest counting cameras today, with the buses
   and road speeds on the map. It takes the map panel's place on the right (a sheet on a phone);
   the inspector still opens over it. */
import { BusFrontIcon, CctvIcon, MessageSquareTextIcon, RouteIcon, XIcon } from "lucide-react"
import { useEffect } from "react"

import { LinePill, SpeedMeter, useRoadColors } from "@/components/mobility/parts"
import { MobilitySwitches } from "@/components/mobility/switches"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Kbd } from "@/components/ui/kbd"
import { Separator } from "@/components/ui/separator"
import { Skeleton } from "@/components/ui/skeleton"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { send, useChat } from "@/lib/chat"
import { useGeometry } from "@/lib/geometry"
import { typing } from "@/lib/hooks"
import { inspect } from "@/lib/inspector"
import { GAP, STRIP_H, useLayout } from "@/lib/layout"
import { useMobility, watchSummary } from "@/lib/mobility/store"
import { openWorkspace } from "@/lib/registries/workspaces"
import { cn } from "@/lib/utils"

const QUESTIONS = [
  "Where are the buses right now?",
  "Which roads are slow right now?",
  "Can I take the bus from Eleftheria Square to the Cyprus Museum?",
]

function Heading({ icon: Icon, title, children }: { icon: React.ComponentType<{ className?: string }>; title: string; children?: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-2">
      <h3 className="flex items-center gap-1.5 text-[13px] font-semibold"><Icon className="size-4 text-muted-foreground" />{title}</h3>
      {children}
    </div>
  )
}

function Rows({ n = 4 }: { n?: number }) {
  return (
    <div className="flex flex-col gap-2" aria-label="Loading">
      {Array.from({ length: n }, (_, i) => <Skeleton key={i} className="h-7 w-full" />)}
    </div>
  )
}

const rowClass = "flex w-full items-center gap-2.5 rounded-md px-1.5 py-1.5 text-left outline-none hover:bg-accent/50 focus-visible:ring-3 focus-visible:ring-ring/50"

function Buses() {
  const summary = useMobility((s) => s.summary)
  const status = useMobility((s) => s.summaryStatus)
  const b = summary?.buses
  if (status !== "ready" && !b) return <Rows />
  if (!b || b.status === "unavailable") {
    return (
      <Alert>
        <AlertTitle>The live bus feed isn't answering</AlertTitle>
        <AlertDescription>It is a national feed and sometimes pauses. The stops' timetables still work; this panel tries again every 30 seconds.</AlertDescription>
      </Alert>
    )
  }
  const lines = b.by_line ?? []
  const most = Math.max(1, ...lines.map((l) => l.buses))
  return (
    <div className="flex flex-col gap-2">
      <p className="font-readout text-[15px]" aria-live="polite">
        <span className="text-[26px] font-semibold text-sodium">{b.running}</span>{" "}
        <span className="text-muted-foreground">on the road, {b.lines} lines{b.late ? `, ${b.late} running 3 min late or more` : ""}</span>
      </p>
      {lines.length === 0 ? (
        <p className="text-[13px] text-muted-foreground">No buses are running just now. Night lines are few; the first buses leave before 06:00.</p>
      ) : (
        <ul className="-mx-1.5 flex flex-col" aria-label="Buses on the road per line">
          {lines.slice(0, 8).map((l) => (
            <li key={l.id}>
              <button type="button" className={rowClass} onClick={() => inspect({ id: l.id }, { fly: true, focus: true })}>
                <LinePill line={l.line} color={l.color} />
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-[13px]">{l.name ?? l.operator}</span>
                  <span className="mt-1 block h-1 rounded-full bg-raised" aria-hidden="true">
                    <span className="block h-1 rounded-full" style={{ width: `${(100 * l.buses) / most}%`, background: l.color }} />
                  </span>
                </span>
                <span className="flex w-12 shrink-0 flex-col items-end leading-tight">
                  <span className="font-readout text-[14px] font-semibold tabular-nums">{l.buses}</span>
                  <span className="font-condensed text-[11.5px] text-muted-foreground">{l.late ? `${l.late} late` : "on time"}</span>
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

function Roads() {
  const summary = useMobility((s) => s.summary)
  const status = useMobility((s) => s.summaryStatus)
  const colors = useRoadColors()
  const r = summary?.roads
  if (status !== "ready" && !r) return <Rows n={3} />
  if (!r || r.status === "unavailable") {
    return <p className="text-[13px] text-muted-foreground">The national traffic feed isn't answering. Road speeds come back when it does.</p>
  }
  const rows = r.slowest ?? []
  if (!rows.length) return <p className="text-[13px] text-muted-foreground">No road has a reading in the last 30 minutes.</p>
  return (
    <ul className="-mx-1.5 flex flex-col" aria-label="The slowest roads">
      {rows.map((x) => (
        <li key={x.id}>
          <button type="button" className={cn(rowClass, "flex-col items-stretch gap-1.5")} onClick={() => inspect({ id: x.id }, { fly: true, focus: true })}>
            <span className="flex items-baseline gap-2">
              <span className="size-2 shrink-0 translate-y-[-1px] rounded-full" style={{ background: colors[x.state] }} aria-hidden="true" />
              <span className="min-w-0 flex-1 truncate text-[13px]">{x.name}</span>
              <span className="shrink-0 font-readout text-[13.5px] tabular-nums">
                {x.speed_kmh !== null ? Math.round(x.speed_kmh) : "?"}<span className="text-muted-foreground"> of {x.free_flow_kmh ? Math.round(x.free_flow_kmh) : "?"} km/h</span>
              </span>
            </span>
            <SpeedMeter speed={x.speed_kmh} free={x.free_flow_kmh} usual={x.usual_kmh} state={x.state} label={`${x.name}: speed against free flow`} />
          </button>
        </li>
      ))}
    </ul>
  )
}

function Cameras() {
  const summary = useMobility((s) => s.summary)
  const status = useMobility((s) => s.summaryStatus)
  const cams = summary?.cameras
  if (status !== "ready" && cams === undefined) return <Rows n={3} />
  if (!cams?.length) return <p className="text-[13px] text-muted-foreground">The cameras' counts for today aren't in yet. They come from the city's video analytics.</p>
  return (
    <ul className="-mx-1.5 flex flex-col" aria-label="Busiest counting cameras today">
      {cams.slice(0, 5).map((c) => (
        <li key={c.camera}>
          <button type="button" className={rowClass}
            onClick={() => inspect({ id: c.camera, at: c.lon !== null && c.lat !== null ? [c.lon, c.lat] : undefined }, { fly: true, focus: true })}>
            <span className="min-w-0 flex-1 truncate text-[13px]">{c.camera}</span>
            <span className="shrink-0 font-readout text-[13.5px] tabular-nums">
              {(c.people ?? 0).toLocaleString("en-GB")} <span className="text-muted-foreground">people, {(c.car ?? 0).toLocaleString("en-GB")} cars</span>
            </span>
          </button>
        </li>
      ))}
    </ul>
  )
}

export function MobilityPanel() {
  const g = useGeometry()
  const busy = useChat((s) => s.busy)
  useEffect(() => watchSummary(), [])
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !typing(e) && !g.inspecting && document.activeElement?.closest("[data-workspace]")) openWorkspace("live")
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [g.inspecting])

  if (g.inspecting) return null // the inspector takes the column (or the sheet) while open
  const style = g.desktop
    ? { top: STRIP_H + GAP, right: GAP, width: g.workspaceW, maxHeight: `calc(100% - ${STRIP_H + 2 * GAP + g.layersBottom}px)` }
    : { left: 8, right: 8, bottom: 8, height: g.sheetH }

  return (
    <aside data-workspace aria-labelledby="mobility-title" className={cn("panel-float absolute flex flex-col overflow-hidden rounded-xl", g.desktop ? "z-10" : "z-20")} style={style}>
      <div className="flex h-14 shrink-0 items-center gap-3 border-b border-line pr-2 pl-4">
        <BusFrontIcon className="size-[18px] text-sodium" aria-hidden="true" />
        <div className="min-w-0 flex-1 leading-tight">
          <h2 id="mobility-title" className="text-[14.5px] font-semibold">Moving now</h2>
          <p className="truncate text-[12.5px] text-muted-foreground">Buses, roads and counts across the city</p>
        </div>
        <Tooltip>
          <TooltipTrigger render={<Button variant="ghost" size="icon-sm" aria-label="Back to Live" onClick={() => openWorkspace("live")} />}>
            <XIcon />
          </TooltipTrigger>
          <TooltipContent>Back to Live <Kbd>Alt 1</Kbd></TooltipContent>
        </Tooltip>
      </div>
      <div className="flex min-h-0 flex-col gap-4 overflow-y-auto p-4">
        <div className="flex flex-col gap-4"><MobilitySwitches idPrefix="ws" /></div>
        <Separator />
        <section aria-label="Buses" className="flex flex-col gap-2.5">
          <Heading icon={BusFrontIcon} title="Buses by line" />
          <Buses />
        </section>
        <Separator />
        <section aria-label="Slowest roads" className="flex flex-col gap-2.5">
          <Heading icon={RouteIcon} title="Slowest roads">
            <span className="text-[12px] text-muted-foreground">against free flow</span>
          </Heading>
          <Roads />
        </section>
        <Separator />
        <section aria-label="Busiest cameras today" className="flex flex-col gap-2.5">
          <Heading icon={CctvIcon} title="Busiest cameras today" />
          <Cameras />
        </section>
        <Separator />
        <section aria-label="Ask" className="flex flex-col gap-1.5">
          <h3 className="text-[12.5px] text-muted-foreground">Ask about it</h3>
          {QUESTIONS.map((q) => (
            <Button key={q} variant="ghost" size="sm" disabled={busy} className="h-auto justify-start py-1.5 text-left whitespace-normal"
              onClick={() => {
                useLayout.setState({ chatOpen: true })
                if (!g.desktop) openWorkspace("live") // a phone has one sheet: the conversation takes it
                send(q)
              }}>
              <MessageSquareTextIcon data-icon="inline-start" />{q}
            </Button>
          ))}
        </section>
      </div>
    </aside>
  )
}
