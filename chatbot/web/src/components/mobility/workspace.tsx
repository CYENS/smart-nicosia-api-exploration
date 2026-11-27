/* The Mobility workspace: what is moving in the city now. Buses on the road per line, the
   slowest roads against their free flow and the busiest counting cameras today, with the buses
   and road speeds on the map. It takes the map panel's place on the right (a sheet on a phone);
   the inspector still opens over it. */
import { BusFrontIcon, CctvIcon, MessageSquareTextIcon, RouteIcon, XIcon } from "lucide-react"
import { useEffect, useRef } from "react"

import { LinePill, Row, RowList, SpeedMeter, useRoadColors } from "@/components/mobility/parts"
import { StalledNote } from "@/components/mobility/stalled"
import { MobilitySwitches } from "@/components/mobility/switches"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Kbd } from "@/components/ui/kbd"
import { Separator } from "@/components/ui/separator"
import { Skeleton } from "@/components/ui/skeleton"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { send, useChat } from "@/lib/chat"
import { fmtMoment, goLive, useClock } from "@/lib/clock"
import { useGeometry } from "@/lib/geometry"
import { typing } from "@/lib/hooks"
import { inspect } from "@/lib/inspector"
import { GAP, STRIP_H, useLayout } from "@/lib/layout"
import { useMobility, watchSummary, type RoadRow } from "@/lib/mobility/store"
import { openWorkspace } from "@/lib/registries/workspaces"
import { cn } from "@/lib/utils"

// The row that opened the inspector: the panel steps aside for it, and on its return the
// keyboard goes back to that row (WAI-ARIA: focus returns to what opened it).
let returnTo: string | null = null

function open(key: string, target: Parameters<typeof inspect>[0]) {
  returnTo = key
  inspect(target, { fly: true, focus: true })
}

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
        <RowList label="Buses on the road per line">
          {lines.slice(0, 8).map((l) => (
            <Row key={l.id} rowKey={l.id} onSelect={() => open(l.id, { id: l.id })}
              label={`Line ${l.line}, ${l.name ?? l.operator}: ${l.buses} on the road, ${l.late ? `${l.late} late` : "on time"}`}>
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
            </Row>
          ))}
        </RowList>
      )}
    </div>
  )
}

