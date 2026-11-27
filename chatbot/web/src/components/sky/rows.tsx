/* Layers rows ("Aircraft over Nicosia", "PWD traffic detectors & CCTV") and the Outside card's
   ISS visible pass readout. */
import { ENV_GROUP } from "@/components/environment/rows"
import { DotLegend } from "@/components/topics/builtin"
import { FromSources } from "@/components/sources/from-sources"
import { Skeleton } from "@/components/ui/skeleton"
import { useClock } from "@/lib/clock"
import { fmtCount } from "@/lib/readouts"
import { registerSystemLayer } from "@/lib/registries/layers"
import { registerTopicSection } from "@/lib/registries/topics"
import { setAircraftOn, setItsOn } from "@/lib/sky/map"
import { useSkyLayers, type ISSPass } from "@/lib/sky/store"

function aircraftSummaryText(ac: ReturnType<typeof useSkyLayers.getState>["aircraft"], past: boolean) {
  if (!ac.on) return "Live within 40 nm via adsb.lol, 24-hour clock replay"
  if (ac.status === "loading" && !ac.fetchedAt) return "Reading community ADS-B receivers"
  if (ac.status === "unavailable") {
    return past
      ? "No recorded aircraft for this moment (24-hour memory only)"
      : `Receiver network didn't answer${ac.error ? `: ${ac.error}` : ""}`
  }
  if (!ac.count) {
    return `${past ? "No aircraft recorded" : "No aircraft seen"} within 40 nm. Community coverage is partial`
  }
  const parts = [`${fmtCount(ac.airborne)} airborne`]
  if (ac.onGround) parts.push(`${fmtCount(ac.onGround)} on ground`)
  return `${parts.join(", ")} within 40 nm${past ? " then" : ""}. Community coverage is partial`
}

registerSystemLayer({
  id: "sky_aircraft",
  group: ENV_GROUP,
  order: 55,
  label: "Aircraft over Nicosia",
  useOn: () => useSkyLayers((s) => s.aircraft.on),
  setOn: setAircraftOn,
  Summary: function Summary() {
    const ac = useSkyLayers((s) => s.aircraft)
    const past = useClock((s) => s.at !== null)
    return <>{aircraftSummaryText(ac, past)}</>
  },
  Legend: function Legend() {
    return (
      <div className="flex flex-col gap-1.5">
        <DotLegend
          label="Aircraft over Nicosia"
          items={[
            ["#38bdf8", "Airborne"],
            ["#8798b0", "On the ground"],
          ]}
        />
        <p className="text-[12px] leading-snug text-muted-foreground">
          Community receiver coverage around Cyprus is partial, especially on quiet evenings.
        </p>
      </div>
    )
  },
})

registerSystemLayer({
  id: "cynap_its",
  group: "sensors",
  order: 65,
  label: "PWD traffic detectors & CCTV",
  useOn: () => useSkyLayers((s) => s.its.on),
  setOn: setItsOn,
  Summary: function Summary() {
    const its = useSkyLayers((s) => s.its)
    if (!its.on) return <>13 PWD CCTV sites and 39 urban loop detectors</>
    if (its.status === "loading" && !its.counts) return <>Loading CyNAP site inventory</>
    if (its.status === "unavailable") return <>Couldn't load CyNAP sites{its.error ? `: ${its.error}` : ""}</>
    if (!its.counts) return <>Loading CyNAP site inventory</>
    const { cctv, vehicle_detectors: veh, cycle_detectors: cyc } = its.counts
    const sw = its.swarco
    const swNote = sw?.has_values
      ? `${sw.measured_count} live Swarco flow values`
      : sw
        ? `Swarco publishes ${sw.site_refs} site references without flow counts yet`
        : "Static site inventory"
    return <>{`${fmtCount(cctv)} CCTV, ${fmtCount(veh)} vehicle and ${fmtCount(cyc)} cycle detectors. ${swNote}`}</>
  },
  Legend: function Legend() {
    const sw = useSkyLayers((s) => s.its.swarco)
    return (
      <div className="flex flex-col gap-1.5">
        <DotLegend
          label="PWD traffic detectors and CCTV"
          items={[
            ["#0ea5e9", "CCTV camera site"],
            ["#f59e0b", "Vehicle loop detector"],
            ["#10b981", "Cycle detector"],
          ]}
        />
        <p className="text-[12px] leading-snug text-muted-foreground">
          Static PWD site inventory (no live video).{" "}
          {sw?.has_values
            ? `Swarco live counts are active (${sw.measured_count} values).`
            : `Swarco currently sends ${sw?.site_refs ?? 92} site references with no flow counts.`}
        </p>
      </div>
    )
  },
})

