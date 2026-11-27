/* The inspector's readout for a spot ("What's it like here?"): sun or shade at the clock's
   moment and the day around it, the air and the noise estimated there with how sure that is,
   the temperature and the wind. */
import { ArrowUpIcon, CloudSunIcon } from "lucide-react"

import { DayStrip } from "@/components/environment/parts"
import { SHADE } from "@/lib/environment/colors"
import { fmtValue } from "@/lib/environment/store"
import { registerInspectorSection, type SectionProps } from "@/lib/registries/inspector-sections"
import type { Entity } from "@/lib/entities"
import { cn } from "@/lib/utils"

type Estimate = {
  metric: string; label: string; unit: string; value: number | null; uncertainty?: number; confidence?: number
  band?: string | null; from?: string; why?: string | null; nearest?: { id: string; name: string; metres: number } | null
}
type SpotData = {
  at: string | null
  sun: {
    altitude_deg: number; bearing_deg: number; from: string; up: boolean; shade?: boolean | null; on_roof?: boolean
    hours_today?: number | null; day?: { time: string; altitude_deg: number; sun: boolean }[] | null
    times?: { sunrise: string | null; sunset: string | null; daylight_h: number }; error?: string
  }
  air: Estimate[]
  temperature: Estimate | null
  wind: { speed_kmh: number; speed_ms: number; from_deg: number; from: string } | null
}
type SpotEntity = Entity & { environment?: SpotData }

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section aria-label={title} className="flex flex-col gap-2.5">
      <h3 className="text-[12.5px] text-muted-foreground">{title}</h3>
      {children}
    </section>
  )
}