// The detectors have stalled: say when they last sent, and the roads usually slowest at this
// hour, dotted and named as the usual, never as a speed now.
function StalledRoads({ usual }: { usual: RoadRow[] }) {
  return (
    <div className="flex flex-col gap-2.5">
      <StalledNote />
      {usual.length > 0 && (
        <RowList label="Usually slowest at this hour">
          {usual.map((x) => (
            <Row key={x.id} rowKey={x.id} className="flex-col flex-nowrap items-stretch gap-1.5" onSelect={() => open(x.id, { id: x.id })}
              label={`${x.name}: usually ${Math.round(x.usual_kmh ?? 0)} of ${x.free_flow_kmh ? Math.round(x.free_flow_kmh) : "unknown"} km/h at this hour, not live`}>
                <span className="flex w-full items-baseline gap-2">
                  <span className="size-2 shrink-0 translate-y-[-1px] rounded-full border border-muted-foreground" aria-hidden="true" />
                  <span className="min-w-0 flex-1 truncate text-[13px]">{x.name}</span>
                  <span className="shrink-0 font-readout text-[13.5px] text-muted-foreground tabular-nums">
                    usually {Math.round(x.usual_kmh ?? 0)}<span> of {x.free_flow_kmh ? Math.round(x.free_flow_kmh) : "?"} km/h</span>
                  </span>
                </span>
                <SpeedMeter speed={null} free={x.free_flow_kmh} usual={x.usual_kmh} state="no_reading" label={`${x.name}: usual speed against free flow`} />
            </Row>
          ))}
        </RowList>
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
  if (r.status === "stalled") return <StalledRoads usual={r.usually_slowest ?? []} />
  const rows = r.slowest ?? []
  if (!rows.length) return <p className="text-[13px] text-muted-foreground">No road has a reading in the last 30 minutes.</p>
  return (
    <RowList label="The slowest roads">
      {rows.map((x) => (
        <Row key={x.id} rowKey={x.id} className="flex-col flex-nowrap items-stretch gap-1.5" onSelect={() => open(x.id, { id: x.id })}
          label={`${x.name}: ${x.speed_kmh !== null ? Math.round(x.speed_kmh) : "no reading"} of ${x.free_flow_kmh ? Math.round(x.free_flow_kmh) : "unknown"} km/h free flow, ${x.state_label}`}>
            <span className="flex w-full items-baseline gap-2">
              <span className="size-2 shrink-0 translate-y-[-1px] rounded-full" style={{ background: colors[x.state] }} aria-hidden="true" />
              <span className="min-w-0 flex-1 truncate text-[13px]">{x.name}</span>
              <span className="shrink-0 font-readout text-[13.5px] tabular-nums">
                {x.speed_kmh !== null ? Math.round(x.speed_kmh) : "?"}<span className="text-muted-foreground"> of {x.free_flow_kmh ? Math.round(x.free_flow_kmh) : "?"} km/h</span>
              </span>
            </span>
            <SpeedMeter speed={x.speed_kmh} free={x.free_flow_kmh} usual={x.usual_kmh} state={x.state} label={`${x.name}: speed against free flow`} />
        </Row>
      ))}
    </RowList>
  )
}

function Cameras() {
  const summary = useMobility((s) => s.summary)
  const status = useMobility((s) => s.summaryStatus)
  const cams = summary?.cameras
  if (status !== "ready" && cams === undefined) return <Rows n={3} />
  if (!cams?.length) return <p className="text-[13px] text-muted-foreground">The cameras' counts for today aren't in yet. They come from the city's video analytics.</p>
  return (
    <RowList label="Busiest counting cameras today">
      {cams.slice(0, 5).map((c) => (
        <Row key={c.camera} rowKey={c.camera}
            onSelect={() => open(c.camera, { id: c.camera, at: c.lon !== null && c.lat !== null ? [c.lon, c.lat] : undefined })}>
            <span className="min-w-0 flex-1 truncate text-[13px]">{c.camera}</span>
            <span className="shrink-0 font-readout text-[13.5px] tabular-nums">
              {(c.people ?? 0).toLocaleString("en-GB")} <span className="text-muted-foreground">people, {(c.car ?? 0).toLocaleString("en-GB")} cars</span>
            </span>
        </Row>
      ))}
    </RowList>
  )
}

// Back to Live, with the keyboard handed to the rail's Mobility button it came from (or the map).
function leave() {
  const inside = document.activeElement?.closest("[data-workspace]")
  openWorkspace("live")
  if (!inside) return
  const back = document.querySelector<HTMLElement>('nav[aria-label="Workspaces"] [data-workspace-id="mobility"]')
  ;(back ?? document.querySelector<HTMLElement>(".maplibregl-canvas"))?.focus({ preventScroll: true })
}

export function MobilityPanel() {
  const g = useGeometry()
  const busy = useChat((s) => s.busy)
  const clockAt = useClock((s) => s.at)
  const title = useRef<HTMLHeadingElement>(null)
  useEffect(() => watchSummary(), [])
  // Opened from the rail or a shortcut, the keyboard comes into the panel at its title.
  useEffect(() => { title.current?.focus({ preventScroll: true }) }, [])
  // Back from the inspector (the panel stays mounted, drawing nothing, while it is open), the
  // keyboard goes to the row that opened it.
  useEffect(() => {
    if (g.inspecting || !returnTo) return
    const key = returnTo
    returnTo = null
    document.querySelector<HTMLElement>(`[data-workspace] [data-row][data-key="${CSS.escape(key)}"]`)?.focus({ preventScroll: true })
  }, [g.inspecting])
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !typing(e) && !g.inspecting && document.activeElement?.closest("[data-workspace]")) leave()
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
          <h2 ref={title} tabIndex={-1} id="mobility-title" className="text-[14.5px] font-semibold outline-none">Moving now</h2>
          <p className="truncate text-[12.5px] text-muted-foreground">Buses, roads and counts across the city</p>
        </div>
        <Tooltip>
          <TooltipTrigger render={<Button variant="ghost" size="icon-sm" aria-label="Back to Live" onClick={leave} />}>
            <XIcon />
          </TooltipTrigger>
          <TooltipContent>Back to Live <Kbd>Alt 1</Kbd></TooltipContent>
        </Tooltip>
      </div>
      <div className="flex min-h-0 flex-col gap-4 overflow-y-auto p-4">
        {clockAt !== null && (
          <p className="rounded-lg border border-line bg-night/50 px-3 py-2 text-[12.5px] leading-snug text-muted-foreground" aria-live="polite">
            The clock shows {fmtMoment(clockAt, { weekday: true })}. The road speeds on the map are from then; the buses
            and the figures below are now.{" "}
            <button type="button" className="text-foreground underline underline-offset-3 hover:text-sodium" onClick={goLive}>Back to now</button>
          </p>
        )}
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
