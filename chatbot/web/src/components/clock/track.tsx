/* The city clock's track: the stretch of time on show, drawn as the sky over Nicosia (nights
   deep, days lighter), with what the twin's memory holds (hatched where it holds nothing), the
   events it logged, the moments answers pointed to, a replay's range and the compared moment.
   The slider over it moves the clock; its keys step a quarter hour, an hour (Shift) or a day
   (Page Up and Down), and Home or L goes back to live. */
import { Slider as SliderPrimitive } from "@base-ui/react/slider"
import SunCalc from "suncalc"
import { useEffect, useMemo, useRef, useState, type KeyboardEvent, type PointerEvent } from "react"

import {
  STEP_MS, compareAt, earliest, fmtMoment, fmtTime, goLive, setAt, spanMs, stepBy,
  togglePlay, useClock, type Track,
} from "@/lib/clock"
import { cn } from "@/lib/utils"

const LAT = 35.1725
const LON = 33.365
const HOUR = 3600e3
const DAY = 24 * HOUR
const BAR_H = 26

function useWidth() {
  const ref = useRef<HTMLDivElement>(null)
  const [w, setW] = useState(600)
  useEffect(() => {
    const el = ref.current
    if (!el) return
    const ro = new ResizeObserver(([e]) => setW(Math.max(160, Math.round(e.contentRect.width))))
    ro.observe(el)
    return () => ro.disconnect()
  }, [])
  return [ref, w] as const
}

// Nights (civil dusk to civil dawn) that touch [start, end].
function nights(start: number, end: number) {
  const out: { from: number; to: number }[] = []
  for (let d = start - DAY; d <= end + DAY; d += DAY) {
    const noon = new Date(d)
    const today = SunCalc.getTimes(noon, LAT, LON)
    const tomorrow = SunCalc.getTimes(new Date(d + DAY), LAT, LON)
    const from = today.dusk.getTime(), to = tomorrow.dawn.getTime()
    if (to > start && from < end && Number.isFinite(from) && Number.isFinite(to)) out.push({ from, to })
  }
  return out
}

// Midnight in Nicosia at or before t.
function midnight(t: number) {
  const wall = new Date(t).toLocaleString("sv-SE", { timeZone: "Asia/Nicosia", hour12: false })
  const [h, m] = wall.slice(11, 16).split(":").map(Number)
  return t - (h * 60 + m) * 60e3 - (t % 60e3)
}

function ticks(start: number, end: number, ms: number) {
  const out: { t: number; label: string; major: boolean }[] = []
  if (ms <= 6 * HOUR) {
    for (let t = Math.ceil(start / HOUR) * HOUR; t <= end; t += HOUR) out.push({ t, label: fmtTime(t), major: fmtTime(t) === "00:00" })
  } else if (ms <= DAY) {
    for (let t = Math.ceil(start / (3 * HOUR)) * 3 * HOUR; t <= end; t += 3 * HOUR) {
      const midnightTick = fmtTime(t) === "00:00"
      out.push({ t, label: midnightTick ? dayName(t) : fmtTime(t), major: midnightTick })
    }
  } else {
    const every = ms <= 7 * DAY ? 1 : 4
    let k = 0
    for (let t = midnight(start) + DAY; t <= end; t += DAY, k++) {
      const m = midnight(t + 2 * HOUR) // stays on midnight across a clock change
      if (k % every === 0) out.push({ t: m, label: ms <= 7 * DAY ? dayName(m) : dayMonth(m), major: true })
    }
  }
  return out
}

const dayName = (t: number) => new Date(t).toLocaleDateString("en-GB", { timeZone: "Asia/Nicosia", weekday: "short", day: "numeric" })
const dayMonth = (t: number) => new Date(t).toLocaleDateString("en-GB", { timeZone: "Asia/Nicosia", day: "numeric", month: "short" })

