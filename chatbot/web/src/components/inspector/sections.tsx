/* The inspector's built-in sections, registered by kind and order. */
import { ChevronRightIcon, ExternalLinkIcon, LandPlotIcon, SearchIcon } from "lucide-react"

import { Sparkline } from "@/components/inspector/sparkline"
import { Item, ItemActions, ItemContent, ItemDescription, ItemGroup, ItemMedia, ItemTitle } from "@/components/ui/item"
import { Marker, MarkerContent } from "@/components/ui/marker"
import { Skeleton } from "@/components/ui/skeleton"
import { ago, clockTime, kindMeta, metres, type Entity, type Reading, type Relation } from "@/lib/entities"
import { useNow } from "@/lib/hooks"
import { inspect } from "@/lib/inspector"
import { openSearch } from "@/lib/layout"
import { registerInspectorSection, type SectionProps } from "@/lib/registries/inspector-sections"
import { cn } from "@/lib/utils"

function Section({ title, children, className }: { title: string; children: React.ReactNode; className?: string }) {
  return (
    <section aria-label={title} className={cn("flex flex-col gap-2.5", className)}>
      <h3 className="text-[12.5px] text-muted-foreground">{title}</h3>
      {children}
    </section>
  )
}

function value(r: Reading) {
  const v = typeof r.value === "number" ? r.value.toLocaleString("en-GB", { maximumFractionDigits: 1 }) : r.value
  return { v, unit: r.unit }
}

// The reading that says the most about each kind, read out large.
const HEADLINE: Record<string, string> = {
  street_light: "power_w", waste_bin: "level_pct", air_station: "caqi",
  building_3d: "height_m", register_building: "height_m",
}

function headlineOf(e: Entity): Reading | null {
  const key = HEADLINE[e.kind]
  const r = e.readings.find((x) => x.key === key)
  if (!r) return null
  if (e.kind === "street_light" && e.state?.value !== "on") return null // a dark lamp's power is last night's
  return r
}

// The headline: one number, large, with its age, over the last 24 hours.
function Headline({ entity, series, seriesStatus, band }: SectionProps) {
  const now = useNow()
  const r = headlineOf(entity)
  const m = entity.moment?.applies ? entity.moment : null
  const live = !!r?.as_of && !m
  const today = series?.totals_24h
  const counted = today && (today.people ?? today.total)
  return (
    <div className="flex flex-col gap-3">
      {r && (
        <div className="flex flex-col gap-1">
          <p className="flex items-baseline gap-1.5 font-readout leading-none">
            <span className={cn("text-[40px] font-semibold tracking-[-0.01em]", live ? "text-sodium" : "text-foreground")}>{value(r).v}</span>
            {r.unit && <span className="text-[17px] text-muted-foreground">{r.unit}</span>}
          </p>
          <p className="text-[13px] text-muted-foreground">
            {r.label}{m ? `, at ${m.label}` : r.as_of ? `, reported ${ago(r.as_of, now)}` : ""}
          </p>
        </div>
      )}
      {!r && counted !== undefined && (
        <div className="flex flex-col gap-1">
          <p className="font-readout text-[40px] leading-none font-semibold text-sodium">{counted.toLocaleString("en-GB")}</p>
          <p className="text-[13px] text-muted-foreground">{today?.people !== undefined ? "People counted in the last 24 hours" : "Counted in the last 24 hours"}</p>
        </div>
      )}
      {m && !m.known && (
        <p className="text-[13.5px] leading-snug text-muted-foreground">The twin's memory holds nothing for {m.label}, so its state then is unknown. The line below shows what it does hold.</p>
      )}
      {!r && !counted && entity.state?.reason && entity.kind === "street_light" && !(m && !m.known) && (
        <p className="text-[13.5px] leading-snug text-muted-foreground">{capitalise(entity.state.reason)}.</p>
      )}
      {entity.has_series && seriesStatus === "loading" && (
        <div className="flex flex-col gap-1.5" aria-label="Loading the last 24 hours">
          <Skeleton className="h-3.5 w-28" />
          <Skeleton className="h-16 w-full" />
        </div>
      )}
      {series && !series.error && series.points?.length > 1 && (
        <Sparkline series={series} band={band} live={!entity.moment} title={entity.moment ? `${series.label}, 24 hours around ${entity.moment.label}` : `${series.label}, last 24 hours`} />
      )}
      {entity.has_series && (seriesStatus === "error" || series?.error) && (
        <p className="text-[12.5px] text-muted-foreground">The city's API didn't send the last 24 hours. The readings above are live.</p>
      )}
      {entity.has_series && seriesStatus === "ready" && !series && (
        <p className="text-[12.5px] text-muted-foreground">No history from the city's API for the last 24 hours.</p>
      )}
    </div>
  )
}

