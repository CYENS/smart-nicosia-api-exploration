import { useEffect, useState, type ReactNode } from "react"

import { Skeleton } from "@/components/ui/skeleton"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { WallsMark } from "@/components/walls-mark"
import { send, useChat } from "@/lib/chat"
import { cn } from "@/lib/utils"

type Overview = {
  lights?: { on: number; total: number; as_of?: string | null }
  parking?: { free: number; total: number }
  bins?: { full: number; total: number }
  air?: { caqi: number; band: string; stations: number }
  weather?: { temp_c: number; sky: string; daylight: boolean }
  registers?: { buildings: number }
}

const AIR_WORDS: Record<string, string> = {
  very_low: "Very good", low: "Good", medium: "Moderate", high: "Poor", very_high: "Very poor",
}

function useOverview() {
  const [data, setData] = useState<Overview | null>(null)
  useEffect(() => {
    let alive = true
    const load = () => fetch("/api/overview")
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((d: Overview) => { if (alive) setData(d) })
      .catch(() => { if (alive) setData((d) => d ?? {}) })
    load()
    const t = window.setInterval(load, 60_000)
    return () => { alive = false; window.clearInterval(t) }
  }, [])
  return data
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

function Readout({ label, question, loading, children }: {
  label: string; question: string; loading: boolean; children: ReactNode
}) {
  const busy = useChat((s) => s.busy)
  return (
    <Tooltip>
      <TooltipTrigger
        render={
          <button
            type="button"
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
      <TooltipContent side="bottom">{question}</TooltipContent>
    </Tooltip>
  )
}

const Of = ({ children }: { children: ReactNode }) => (
  <span className="text-[14px] font-normal text-muted-foreground">{children}</span>
)

export function StatusStrip() {
  const data = useOverview()
  const busy = useChat((s) => s.busy)
  const clock = useNicosiaClock()
  const loading = data === null
  const d = data ?? {}

  return (
    <header className="absolute inset-x-0 top-0 z-20 flex h-[52px] items-stretch border-b border-line bg-night">
      <div className="flex shrink-0 items-center gap-2.5 border-r border-line pr-4 pl-3.5">
        <WallsMark working={busy} className="size-7 text-sodium" />
        <div className="flex flex-col leading-none">
          <h1 className="text-[15px] font-semibold tracking-[-0.01em]">Nicosia</h1>
          <span className="mt-1 text-[12px] text-muted-foreground">Digital twin</span>
        </div>
      </div>

      <div className="flex min-w-0 flex-1 items-stretch overflow-x-auto [scrollbar-width:none]">
        {(loading || d.lights) && (
          <Readout label="Lamps lit" question="Which street lights are on right now?" loading={loading}>
            <span className="text-sodium">{d.lights?.on.toLocaleString()}</span> <Of>of {d.lights?.total.toLocaleString()}</Of>
          </Readout>
        )}
        {(loading || d.parking) && (
          <Readout label="Free parking" question="Where can I park right now? Show the free spaces on the map" loading={loading}>
            {d.parking?.free.toLocaleString()} <Of>of {d.parking?.total.toLocaleString()}</Of>
          </Readout>
        )}
        {(loading || d.bins) && (
          <Readout label="Full bins" question="Which bins are full? Show them in 3D" loading={loading}>
            <span className={cn(d.bins?.full ? "text-signal" : "")}>{d.bins?.full.toLocaleString()}</span> <Of>of {d.bins?.total.toLocaleString()}</Of>
          </Readout>
        )}
        {(loading || d.air) && (
          <Readout label="Air" question="How are air quality and noise across the city?" loading={loading}>
            {AIR_WORDS[d.air?.band ?? ""] ?? "Unknown"} <Of>CAQI {d.air?.caqi}</Of>
          </Readout>
        )}
        {(loading || d.weather) && (
          <Readout label={d.weather ? d.weather.sky.replace(/^./, (c) => c.toUpperCase()) : "Weather"} question="Is it a good time for a walk around the old town?" loading={loading}>
            {d.weather ? Math.round(d.weather.temp_c) : ""}<Of>°C</Of>
          </Readout>
        )}
        {d.registers && (
          <Readout label="Dangerous buildings" question="Show the dangerous buildings in the old town in 3D" loading={false}>
            <span className="text-signal">{d.registers.buildings.toLocaleString()}</span> <Of>on the registers</Of>
          </Readout>
        )}
      </div>

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
    </header>
  )
}