// The events in the window, gathered into buckets a few pixels wide.
function eventTicks(track: Track | null, start: number, end: number, width: number) {
  if (!track) return []
  const per = Math.max(HOUR, ((end - start) / width) * 4)
  const buckets = new Map<number, { t: number; alarms: number; faults: number }>()
  for (const e of track.events) {
    if (e.t < start - HOUR || e.t > end) continue
    const k = Math.floor(e.t / per) * per
    const b = buckets.get(k) ?? { t: k + per / 2, alarms: 0, faults: 0 }
    b.alarms += e.full_bins + e.air
    b.faults += e.faults
    buckets.set(k, b)
  }
  return [...buckets.values()]
}

// What the memory covers, as runs of hours: 0 is not recorded, the rest a share of the devices.
function coverageRuns(track: Track | null, start: number, end: number) {
  if (!track || !track.coverage.length) return [{ from: start, to: end, pct: 0 }]
  const runs: { from: number; to: number; pct: number }[] = []
  for (let t = Math.floor(start / HOUR) * HOUR; t < end; t += HOUR) {
    const h = Math.floor((t - track.start) / HOUR)
    const pct = h >= 0 && h < track.coverage.length ? track.coverage[h] : 0
    const last = runs[runs.length - 1]
    if (last && Math.abs(last.pct - pct) < 5 && last.to === t) last.to = t + HOUR
    else runs.push({ from: t, to: t + HOUR, pct })
  }
  return runs
}

