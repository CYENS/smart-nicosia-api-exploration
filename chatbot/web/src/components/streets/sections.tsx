/* The inspector's sections for the street rules: how sure a speed limit's stretch is, how a
   school is served for crossing, and, for every inventory, that it stands at every moment of
   the clock and how old it is. Readings, facts, relations and sources come from the generic
   sections. */
import { Badge } from "@/components/ui/badge"
import type { Entity } from "@/lib/entities"
import { useMapUi } from "@/lib/map/engine"
import { registerInspectorSection, type SectionProps } from "@/lib/registries/inspector-sections"
import { LIMIT_COLORS } from "@/lib/streets/map"
import { inventoryDay } from "@/lib/streets/store"

type Limit = { kmh: number; tiers: Record<string, number>; alt_kmh: number[] }
type Crossing = {
  verdict: string; nearest_crossing_m: number | null; nearest_signal_m: number | null; main_road: string | null
  main_road_m: number | null; main_road_limit_kmh: number | null; zone: string | null
}
type StreetEntity = Entity & { limit?: Limit; crossing?: Crossing; inventory?: boolean }

const TIERS: { key: string; label: string; opacity: number; dashed?: boolean }[] = [
  { key: "signed", label: "Posted", opacity: 0.9 },
  { key: "likely", label: "Likely", opacity: 0.5 },
  { key: "uncertain", label: "Changes somewhere", opacity: 0.8, dashed: true },
  { key: "mapped", label: "Mapped by OpenStreetMap", opacity: 0.4 },
]

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section aria-label={title} className="flex flex-col gap-2.5">
      <h3 className="text-[12.5px] text-muted-foreground">{title}</h3>
      {children}
    </section>
  )
}

function LimitSection({ entity }: SectionProps) {
  const e = entity as StreetEntity
  const base = useMapUi((s) => s.basemap)
  const lim = e.limit
  if (!lim) return null
  const total = Object.values(lim.tiers).reduce((a, b) => a + b, 0)
  const color = LIMIT_COLORS[base][lim.kmh] ?? LIMIT_COLORS[base][50]
  if (!total) {
    return (
      <Section title="The stretch it governs">
        <p className="text-[12.5px] text-muted-foreground">Not placed on a street: the nearest is more than 35 m from where the inventory puts the sign.</p>
      </Section>
    )
  }
  return (
    <Section title="The stretch it governs">
      <div className="flex h-2.5 w-full overflow-hidden rounded-full bg-muted" role="img"
           aria-label={TIERS.filter((t) => lim.tiers[t.key]).map((t) => `${t.label} ${Math.round(lim.tiers[t.key])} m`).join(", ")}>
        {TIERS.filter((t) => lim.tiers[t.key]).map((t) => (
          <span key={t.key} style={{ width: `${(100 * lim.tiers[t.key]) / total}%`, opacity: t.opacity,
                                     background: t.dashed ? `repeating-linear-gradient(90deg, ${color} 0 5px, transparent 5px 8px)` : color }} />
        ))}
      </div>
      <ul className="flex flex-wrap gap-x-3.5 gap-y-1 text-[12.5px] text-muted-foreground">
        {TIERS.filter((t) => lim.tiers[t.key]).map((t) => (
          <li key={t.key}><span className="font-readout font-semibold text-foreground">{Math.round(lim.tiers[t.key])} m</span> {t.label.toLowerCase()}</li>
        ))}
      </ul>
      {lim.alt_kmh.length > 0 && (
        <p className="text-[12.5px] leading-snug text-muted-foreground">
          Between this sign and the next, the limit changes to {lim.alt_kmh.join(" or ")} km/h somewhere: the inventory doesn't say where, nor which way each sign faces.
        </p>
      )}
    </Section>
  )
}

const VERDICT: Record<string, { text: string; tone: string }> = {
  served: { text: "A crossing or signals within a short walk", tone: "border-line text-foreground" },
  gap: { text: "Beside a fast main road, nothing to cross at near", tone: "border-signal/40 text-signal" },
  slow_road: { text: "Beside a slower main road, no crossing near", tone: "border-line text-foreground" },
  quiet: { text: "Side streets only", tone: "text-muted-foreground" },
}

function CrossingSection({ entity }: SectionProps) {
  const c = (entity as StreetEntity).crossing
  if (!c) return null
  const v = VERDICT[c.verdict] ?? VERDICT.quiet
  const m = (n: number | null) => (n === null ? "None within 250 m" : `${n} m`)
  return (
    <Section title="Crossing to it">
      <Badge variant="outline" className={`h-auto self-start whitespace-normal px-2.5 py-1 text-[12.5px] ${v.tone}`}>{v.text}</Badge>
      <dl className="grid grid-cols-3 gap-2 text-[12.5px]">
        <div><dt className="text-muted-foreground">Crossing</dt><dd className="font-readout text-[15px] font-semibold">{m(c.nearest_crossing_m)}</dd></div>
        <div><dt className="text-muted-foreground">Signals</dt><dd className="font-readout text-[15px] font-semibold">{m(c.nearest_signal_m)}</dd></div>
        <div><dt className="text-muted-foreground">Main road</dt><dd className="font-readout text-[15px] font-semibold">{c.main_road_limit_kmh ? `${c.main_road_limit_kmh} km/h` : c.main_road ? "Limit unknown" : "None"}</dd></div>
      </dl>
      <p className="text-[12.5px] leading-snug text-muted-foreground">
        {c.main_road ? `${c.main_road} runs ${c.main_road_m} m away. ` : ""}Distances are straight lines, not walks{c.zone ? `; it lies in ${c.zone}` : ""}.
      </p>
    </Section>
  )
}

function InventoryNote({ entity }: SectionProps) {
  const e = entity as StreetEntity
  if (!e.inventory) return null
  const when = inventoryDay(e.sources.find((s) => s.as_of)?.as_of ?? null)
  return (
    <p className="text-[12px] leading-snug text-muted-foreground">
      From a national inventory, not a live reading: it stands at every moment of the clock{when ? `. Updated by its publisher on ${when}` : ""}.
    </p>
  )
}

const STREET_KINDS = ["speed_limit", "slow_zone", "slow_zone_entrance", "traffic_signal", "pedestrian_crossing",
                      "enforcement_camera", "cycleway", "bus_lane"]

registerInspectorSection({ id: "street-limit", order: 14, kinds: ["speed_limit"], Component: LimitSection })
registerInspectorSection({ id: "street-school", order: 14, kinds: ["school"], Component: CrossingSection })
registerInspectorSection({ id: "street-inventory", order: 85, kinds: STREET_KINDS, Component: InventoryNote })
