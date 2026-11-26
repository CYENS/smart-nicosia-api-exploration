/* The now line, top right: the weather, the air in words, when it gets dark (or light) and the
   time, in one quiet line. Clicking the first part opens the Outside topic. The second part is
   the city clock folded up: the time, or the moment the twin shows in sodium with the way back
   to live, and the button that shows the clock's track. The clock's keys live here, so they
   work whether the track shows or not: T puts the keyboard on the track, L goes back to live. */
import { ChevronDownIcon, ChevronUpIcon, HistoryIcon, RadioIcon, SunIcon } from "lucide-react"
import { useEffect, useState } from "react"

import { MoonGlyph } from "@/components/moon-glyph"
import { Button } from "@/components/ui/button"
import { Kbd } from "@/components/ui/kbd"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { compareAt, compareName, fmtMoment, fmtTime, focusClock, goLive, setExpanded, useClock } from "@/lib/clock"
import { useGeometry } from "@/lib/geometry"
import { typing } from "@/lib/hooks"
import { GAP, PHONE_GAP, ROW_H } from "@/lib/layout"
import { nicosiaTime } from "@/lib/map/engine"
import { moonName } from "@/lib/map/sky"
import { airWords, cap, useSky, useStrip } from "@/lib/readouts"
import { selectTopic, useTopics } from "@/lib/registries/topics"
import { cn } from "@/lib/utils"

export const CLOCK_ID = "city-clock"

function nothingRecorded(strip: ReturnType<typeof useClock.getState>["strip"]) {
  if (!strip || strip.live) return false
  return [strip.lights, strip.parking, strip.bins, strip.air].every((r) => !r || r.unknown)
}

// The clock's keys anywhere on the page: L goes back to live, T puts the keyboard on the track.
function useClockKeys() {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (typing(e) || e.ctrlKey || e.metaKey || e.altKey) return
      const k = e.key.toLowerCase()
      if (k === "l" && useClock.getState().at !== null) { e.preventDefault(); goLive() }
      else if (k === "t") {
        e.preventDefault()
        if (!useClock.getState().expanded) setExpanded(true)
        focusClock("track")
      }
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [])
}

// What the clock says to a screen reader once it settles on a moment.
function useAnnouncement() {
  const at = useClock((s) => s.at)
  const loading = useClock((s) => s.loading)
  const strip = useClock((s) => s.strip)
  const [text, setText] = useState("")
  useEffect(() => {
    const t = window.setTimeout(() => {
      if (at === null) setText("Live: the twin shows now")
      else if (!loading) setText(`Showing ${fmtMoment(at, { year: true })}${nothingRecorded(strip) ? ". The twin's memory holds nothing for this moment" : ""}`)
    }, 700)
    return () => window.clearTimeout(t)
  }, [at, loading, strip])
  return text
}

// The parts that have something to say, with a quiet dot between them.
function joined(parts: (React.ReactElement | null | undefined | false | "")[]) {
  return parts.filter(Boolean).flatMap((p, i) => (i ? [<span key={`d${i}`} className="text-muted-foreground/60" aria-hidden="true">·</span>, p] : [p]))
}