export function ClockTrack({ className }: { className?: string }) {
  const s = useClock()
  const [box, width] = useWidth()
  const [hover, setHover] = useState<number | null>(null)
  const ms = spanMs(s.span)
  const end = s.windowEnd
  const start = end - ms
  const floor = earliest(s)
  const x = (t: number) => ((t - start) / ms) * width
  const at = s.at ?? s.now
  const other = compareAt(s)

  const sky = useMemo(() => nights(start, end), [start, end])
  const marks = useMemo(() => ticks(start, end, ms), [start, end, ms])
  const events = useMemo(() => eventTicks(s.track, start, end, width), [s.track, start, end, width])
  const cover = useMemo(() => coverageRuns(s.track, start, end), [s.track, start, end])

  // The slider's value: quarter hours from the start of the window.
  const steps = Math.round(ms / STEP_MS)
  const value = Math.max(0, Math.min(steps, Math.round((at - start) / STEP_MS)))
  const toTime = (v: number) => start + v * STEP_MS

  const onKey = (e: KeyboardEvent<HTMLInputElement> & { preventBaseUIHandler?: () => void }) => {
    const k = e.key
    let handled = true
    if (k === "ArrowLeft" || k === "ArrowDown") stepBy(-(e.shiftKey ? HOUR : STEP_MS))
    else if (k === "ArrowRight" || k === "ArrowUp") stepBy(e.shiftKey ? HOUR : STEP_MS)
    else if (k === "PageDown") stepBy(-DAY)
    else if (k === "PageUp") stepBy(DAY)
    else if (k === "Home" || k === "End" || k.toLowerCase() === "l") goLive()
    else if (k === " " || k.toLowerCase() === "k") togglePlay()
    else if (k === "[" || k === "]") jumpToMark(k === "]" ? 1 : -1)
    else handled = false
    if (handled) {
      e.preventBaseUIHandler?.()
      e.preventDefault()
    }
  }

  const onPointer = (e: PointerEvent<HTMLDivElement>) => {
    const r = e.currentTarget.getBoundingClientRect()
    const t = start + ((e.clientX - r.left) / Math.max(1, r.width)) * ms
    setHover(Math.max(start, Math.min(end, t)))
  }

  const hoverInfo = hover !== null ? describe(hover, s.track, s.pins) : null
  const live = s.at === null

  return (
    <div className={cn("flex min-w-0 flex-col gap-1", className)}>
      <div ref={box} className="relative" onPointerMove={onPointer} onPointerLeave={() => setHover(null)}>
        <svg width={width} height={BAR_H} className="pointer-events-none absolute inset-0 block overflow-visible rounded-md" aria-hidden="true">
          <defs>
            <pattern id="clock-hatch" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
              <rect width="6" height="6" fill="var(--night)" />
              <line x1="0" y1="0" x2="0" y2="6" stroke="rgb(135 152 176 / 0.22)" strokeWidth="2" />
            </pattern>
            <clipPath id="clock-clip"><rect width={width} height={BAR_H} rx="6" /></clipPath>
          </defs>
          <g clipPath="url(#clock-clip)">
            {/* Days, and the nights over them. */}
            <rect width={width} height={BAR_H} fill="#20324f" />
            {sky.map((n) => (
              <rect key={n.from} x={x(n.from)} width={Math.max(0, x(n.to) - x(n.from))} height={BAR_H} fill="#0c1526" />
            ))}
            {/* Where the memory holds nothing, hatched; where it holds part, a thinner bar below. */}
            {cover.filter((c) => c.pct < 3).map((c) => (
              <rect key={`u${c.from}`} x={x(c.from)} width={Math.max(0, x(c.to) - x(c.from))} height={BAR_H} fill="url(#clock-hatch)" opacity={0.9} />
            ))}
            {cover.filter((c) => c.pct >= 3).map((c) => (
              <rect key={`c${c.from}`} x={x(c.from)} y={BAR_H - 3} width={Math.max(0, x(c.to) - x(c.from))} height={3} fill="#8798b0" opacity={0.25 + 0.55 * (c.pct / 100)} />
            ))}
            {/* Before the clock can go: further back than the memory or 62 days. */}
            {floor > start && <rect width={Math.max(0, x(floor))} height={BAR_H} fill="var(--night)" opacity={0.55} />}
            {/* A replay's range. */}
            {s.range && (
              <rect x={x(s.range.start)} width={Math.max(2, x(s.range.end) - x(s.range.start))} y={1} height={BAR_H - 2} rx={4}
                fill="var(--sodium)" fillOpacity={0.14} stroke="var(--sodium)" strokeOpacity={0.55} />
            )}
            {/* Events: alarms (full bins, air) in signal red, faults faint. */}
            {events.map((e) => (
              <rect key={e.t} x={x(e.t) - 1} y={2} width={2} rx={1}
                height={Math.min(9, 3 + Math.log2(1 + e.alarms + e.faults / 3) * 2)}
                fill={e.alarms ? "var(--signal)" : "#8798b0"} opacity={e.alarms ? 0.9 : 0.45} />
            ))}
          </g>
          {/* Moments an answer pointed to. */}
          {s.pins.filter((p) => p.at >= start && p.at <= end).map((p) => (
            <path key={p.at} d={`M${x(p.at)},${BAR_H - 1} l4,-5 l-4,-5 l-4,5 z`} fill="var(--sodium)" stroke="var(--console)" strokeWidth={1.2} />
          ))}
          {/* The compared moment. */}
          {other !== null && other >= start && other <= end && (
            <line x1={x(other)} x2={x(other)} y1={-2} y2={BAR_H + 2} stroke="var(--foreground)" strokeWidth={1.5} strokeDasharray="3 2" />
          )}
          {hover !== null && <line x1={x(hover)} x2={x(hover)} y1={0} y2={BAR_H} stroke="var(--foreground)" strokeOpacity={0.35} />}
          {/* The clock's moment. */}
          <line x1={x(at)} x2={x(at)} y1={-3} y2={BAR_H + 3} stroke="var(--sodium)" strokeWidth={2} />
        </svg>
        <SliderPrimitive.Root
          data-clock-part="track"
          value={value}
          min={0}
          max={steps}
          step={1}
          thumbAlignment="center"
          onValueChange={(v) => setAt(toTime(Array.isArray(v) ? v[0] : v))}
          className="relative"
        >
          <SliderPrimitive.Control className="relative flex w-full touch-none items-center select-none" style={{ height: BAR_H }}>
            <SliderPrimitive.Track className="relative h-full w-full cursor-pointer">
              <SliderPrimitive.Thumb
                getAriaLabel={() => "City clock"}
                getAriaValueText={() => (live ? `Live, ${fmtMoment(s.now)}` : `${fmtMoment(at, { year: true })}${s.playing ? ", playing" : ""}`)}
                onKeyDown={onKey}
                className={cn(
                  "block size-4 rounded-full border-2 border-console bg-sodium shadow-[0_0_0_1px_rgb(255_174_69_/_0.5)] outline-none",
                  "transition-[box-shadow] has-[:focus-visible]:shadow-[0_0_0_4px_rgb(255_174_69_/_0.45)] hover:shadow-[0_0_0_3px_rgb(255_174_69_/_0.3)] active:shadow-[0_0_0_5px_rgb(255_174_69_/_0.35)]",
                )}
                style={{ top: BAR_H / 2 }}
              />
            </SliderPrimitive.Track>
          </SliderPrimitive.Control>
        </SliderPrimitive.Root>
        {hoverInfo && hover !== null && (
          <div
            className="pointer-events-none absolute bottom-full z-10 mb-1.5 -translate-x-1/2 rounded-md border border-line bg-popover px-2 py-1 text-[12px] leading-snug whitespace-nowrap shadow-md"
            style={{ left: Math.max(70, Math.min(width - 70, x(hover))) }}
          >
            <span className="font-readout font-semibold">{fmtMoment(hover)}</span>
            {hoverInfo.lines.map((l) => <span key={l} className="block text-muted-foreground">{l}</span>)}
          </div>
        )}
      </div>
      <div className="relative h-3.5 font-readout text-[11.5px] leading-none text-muted-foreground" aria-hidden="true">
        {marks.map((m) => {
          const left = x(m.t)
          if (left < 14 || left > width - 30) return null
          return <span key={m.t} className={cn("absolute -translate-x-1/2", m.major && "text-foreground/80")} style={{ left }}>{m.label}</span>
        })}
        <span className={cn("absolute right-0", end >= s.now - 60e3 ? "text-sodium" : "")}>{end >= s.now - 60e3 ? "now" : fmtTime(end)}</span>
      </div>
    </div>
  )
}