function SunReadout({ data, moment }: { data: SpotData; moment: string | null }) {
  const s = data.sun
  const state = !s.up ? "night" : s.shade === null || s.shade === undefined ? "unknown" : s.shade ? "shade" : "sun"
  const big = { sun: "In the sun", shade: "In shade", night: "The sun is down", unknown: "Sun or shade unknown" }[state]
  // The moment read: the clock's (local time from the server) or now in Nicosia.
  const nowHm = data.at ? data.at.slice(11, 16) : new Date().toLocaleTimeString("en-GB", { timeZone: "Asia/Nicosia", hour: "2-digit", minute: "2-digit" })
  return (
    <div className="flex flex-col gap-3" aria-live="polite">
      <div className="flex items-center gap-3">
        <span
          className="size-9 shrink-0 rounded-full"
          style={{ background: state === "sun" ? SHADE.sun : state === "shade" ? SHADE.shade : "var(--muted)" }}
          aria-hidden="true"
        />
        <p className="min-w-0 leading-tight">
          <span className={cn("block font-readout text-[26px] font-semibold", state === "sun" && "text-sodium")}>{big}</span>
          <span className="text-[12.5px] text-muted-foreground">
            {s.up ? `The sun ${Math.round(s.altitude_deg)}° up from the ${s.from}` : "No direct sun"}
            {moment ? `, at ${moment}` : `, now (${nowHm})`}
            {s.on_roof ? ". This point is on a roof." : ""}
          </span>
        </p>
      </div>
      {s.error && <p className="text-[12.5px] text-muted-foreground">{s.error}; the sun's position is still right.</p>}
      {s.hours_today !== null && s.hours_today !== undefined && (
        <p className="text-[13px]">
          <span className="font-readout text-[17px] font-semibold">{fmtValue(s.hours_today)}</span>
          <span className="text-muted-foreground"> hours of direct sun that day{s.times ? `, of ${s.times.daylight_h} of daylight (${s.times.sunrise} to ${s.times.sunset})` : ""}</span>
        </p>
      )}
      {s.day && <DayStrip day={s.day} now={nowHm} label="That day here, quarter by quarter" />}
    </div>
  )
}

function EstimateRow({ e }: { e: Estimate }) {
  if (e.value === null) {
    return (
      <li className="flex items-baseline justify-between gap-3 text-[13px]">
        <span className="text-muted-foreground">{e.label}</span>
        <span className="text-right text-[12.5px] text-muted-foreground">{e.why ?? "unknown"}</span>
      </li>
    )
  }
  const conf = Math.round(100 * (e.confidence ?? 0))
  return (
    <li className="flex flex-col gap-1">
      <div className="flex items-baseline justify-between gap-3">
        <span className="text-[13px]">{e.label}</span>
        <span className="font-readout text-[16px] font-semibold tabular-nums">
          {fmtValue(e.value)}
          <span className="text-[12.5px] font-normal text-muted-foreground"> ± {fmtValue(e.uncertainty ?? 0)}{e.unit ? ` ${e.unit}` : ""}</span>
        </span>
      </div>
      <div className="flex items-center gap-2">
        <div className="h-1 flex-1 rounded-full bg-raised" role="meter" aria-label={`How sure the ${e.label} estimate is`}
          aria-valuemin={0} aria-valuemax={100} aria-valuenow={conf} aria-valuetext={`${conf}%, from ${e.from}`}>
          <div className="h-1 rounded-full bg-muted-foreground" style={{ width: `${conf}%` }} />
        </div>
        <span className="shrink-0 font-condensed text-[11.5px] text-muted-foreground">{e.band ? `${e.band}, ` : ""}from {e.from}</span>
      </div>
    </li>
  )
}

function SpotSection({ entity }: SectionProps) {
  const data = (entity as SpotEntity).environment
  if (!data) return null
  const moment = entity.moment?.applies ? entity.moment.label : null
  const air = data.air.filter((a) => a.metric !== "noise_db")
  const noise = data.air.find((a) => a.metric === "noise_db")
  const nearest = data.air.find((a) => a.nearest)?.nearest
  return (
    <div className="flex flex-col gap-5">
      <SunReadout data={data} moment={moment} />
      <Section title="Air and noise here">
        <ul className="flex flex-col gap-3">
          {air.map((e) => <EstimateRow key={e.metric} e={e} />)}
          {noise && <EstimateRow e={noise} />}
        </ul>
        <p className="text-[12px] leading-snug text-muted-foreground">
          Estimated from the city's stations{nearest ? ` (the nearest, ${nearest.name}, is ${fmtValue(nearest.metres)} m away)` : ""} and the regional air model. The bar is how sure: full at a station, low where only the model speaks.
        </p>
      </Section>
      <Section title="Weather here">
        <dl className="grid grid-cols-2 gap-x-4 gap-y-3">
          <div className="flex flex-col gap-0.5">
            <dt className="font-condensed text-[12px] text-muted-foreground">Temperature</dt>
            <dd className="font-readout text-[17px] leading-tight font-semibold">
              {data.temperature?.value !== null && data.temperature?.value !== undefined
                ? <>{fmtValue(data.temperature.value)}<span className="ml-1 text-[13px] font-normal text-muted-foreground">± {fmtValue(data.temperature.uncertainty ?? 0)} °C</span></>
                : <span className="text-[13px] font-normal text-muted-foreground">unknown</span>}
            </dd>
          </div>
          <div className="flex flex-col gap-0.5">
            <dt className="font-condensed text-[12px] text-muted-foreground">Wind</dt>
            <dd className="flex items-center gap-1.5 font-readout text-[17px] leading-tight font-semibold">
              {data.wind ? (
                <>
                  {/* The arrow points where the wind goes. */}
                  <ArrowUpIcon className="size-4 text-muted-foreground" style={{ transform: `rotate(${(data.wind.from_deg + 180) % 360}deg)` }} aria-hidden="true" />
                  {data.wind.speed_kmh}<span className="text-[13px] font-normal text-muted-foreground">km/h from the {data.wind.from}</span>
                </>
              ) : <span className="text-[13px] font-normal text-muted-foreground">unknown</span>}
            </dd>
          </div>
        </dl>
      </Section>
    </div>
  )
}

registerInspectorSection({ id: "spot", order: 12, kinds: ["spot"], Component: SpotSection })

export const SPOT_ICON = CloudSunIcon