function capitalise(s: string) {
  return s.charAt(0).toUpperCase() + s.slice(1)
}

function Readings({ entity }: SectionProps) {
  const head = headlineOf(entity)
  const rest = entity.readings.filter((r) => r !== head)
  if (!rest.length) return null
  return (
    <Section title={rest.some((r) => r.as_of) ? "Readings" : "Figures"}>
      <dl className="grid grid-cols-2 gap-x-4 gap-y-3">
        {rest.map((r) => (
          <div key={r.key} className="flex min-w-0 flex-col gap-0.5">
            <dt className="truncate font-condensed text-[12px] text-muted-foreground">{r.label}</dt>
            <dd className="font-readout text-[17px] leading-tight font-semibold">
              {value(r).v}{r.unit && <span className="ml-1 text-[13px] font-normal text-muted-foreground">{r.unit}</span>}
            </dd>
          </div>
        ))}
      </dl>
    </Section>
  )
}

// A register listing, said plainly: what the register calls it, and what it isn't.
function RegisterNote({ entity }: SectionProps) {
  const status = entity.attributes.find((a) => a.label === "Status")?.value
  const register = entity.attributes.find((a) => a.label === "Register")?.value
  return (
    <Marker className="items-start rounded-lg border border-signal/30 bg-signal/[0.06] px-3 py-2.5 text-[13px] leading-snug">
      <MarkerContent>
        Listed as <span lang="el">{status}</span> on the <span lang="el">{register}</span> register. A listing is a case
        on a public register, not an engineering verdict that it will fall.
      </MarkerContent>
    </Marker>
  )
}

const SKIP_ATTRS = new Set(["Status", "Register"])

function Attributes({ entity }: SectionProps) {
  const rows = entity.attributes.filter((a) => !(entity.kind === "register_building" && SKIP_ATTRS.has(a.label)))
  if (!rows.length) return null
  return (
    <Section title="About">
      <dl className="grid grid-cols-[minmax(0,38%)_1fr] gap-x-3 gap-y-1.5 text-[13.5px] leading-snug">
        {rows.map((a) => (
          <div key={a.label} className="contents">
            <dt className="text-muted-foreground">{a.label}</dt>
            <dd className="min-w-0 break-words" lang={/[Ͱ-Ͽ]/.test(String(a.value)) ? "el" : undefined}>
              {isUrl(a.value) ? (
                <a href={String(a.value)} target="_blank" rel="noreferrer" className="underline decoration-sodium underline-offset-3 hover:text-sodium">
                  {String(a.value).replace(/^https?:\/\/(www\.)?/, "").replace(/\/$/, "")}
                </a>
              ) : String(a.value)}
            </dd>
          </div>
        ))}
      </dl>
    </Section>
  )
}

function isUrl(v: unknown) {
  return typeof v === "string" && /^https?:\/\//.test(v)
}