// What the track holds at a moment, for its hover label.
function describe(t: number, track: Track | null, pins: { at: number; label: string }[]) {
  const lines: string[] = []
  if (track) {
    const h = Math.floor((t - track.start) / HOUR)
    const pct = h >= 0 && h < track.coverage.length ? track.coverage[h] : 0
    lines.push(pct < 3 ? "Not recorded in the twin's memory" : pct >= 97 ? "Recorded" : `Recorded for ${pct}% of the devices`)
    const hour = Math.floor(t / HOUR) * HOUR
    const ev = track.events.find((e) => e.t === hour)
    if (ev) {
      const parts = [
        ev.full_bins ? `${ev.full_bins} full bin${ev.full_bins > 1 ? "s" : ""}` : "",
        ev.air ? `${ev.air} air alert${ev.air > 1 ? "s" : ""}` : "",
        ev.faults ? `${ev.faults} lost connection${ev.faults > 1 ? "s" : ""} or faults` : "",
      ].filter(Boolean)
      if (parts.length) lines.push(parts.join(", "))
    }
  }
  const sun = SunCalc.getTimes(new Date(t), LAT, LON)
  for (const [name, when] of [["Dusk", sun.dusk], ["Dawn", sun.dawn]] as const) {
    if (Math.abs(when.getTime() - t) < 40 * 60e3) lines.push(`${name} at ${fmtTime(when.getTime())}`)
  }
  const pin = pins.find((p) => Math.abs(p.at - t) < 45 * 60e3)
  if (pin) lines.push(`An answer pointed here: ${pin.label}`)
  return { lines }
}

// The previous or next thing worth stopping at: an hour with alarms, a moment an answer pointed
// to, dusk or dawn.
export function jumpToMark(direction: 1 | -1) {
  const s = useClock.getState()
  const t = s.at ?? s.now
  const floor = earliest(s)
  const candidates: number[] = []
  for (const e of s.track?.events ?? []) if (e.full_bins + e.air > 0) candidates.push(e.t)
  for (const p of s.pins) candidates.push(p.at)
  for (const n of nights(t - 3 * DAY, t + 3 * DAY)) candidates.push(n.from, n.to)
  const next = candidates
    .map((c) => Math.round(c / STEP_MS) * STEP_MS)
    .filter((c) => c >= floor && c <= s.now && (direction > 0 ? c > t + 1 : c < t - 1))
    .sort((a, b) => (direction > 0 ? a - b : b - a))[0]
  if (next !== undefined) setAt(next)
  else if (direction > 0) goLive()
}