function passDayLabel(iso: string, nowMs: number) {
  const dayFmt = (d: Date) =>
    d.toLocaleDateString("en-CA", { timeZone: "Asia/Nicosia" })
  const passDate = new Date(iso)
  const todayStr = dayFmt(new Date(nowMs))
  const tomorrowStr = dayFmt(new Date(nowMs + 86_400_000))
  const passStr = dayFmt(passDate)
  const hm = passDate.toLocaleTimeString("en-GB", {
    timeZone: "Asia/Nicosia",
    hour: "2-digit",
    minute: "2-digit",
  })
  if (passStr === todayStr) return `Today at ${hm}`
  if (passStr === tomorrowStr) return `Tomorrow at ${hm}`
  const weekday = passDate.toLocaleDateString("en-GB", {
    timeZone: "Asia/Nicosia",
    weekday: "short",
    day: "numeric",
    month: "short",
  })
  return `${weekday} at ${hm}`
}

function passText(pass: ISSPass, nowMs: number) {
  return `${passDayLabel(pass.start, nowMs)}, rising ${pass.duration_min} min (${pass.start_dir} to ${pass.end_dir})`
}

function OutsideISS() {
  const iss = useSkyLayers((s) => s.iss)
  const at = useClock((s) => s.at)
  const now = useClock((s) => s.now)
  const refMs = at ?? now

  return (
    <section aria-label="International Space Station" className="flex flex-col gap-2 border-t border-border/60 pt-2.5">
      <div className="flex flex-col gap-0.5" aria-live="polite">
        <span className="font-condensed text-[12px] text-muted-foreground">
          {at !== null ? "Next visible ISS pass after then" : "Next visible ISS pass"}
        </span>
        {(iss.status === "idle" || iss.status === "loading") && !iss.nextPass ? (
          <div className="flex flex-col gap-1.5 pt-0.5" aria-label="Computing ISS passes">
            <Skeleton className="h-4 w-44" />
            <Skeleton className="h-3 w-32" />
          </div>
        ) : iss.position?.visible_now ? (
          <div className="text-[13.5px] leading-snug">
            <p className="font-medium text-sodium">
              Visible overhead now, <span className="font-readout text-[15px] font-semibold">{Math.round(iss.position.elevation_deg)}°</span> {iss.position.direction}
            </p>
            {iss.nextPass && (
              <p className="text-[12.5px] text-muted-foreground">
                Peak {iss.nextPass.max_elevation_deg}°, {iss.nextPass.start_dir} to {iss.nextPass.end_dir}
              </p>
            )}
          </div>
        ) : iss.nextPass ? (
          <div className="text-[13.5px] leading-snug">
            <p>
              <span className="font-readout text-[16px] font-semibold">{iss.nextPass.max_elevation_deg}°</span> peak,{" "}
              {passText(iss.nextPass, refMs)}
            </p>
          </div>
        ) : iss.status === "unavailable" ? (
          <p className="text-[13px] leading-snug text-muted-foreground">
            Orbit elements couldn't be read right now.
          </p>
        ) : (
          <p className="text-[13px] leading-snug text-muted-foreground">
            No twilight visible pass over Nicosia in the next few days.
          </p>
        )}
      </div>
      <FromSources ids={["celestrak_iss"]} />
    </section>
  )
}

registerTopicSection({ id: "outside-iss", topic: "outside", order: 20, Section: OutsideISS })
