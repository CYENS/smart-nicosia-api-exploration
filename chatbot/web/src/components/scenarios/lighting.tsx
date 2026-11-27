/* The street lighting scenario's own sections in the lab: what the lamps do today (the schedule
   the twin learnt from them) and the safety check, each flagged lamp a row that flies to it. */
import { Row, RowList } from "@/components/mobility/parts"
import { registerParamGroups, registerScenarioSection, type ScenarioRun } from "@/lib/registries/scenarios"
import { flyToScenarioFeature } from "@/lib/scenarios/map"
import { fmtNum } from "@/lib/scenarios/store"

registerParamGroups({
  switching: "Switching by the sun", dimming: "Dimming", part_night: "Quiet streets off late at night",
  retrofit: "LED retrofit", safety: "Safety check",
})

function minutes(v: number, after: string) {
  const m = Math.round(v)
  if (m === 0) return `at ${after}`
  return `${Math.abs(m)} min ${m < 0 ? "before" : "after"} ${after}`
}

function Today({ run }: { run: ScenarioRun }) {
  const s = run.result.details?.summary
  if (!s) return null
  const basis: Record<string, number> = s.schedule.basis ?? {}
  const classes: { watts: number; lamps: number }[] = run.result.details?.wattage_classes ?? []
  return (
    <div className="flex flex-col gap-2 text-[13px] leading-snug">
      <p>
        Half the lamps switch on {minutes(s.schedule.on_min_after_sunset, "sunset")} and off {minutes(s.schedule.off_min_after_sunrise, "sunrise")},
        lit about <span className="font-readout font-semibold tabular-nums">{fmtNum(s.schedule.lit_hours_a_night_now, 1)}</span> hours a night in the period.
      </p>
      <p className="text-[12.5px] text-muted-foreground">
        {s.modelled} of {s.lamps} lamps modelled{s.without_power ? `; ${s.without_power} reported no power` : ""}. Switching times:
        {" "}{basis.measured ?? 0} read from 10-minute reports, {basis.meter ?? 0} from their energy meters, {basis.city ?? 0} assumed like the closely read lamps.
      </p>
      {classes.length > 0 && (
        <p className="text-[12.5px] text-muted-foreground">
          Power at full light: {classes.map((c) => `${c.lamps} at about ${c.watts} W`).join(", ")}.
        </p>
      )}
    </div>
  )
}

type Flag = { id: string; street: string; label: string; reasons: string[]; lamp_hours: number; people_an_hour: number | null }

function Safety({ run }: { run: ScenarioRun }) {
  const rows: Flag[] = run.result.details?.safety ?? []
  const unmeasured: string[] = run.result.details?.unmeasured ?? []
  if (!rows.length) {
    return (
      <p className="text-[13px] text-muted-foreground">
        No dimmed or dark lamp is on a street with night pedestrians, near a bus stop or near a dangerous building.
        {unmeasured.length ? ` ${unmeasured.length} dimmed or dark lamps are on streets no camera counts.` : ""}
      </p>
    )
  }
  return (
    <div className="flex flex-col gap-2">
      <p className="text-[13px] leading-snug">
        {rows.length} dimmed or dark lamps are flagged{unmeasured.length ? `, and ${unmeasured.length} are on streets no camera counts` : ""}.
      </p>
      <RowList label="Flagged lamps">
        {rows.slice(0, 40).map((r) => (
          <Row key={r.id} rowKey={r.id} onSelect={() => flyToScenarioFeature(r.id)}
            label={`${r.label}, ${r.reasons.join(", ").toLowerCase()}, ${r.lamp_hours} lamp-hours a year`}>
            <span className="min-w-0 flex-1">
              <span className="block truncate text-[13px]">{r.label}</span>
              <span className="block truncate font-condensed text-[12px] text-muted-foreground">
                {r.reasons.join(", ")}{r.people_an_hour !== null ? `, ${fmtNum(r.people_an_hour, 1)} people an hour` : ""}
              </span>
            </span>
            <span className="shrink-0 font-readout text-[13px] tabular-nums">{fmtNum(r.lamp_hours)}<span className="text-muted-foreground"> h</span></span>
          </Row>
        ))}
      </RowList>
      {rows.length > 40 && <p className="text-[12px] text-muted-foreground">The first 40 of {rows.length}; the brief lists them by street.</p>}
    </div>
  )
}

registerScenarioSection({ scenario: "lighting", id: "today", title: "What the lamps do today", order: 10, Section: Today })
registerScenarioSection({ scenario: "lighting", id: "safety", title: "Safety check", order: 20, Section: Safety })
