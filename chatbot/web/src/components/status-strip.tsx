import { HistoryIcon, RadioIcon, SunIcon } from "lucide-react"
import { useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react"

import { MoonGlyph } from "@/components/moon-glyph"
import { Button } from "@/components/ui/button"
import { Kbd } from "@/components/ui/kbd"
import { Skeleton } from "@/components/ui/skeleton"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { WallsMark } from "@/components/walls-mark"
import { send, useChat } from "@/lib/chat"
import { compareAt, compareName, fmtMoment, focusClock, goLive, useClock } from "@/lib/clock"
import { nicosiaTime } from "@/lib/map/engine"
import { shortName, useSources } from "@/lib/sources"
import { moonName, skyAt } from "@/lib/map/sky"
import { cn } from "@/lib/utils"

const AIR_WORDS: Record<string, string> = {
  very_low: "Very good", low: "Good", medium: "Moderate", high: "Poor", very_high: "Very poor",
}

function useNicosiaClock() {
  const read = () => {
    const now = new Date()
    return {
      time: now.toLocaleTimeString("en-GB", { timeZone: "Asia/Nicosia", hour: "2-digit", minute: "2-digit" }),
      day: now.toLocaleDateString("en-GB", { timeZone: "Asia/Nicosia", weekday: "short", day: "numeric", month: "short" }),
    }
  }
  const [clock, setClock] = useState(read)
  useEffect(() => {
    const t = window.setInterval(() => setClock(read()), 10_000)
    return () => window.clearInterval(t)
  }, [])
  return clock
}

// Which readouts go first when the strip runs out of room: weather first (the sky readout covers
// part of it), lamps lit last.
const PRIORITY = ["lights", "registers", "buses", "bins", "parking", "roads", "air", "weather"]

// The readouts that don't fit whole, dropped lowest priority first, so none is ever cut off.
// Widths are measured while a readout is shown and remembered while it is hidden.
function useFitting(ref: React.RefObject<HTMLDivElement | null>, deps: unknown[]) {
  const [hidden, setHidden] = useState<string[]>([])
  const widths = useRef<Record<string, number>>({})
  useLayoutEffect(() => {
    const box = ref.current
    if (!box) return
    const fit = () => {
      const present: string[] = []
      for (const el of box.querySelectorAll<HTMLElement>("[data-readout]")) {
        const id = el.dataset.readout!
        present.push(id)
        if (!el.hidden) widths.current[id] = el.offsetWidth
      }
      let total = present.reduce((s, id) => s + (widths.current[id] ?? 0), 0)
      const drop: string[] = []
      for (const id of [...present].sort((a, b) => PRIORITY.indexOf(b) - PRIORITY.indexOf(a))) {
        if (total <= box.clientWidth) break
        drop.push(id)
        total -= widths.current[id] ?? 0
      }
      setHidden((prev) => (prev.length === drop.length && prev.every((id) => drop.includes(id)) ? prev : drop))
    }
    fit()
    const ro = new ResizeObserver(fit)
    ro.observe(box)
    return () => ro.disconnect()
  }, deps) // eslint-disable-line react-hooks/exhaustive-deps
  return hidden
}

// Where each readout comes from (lib/sources.ts); at a past moment, through the twin's memory.
const READOUT_SOURCES: Record<string, string[]> = {
  lights: ["smart_nicosia"], parking: ["smart_nicosia"], bins: ["smart_nicosia"], air: ["smart_nicosia"],
  weather: ["open_meteo"], registers: ["registers"], buses: ["bus_live"], roads: ["road_speeds"],
}

function fromLine(ids: string[], list: Parameters<typeof shortName>[1]) {
  const names = ids.map((id) => shortName(id, list))
  const text = names.length > 1 ? `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}` : names[0]
  return `From ${text}`
}

function Readout({ id, hidden, label, question, note, loading, past, children }: {
  id: string; hidden: string[]; label: string; question: string; note?: string | null; loading: boolean; past?: boolean; children: ReactNode
}) {
  const busy = useChat((s) => s.busy)
  const sources = useSources((s) => s.list)
  const from = READOUT_SOURCES[id]
  const fromIds = from && past && id !== "buses" && id !== "registers" ? [...from, "twin_memory"] : from
  return (
    <Tooltip>
      <TooltipTrigger
        render={
          <button
            type="button"
            data-readout={id}
            hidden={hidden.includes(id)}
            disabled={busy}
            onClick={() => send(question)}
            className="flex h-full shrink-0 flex-col justify-center gap-0.5 border-r border-line px-4 text-left transition-colors outline-none hover:bg-accent/60 focus-visible:bg-accent/60 disabled:cursor-default disabled:hover:bg-transparent"
          />
        }
      >
        <span className="font-condensed text-[12px] leading-none text-muted-foreground">{label}</span>
        {loading ? <Skeleton className="h-[18px] w-14" /> : (
          <span className="font-readout text-[19px] leading-none font-semibold">{children}</span>
        )}
      </TooltipTrigger>
      <TooltipContent side="bottom">
        {note && <span className="block">{note.replace(/^./, (c) => c.toUpperCase())}.</span>}
        {question}
        {fromIds && <span className="mt-1 block text-muted-foreground">{fromLine(fromIds, sources)}</span>}
      </TooltipContent>
    </Tooltip>
  )
}

const Of = ({ children }: { children: ReactNode }) => (
  <span className="text-[14px] font-normal text-muted-foreground">{children}</span>
)

// The real sky: when it gets dark (or light) next, with the moon's phase. The map follows it.
function SkyReadout() {
  const at = useClock((s) => s.at)
  const now = useClock((s) => s.now)
  const sky = useMemo(() => skyAt(new Date(at ?? now)), [at, now])
  const { moon, sun } = sky
  return (
    <Tooltip>
      <TooltipTrigger
        render={<div tabIndex={0} className="hidden shrink-0 cursor-default items-center gap-2.5 border-l border-line px-4 outline-none focus-visible:bg-accent/60 lg:flex" />}
      >
        {sky.dark ? <MoonGlyph phase={moon.phase} className="size-6" /> : <SunIcon className="size-5 text-sodium" />}
        <div className="flex flex-col gap-0.5 leading-none">
          <span className="font-condensed text-[12px] text-muted-foreground">{sky.dark ? "Light at" : "Dark at"}</span>
          <span className="font-readout text-[19px] font-semibold">{nicosiaTime(sky.next.at)}</span>
        </div>
      </TooltipTrigger>
      <TooltipContent side="bottom">
        {moonName(moon.phase)}, {Math.round(moon.lit * 100)}% lit. The sun is {Math.abs(Math.round(sun.altitude))}°{" "}
        {sun.altitude >= 0 ? "above" : "below"} the horizon.
      </TooltipContent>
    </Tooltip>
  )
}

// A readout the twin's memory holds nothing for at the clock's moment.
const NoRecord = () => <span className="text-[15px] font-medium text-muted-foreground">No record</span>

// The compared moment's value beside a readout ("98 last week").
function Then({ children, when }: { children: ReactNode; when: string | null }) {
  if (!when) return null
  return <span className="ml-2 text-[13px] font-normal text-muted-foreground">{children} {when}</span>
}

// Where the strip says which moment its readouts are from: live, or the clock's moment with the
// way back.
function MomentReadout() {
  const at = useClock((s) => s.at)
  const clock = useNicosiaClock()
  if (at === null) {
    return (
      <div className="hidden shrink-0 items-center gap-3 border-l border-line px-4 sm:flex">
        <span className="flex items-center gap-2 text-[12px] text-muted-foreground">
          <span className="live-dot size-2 rounded-full bg-sodium" aria-hidden="true" />
          Live
        </span>
        <div className="flex flex-col items-end leading-none">
          <span className="font-readout text-[19px] font-semibold">{clock.time}</span>
          <span className="mt-1 font-condensed text-[12px] text-muted-foreground"><span lang="el">Λευκωσία</span>, {clock.day}</span>
        </div>
      </div>
    )
  }
  return (
    <div className="hidden shrink-0 items-center gap-3 border-l border-line pr-2.5 pl-4 sm:flex">
      <div className="flex flex-col items-end leading-none">
        <span className="flex items-center gap-1 text-[12px] text-muted-foreground"><HistoryIcon className="size-3.5" aria-hidden="true" />Viewing</span>
        <span className="mt-1 font-readout text-[17px] font-semibold text-sodium">{fmtMoment(at)}</span>
      </div>
      <Tooltip>
        <TooltipTrigger render={<Button size="sm" className="h-8 gap-1.5" onClick={() => { goLive(); focusClock(useClock.getState().expanded ? "track" : "toggle") }} />}>
          <RadioIcon data-icon="inline-start" />
          Live
        </TooltipTrigger>
        <TooltipContent side="bottom">Back to now <Kbd>L</Kbd></TooltipContent>
      </Tooltip>
    </div>
  )
}

export function StatusStrip() {
  const data = useClock((s) => s.strip)
  const then = useClock((s) => s.compareStrip)
  const at = useClock((s) => s.at)
  const compare = useClock((s) => s.compare)
  const other = useClock((s) => compareAt(s))
  const busy = useChat((s) => s.busy)
  const loading = data === null
  const d = data ?? {}
  const t = then ?? {}
  const past = at !== null
  const when = then ? compareName(compare, other) : null
  const readouts = useRef<HTMLDivElement>(null)
  const hidden = useFitting(readouts, [data, then])
  // A readout's question asks about the clock's moment when it isn't now.
  const ask = (live: string, then: string) => (past ? `${then} at ${fmtMoment(at!)}?` : live)
  const reason = (r?: { unknown?: boolean; reason?: string | null }) => (r?.unknown ? r.reason ?? "The twin's memory holds nothing for this moment" : null)

  return (
    <header className={cn("absolute inset-x-0 top-0 z-20 flex h-[52px] items-stretch border-b bg-night", past ? "border-sodium/45" : "border-line")}>
      <div className="flex shrink-0 items-center gap-2.5 border-r border-line pr-4 pl-3.5">
        <WallsMark working={busy} className="size-7 text-sodium" />
        <div className="flex flex-col leading-none">
          <h1 className="text-[15px] font-semibold tracking-[-0.01em]">Nicosia</h1>
          <span className="mt-1 text-[12px] text-muted-foreground">Digital twin</span>
        </div>
      </div>

      <div ref={readouts} className="flex min-w-0 flex-1 items-stretch overflow-hidden" aria-busy={loading}>
        {(loading || d.lights) && (
          <Readout id="lights" hidden={hidden} past={past} label="Lamps lit" question={ask("Which street lights are on right now?", "Which street lights were on")} note={reason(d.lights)} loading={loading}>
            {d.lights?.unknown ? <NoRecord /> : <><span className="text-sodium">{d.lights?.on?.toLocaleString()}</span> <Of>of {d.lights?.total?.toLocaleString()}</Of></>}
            <Then when={when}>{t.lights?.unknown ? "no record" : t.lights?.on?.toLocaleString()}</Then>
          </Readout>
        )}
        {(loading || d.parking) && (
          <Readout id="parking" hidden={hidden} past={past} label="Free parking" question={ask("Where can I park right now? Show the free spaces on the map", "Where were the free parking spaces")} note={reason(d.parking)} loading={loading}>
            {d.parking?.unknown ? <NoRecord /> : <>{d.parking?.free?.toLocaleString()} <Of>of {d.parking?.total?.toLocaleString()}</Of></>}
            <Then when={when}>{t.parking?.unknown ? "no record" : t.parking?.free?.toLocaleString()}</Then>
          </Readout>
        )}
        {(loading || d.bins) && (
          <Readout id="bins" hidden={hidden} past={past} label="Full bins" question={ask("Which bins are full? Show them in 3D", "Which bins were full")} note={reason(d.bins)} loading={loading}>
            {d.bins?.unknown ? <NoRecord /> : <><span className={cn(d.bins?.full ? "text-signal" : "")}>{d.bins?.full?.toLocaleString()}</span> <Of>of {d.bins?.total?.toLocaleString()}</Of></>}
            <Then when={when}>{t.bins?.unknown ? "no record" : t.bins?.full?.toLocaleString()}</Then>
          </Readout>
        )}
        {(loading || d.air) && (
          <Readout id="air" hidden={hidden} past={past} label="Air" question={ask("How are air quality and noise across the city?", "How were air quality and noise across the city")} note={reason(d.air)} loading={loading}>
            {d.air?.unknown ? <NoRecord /> : <>{AIR_WORDS[d.air?.band ?? ""] ?? "Unknown"} <Of>CAQI {d.air?.caqi}</Of></>}
            <Then when={when}>{t.air?.unknown ? "no record" : t.air?.caqi !== undefined ? `CAQI ${t.air.caqi}` : null}</Then>
          </Readout>
        )}
        {(loading || d.weather) && (
          <Readout id="weather" hidden={hidden} past={past} label={d.weather ? d.weather.sky.replace(/^./, (c) => c.toUpperCase()) : "Weather"} question={ask("Is it a good time for a walk around the old town?", "What was the weather like")} loading={loading}>
            {d.weather ? Math.round(d.weather.temp_c) : ""}<Of>°C</Of>
            <Then when={when && t.weather ? when : null}>{t.weather ? `${Math.round(t.weather.temp_c)}°C` : null}</Then>
          </Readout>
        )}
        {d.registers && (
          <Readout id="registers" hidden={hidden} past={past} label="Dangerous buildings" question="Show the dangerous buildings in the old town in 3D" loading={false}>
            <span className="text-signal">{d.registers.buildings.toLocaleString()}</span> <Of>on the registers</Of>
          </Readout>
        )}
        {d.buses && (
          <Readout id="buses" hidden={hidden} past={past} label="Buses running" question="Where are the buses right now?" loading={false}>
            <span className="text-sodium">{d.buses.running.toLocaleString()}</span> <Of>on {d.buses.lines ?? 0} lines</Of>
          </Readout>
        )}
        {d.slowest_road && (
          <Readout id="roads" hidden={hidden} past={past} label="Slowest road" question="Which roads are slow right now?" loading={false}>
            <span className="inline-block max-w-[11rem] truncate align-bottom">{d.slowest_road.name}</span>{" "}
            <Of>{Math.round(d.slowest_road.speed_kmh)} km/h</Of>
          </Readout>
        )}
      </div>

      <SkyReadout />
      <MomentReadout />
    </header>
  )
}