export function NowLine() {
  const g = useGeometry()
  const { strip, past } = useStrip()
  const sky = useSky()
  const at = useClock((s) => s.at)
  const now = useClock((s) => s.now)
  const expanded = useClock((s) => s.expanded)
  const compare = useClock((s) => s.compare)
  const other = useClock((s) => compareAt(s))
  const outsideOpen = useTopics((s) => s.active === "outside")
  const hasOutside = useTopics((s) => s.list.some((t) => t.id === "outside"))
  const said = useAnnouncement()
  useClockKeys()

  // A phone's clock console takes this row while it shows.
  if (!g.desktop && expanded) return <span className="sr-only" aria-live="polite">{said}</span>

  const w = strip?.weather
  const air = airWords(strip?.air)
  const light = `${sky.dark ? "Light at" : "Dark at"} ${nicosiaTime(sky.next.at)}`
  const vs = compareName(compare, other)
  const outsideLabel = [
    w ? `${cap(w.sky)}, ${Math.round(w.temp_c)}°C` : null,
    air ? `${air.toLowerCase()} air` : null,
    light,
  ].filter(Boolean).join(", ")

  const toggleClock = () => {
    setExpanded(!expanded)
    focusClock(expanded ? "toggle" : "track")
  }

  return (
    <div
      className="panel-float absolute z-20 flex items-center gap-0.5 rounded-xl p-0.5"
      style={g.desktop ? { top: GAP, right: GAP, height: ROW_H } : { top: g.nowTop, right: PHONE_GAP, height: ROW_H }}
    >
      {hasOutside && (
        <Tooltip>
          <TooltipTrigger
            render={
              <button
                type="button"
                aria-pressed={outsideOpen}
                aria-label={`${past ? "Outside then" : "Outside now"}: ${outsideLabel}. ${outsideOpen ? "Close" : "Open"} the Outside card`}
                onClick={() => selectTopic(outsideOpen ? null : "outside")}
                className={cn(
                  "flex h-full items-center gap-2 rounded-[10px] px-2.5 text-[13.5px] whitespace-nowrap transition-colors outline-none",
                  "hover:bg-muted focus-visible:ring-3 focus-visible:ring-ring/60 aria-pressed:bg-sodium/[0.14] aria-pressed:inset-ring-1 aria-pressed:inset-ring-sodium/55",
                )}
              />
            }
          >
            {sky.dark ? <MoonGlyph phase={sky.moon.phase} className="size-[18px]" /> : <SunIcon className="size-[18px] text-muted-foreground" aria-hidden="true" />}
            {joined([
              w && (
                <span key="w" className="font-readout text-[15px] font-semibold">
                  {g.desktop && <span className="mr-1.5 font-sans text-[13.5px] font-normal">{cap(w.sky)}</span>}
                  {Math.round(w.temp_c)}°
                </span>
              ),
              air && <span key="a">{air} air</span>,
              (g.vw >= 1024 || !(w || air)) && <span key="l" className="text-muted-foreground">{light}</span>,
            ])}
          </TooltipTrigger>
          <TooltipContent side="bottom" className="flex-col items-start gap-0.5">
            <span>{light}. {sky.dark ? `${moonName(sky.moon.phase)}, ${Math.round(sky.moon.lit * 100)}% lit.` : `The sun is ${Math.round(sky.sun.altitude)}° up.`}</span>
            <span className="text-background/70">{outsideOpen ? "Close" : "Open"} the weather and the air</span>
          </TooltipContent>
        </Tooltip>
      )}

      <span className="mx-0.5 h-5 w-px bg-line" aria-hidden="true" />

      <Tooltip>
        <TooltipTrigger
          render={
            <button
              type="button"
              data-clock-part={expanded ? undefined : "toggle"}
              aria-expanded={expanded}
              aria-controls={expanded ? CLOCK_ID : undefined}
              aria-label={`${at === null ? `Live, ${fmtMoment(now)}` : `Showing ${fmtMoment(at)}`}. ${expanded ? "Hide" : "Show"} the city clock`}
              onClick={toggleClock}
              className={cn(
                "flex h-full items-center gap-1.5 rounded-[10px] pr-1.5 pl-2.5 whitespace-nowrap transition-colors outline-none",
                "hover:bg-muted focus-visible:ring-3 focus-visible:ring-ring/60 aria-expanded:bg-accent/60",
              )}
            />
          }
        >
          {at === null ? (
            <span className="live-dot size-2 shrink-0 rounded-full bg-sodium" aria-hidden="true" />
          ) : (
            <HistoryIcon className="size-4 shrink-0 text-sodium" aria-hidden="true" />
          )}
          <span className={cn("font-readout text-[15px] font-semibold", at !== null && "text-sodium")}>
            {at === null ? fmtTime(now) : fmtMoment(at, { weekday: g.desktop })}
          </span>
          {vs && g.desktop && <span className="text-[12.5px] text-muted-foreground">vs {vs}</span>}
          {expanded ? <ChevronUpIcon className="size-4 text-muted-foreground" /> : <ChevronDownIcon className="size-4 text-muted-foreground" />}
        </TooltipTrigger>
        <TooltipContent side="bottom">{expanded ? "Hide the city clock" : "Go back in time with the city clock"} <Kbd>T</Kbd></TooltipContent>
      </Tooltip>

      {at !== null && (
        <Tooltip>
          <TooltipTrigger render={<Button size="sm" className="ml-0.5 h-8 gap-1.5 rounded-[10px] px-2.5" onClick={() => { goLive(); focusClock(useClock.getState().expanded ? "track" : "toggle") }} />}>
            <RadioIcon data-icon="inline-start" />
            Live
          </TooltipTrigger>
          <TooltipContent side="bottom">Back to now <Kbd>L</Kbd></TooltipContent>
        </Tooltip>
      )}
      <span className="sr-only" aria-live="polite">{said}</span>
    </div>
  )
}