function RelationRow({ r }: { r: Relation }) {
  const meta = kindMeta(r.relation === "street" ? "street" : r.kind)
  const Icon = r.relation === "parcel" ? LandPlotIcon : meta.icon
  const detail = [r.label, r.detail, metres(r.distance_m)].filter(Boolean).join(", ")
  const media = (
    <ItemMedia variant="icon" className="relative">
      <Icon className="text-muted-foreground" />
      {r.color && <span className="absolute -right-0.5 -bottom-0.5 size-2 rounded-full ring-2 ring-console" style={{ background: r.color }} aria-hidden="true" />}
    </ItemMedia>
  )
  const body = (
    <ItemContent className="min-w-0">
      <ItemTitle className="line-clamp-1 w-full" lang={/[Ͱ-Ͽ]/.test(r.name ?? "") ? "el" : undefined}>{r.name}</ItemTitle>
      <ItemDescription className="line-clamp-1 text-[12.5px]">{detail}</ItemDescription>
    </ItemContent>
  )
  // A list item that holds a button (the button keeps its role, so it is announced as one).
  if (r.id) {
    return (
      <div role="listitem">
        <Item
          size="xs"
          className="cursor-pointer text-left hover:bg-accent/60"
          render={<button type="button" onClick={() => inspect({ id: r.id!, at: r.lon !== undefined ? [r.lon, r.lat!] : undefined }, { fly: true, focus: true })} />}
        >
          {media}{body}
          <ItemActions><ChevronRightIcon className="size-4 text-muted-foreground" aria-hidden="true" /></ItemActions>
        </Item>
      </div>
    )
  }
  if (r.relation === "street" && r.name) {
    return (
      <div role="listitem">
        <Item
          size="xs"
          className="cursor-pointer text-left hover:bg-accent/60"
          aria-label={`Search ${r.name}`}
          render={<button type="button" onClick={() => openSearch(r.name!)} />}
        >
          {media}{body}
          <ItemActions><SearchIcon className="size-4 text-muted-foreground" aria-hidden="true" /></ItemActions>
        </Item>
      </div>
    )
  }
  return <div role="listitem"><Item size="xs">{media}{body}</Item></div>
}


function Relations({ entity }: SectionProps) {
  if (!entity.relations.length) return null
  return (
    <Section title="Around it">
      <ItemGroup className="gap-0.5 -mx-2.5 w-auto">
        {entity.relations.map((r, i) => <RelationRow key={`${r.relation}:${r.id ?? r.name}:${i}`} r={r} />)}
      </ItemGroup>
    </Section>
  )
}

function Sources({ entity }: SectionProps) {
  if (!entity.sources.length) return null
  return (
    <Section title="Where this comes from">
      <ul className="flex flex-col gap-1.5 text-[12.5px] leading-snug text-muted-foreground">
        {entity.sources.map((s) => (
          <li key={s.name} lang={/[Ͱ-Ͽ]/.test(s.name) ? "el" : undefined}>
            {s.url ? (
              <a href={s.url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-foreground underline decoration-line underline-offset-3 hover:text-sodium">
                {s.name}<ExternalLinkIcon className="size-3" aria-hidden="true" />
              </a>
            ) : <span className="text-foreground">{s.name}</span>}
            {s.detail && <span>, {s.detail}</span>}
            {s.as_of && <span>, as of {s.as_of.length > 10 ? clockTime(s.as_of) : s.as_of}</span>}
          </li>
        ))}
      </ul>
    </Section>
  )
}

registerInspectorSection({
  id: "headline", order: 10, Component: Headline,
  when: (e) => !!headlineOf(e) || e.has_series || e.kind === "street_light",
})
registerInspectorSection({ id: "readings", order: 20, Component: Readings })
registerInspectorSection({ id: "register", order: 30, kinds: ["register_building"], Component: RegisterNote })
registerInspectorSection({ id: "attributes", order: 40, Component: Attributes })
registerInspectorSection({ id: "relations", order: 50, Component: Relations })
registerInspectorSection({ id: "sources", order: 90, Component: Sources })
