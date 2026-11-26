/* The inspector's "What's here?" for a spot: six short sections about the point (address, buses,
   parking, dangerous buildings, the building under the pin, street lights after dark), each
   loaded on its own with its own skeleton, its own plain message when it fails and a way to try
   again. Every row that names a thing opens it, as the inspector's relations do. */
import {
  BusFrontIcon, Building2Icon, ChevronRightIcon, HistoryIcon, LampIcon, LocateFixedIcon, MapPinIcon,
  RotateCwIcon, SquareParkingIcon, TriangleAlertIcon, type LucideIcon,
} from "lucide-react"
import { useEffect, useRef, useState } from "react"

import { LinePill, Row, RowList } from "@/components/mobility/parts"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { metres } from "@/lib/entities"
import { SECTION_KEYS, fetchHere, type HereRow, type HereSection, type SectionKey } from "@/lib/here-data"
import { inspect, useInspector } from "@/lib/inspector"
import { setMyLocation, useMapUi } from "@/lib/map/engine"
import { registerInspectorSection, type SectionProps } from "@/lib/registries/inspector-sections"
import { cn } from "@/lib/utils"

const ICONS: Record<SectionKey, LucideIcon> = {
  address: MapPinIcon, buses: BusFrontIcon, parking: SquareParkingIcon, registers: TriangleAlertIcon,
  building: Building2Icon, lamps: LampIcon,
}
const TITLES: Record<SectionKey, string> = {
  address: "Address", buses: "Buses", parking: "Parking", registers: "Dangerous buildings",
  building: "Building", lamps: "Street lights",
}
const BUS_REFRESH_MS = 30_000

type Load = { id: string; status: "loading" | "ready" | "error"; section: HereSection | null; error: string | null }

// One section of the open spot at the clock's moment. A refresh (buses, every 30 s while live)
// keeps what is shown until the new one comes; a new spot or moment shows the skeleton again.
function useSection(id: string, key: SectionKey, moment: string | null) {
  const [load, setLoad] = useState<Load>({ id, status: "loading", section: null, error: null })
  const [asked, setAsked] = useState(0)
  const shown = useRef<string>("")
  useEffect(() => {
    const ctrl = new AbortController()
    const at = `${id}|${moment ?? ""}`
    const same = shown.current === at
    setLoad((l) => (same && l.section ? l : { id, status: "loading", section: null, error: null }))
    fetchHere(id, key, moment, ctrl.signal, asked > 0)
      .then((section) => { shown.current = at; setLoad({ id, status: "ready", section, error: null }) })
      .catch((e: Error) => {
        if (ctrl.signal.aborted || e.name === "AbortError") return
        setLoad({ id, status: "error", section: null, error: e.message })
      })
    return () => ctrl.abort()
  }, [id, key, moment, asked])
  useEffect(() => {
    if (key !== "buses" || moment) return
    const t = window.setInterval(() => { if (!document.hidden) setAsked((n) => n + 1) }, BUS_REFRESH_MS)
    return () => window.clearInterval(t)
  }, [id, key, moment])
  return { ...load, retry: () => setAsked((n) => n + 1) }
}

function openRow(r: HereRow) {
  if (!r.id) return
  inspect({ id: r.id, at: r.lon !== undefined && r.lat !== undefined ? [r.lon, r.lat] : undefined }, { fly: true, focus: true })
}

function Block({ k, busy, live, children }: { k: SectionKey; busy?: boolean; live?: boolean; children: React.ReactNode }) {
  const Icon = ICONS[k]
  return (
    <section aria-labelledby={`here-${k}`} aria-busy={busy || undefined} aria-live={live ? "polite" : undefined} className="flex flex-col gap-2">
      <h3 id={`here-${k}`} className="flex items-center gap-1.5 text-[12.5px] text-muted-foreground">
        <Icon className="size-3.5" aria-hidden="true" />
        {TITLES[k]}
      </h3>
      {children}
    </section>
  )
}

function Lines({ wide = "w-3/4" }: { wide?: string }) {
  return (
    <div className="flex flex-col gap-1.5" aria-label="Loading">
      <Skeleton className={cn("h-4", wide)} />
      <Skeleton className="h-3 w-1/2" />
    </div>
  )
}

function From({ sources }: { sources: HereSection["sources"] }) {
  if (!sources.length) return null
  return (
    <p className="text-[11.5px] leading-snug text-muted-foreground">
      From{" "}
      {sources.map((s, i) => (
        <span key={s.name} lang={/[Ͱ-Ͽ]/.test(s.name) ? "el" : undefined}>
          {i > 0 && (i === sources.length - 1 ? " and " : ", ")}
          {s.url ? (
            <a href={s.url} target="_blank" rel="noreferrer" className="underline decoration-line underline-offset-2 hover:text-sodium">{s.name}</a>
          ) : s.name}
        </span>
      ))}
    </p>
  )
}

function Note({ children, past }: { children: React.ReactNode; past?: boolean }) {
  return (
    <p className="flex gap-1.5 text-[12.5px] leading-snug text-muted-foreground">
      {past && <HistoryIcon className="mt-0.5 size-3.5 shrink-0" aria-hidden="true" />}
      <span>{children}</span>
    </p>
  )
}

// A list of things that each open in the inspector: Tab enters, arrows move, Enter opens.
function Things({ label, rows, icon: Icon }: { label: string; rows: HereRow[]; icon: LucideIcon }) {
  if (!rows.length) return null
  return (
    <RowList label={label}>
      {rows.map((r, i) => (
        <Row key={`${r.id}:${i}`} rowKey={`${r.id}:${i}`} onSelect={() => openRow(r)}
          label={[r.name, r.detail, r.distance_m !== undefined && r.distance_m > 0 ? `${metres(r.distance_m)} away` : null].filter(Boolean).join(", ")}>
          <span className="relative flex size-6 shrink-0 items-center justify-center">
            <Icon className="size-4 text-muted-foreground" aria-hidden="true" />
            {r.color && <span className="absolute -right-0.5 -bottom-0.5 size-2 rounded-full ring-2 ring-console" style={{ background: r.color }} aria-hidden="true" />}
          </span>
          <span className="flex min-w-0 flex-1 flex-col">
            <span className="truncate text-[13.5px]" lang={/[Ͱ-Ͽ]/.test(r.name ?? "") ? "el" : undefined}>{r.name}</span>
            {(r.detail || r.distance_m) ? (
              <span className="truncate text-[12px] text-muted-foreground">
                {[r.detail, r.distance_m ? metres(r.distance_m) : null].filter(Boolean).join(", ")}
              </span>
            ) : null}
          </span>
          <ChevronRightIcon className="size-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
        </Row>
      ))}
    </RowList>
  )
}

// -- each section ------------------------------------------------------------------------------

function Address({ s, at }: { s: HereSection; at: [number, number] | null }) {
  const me = useMapUi((u) => u.me.loc)
  const isMe = !!me && !!at && Math.abs(me.lat - at[1]) < 1e-6 && Math.abs(me.lon - at[0]) < 1e-6
  return (
    <>
      {s.summary && (
        <div className="flex items-start gap-2">
          <div className="flex min-w-0 flex-1 flex-col gap-0.5">
            <p className="text-[15px] leading-snug font-medium" lang={/[Ͱ-Ͽ]/.test(s.summary) ? "el" : undefined}>{s.summary}</p>
            {s.detail && <p className="text-[12.5px] text-muted-foreground">{s.detail}</p>}
          </div>
          {at && (
            <Button variant="outline" size="xs" className="shrink-0" disabled={isMe}
              onClick={() => setMyLocation(at[1], at[0], NaN, "set on the map")}>
              <LocateFixedIcon data-icon="inline-start" />
              {isMe ? "You're here" : "I'm here"}
            </Button>
          )}
        </div>
      )}
      {s.message && <Note>{s.message}</Note>}
    </>
  )
}

function Buses({ s }: { s: HereSection }) {
  return (
    <>
      {s.summary && <p className="text-[14px] leading-snug">{s.summary}</p>}
      {s.rows.length > 0 && (
        <RowList label="Nearest bus stops">
          {s.rows.map((r, i) => (
            <Row key={r.id} rowKey={r.id ?? String(i)} onSelect={() => openRow(r)}
              label={`Stop ${r.name}, ${metres(r.distance_m)} away${r.departures?.length ? `, next: ${r.departures.map((d) => `${d.line} ${d.in_min === 0 ? "now" : `in ${d.in_min} minutes`}${d.realtime ? ", live" : ""}`).join("; ")}` : ""}`}>
              <BusFrontIcon className="size-4 shrink-0 self-start text-muted-foreground" aria-hidden="true" />
              <span className="flex min-w-0 flex-1 flex-col gap-1">
                <span className="flex items-baseline gap-2">
                  <span className="truncate text-[13.5px]">{r.name}</span>
                  <span className="shrink-0 font-condensed text-[12px] text-muted-foreground">{metres(r.distance_m)}</span>
                </span>
                {r.departures && r.departures.length > 0 ? (
                  <span className="flex flex-wrap items-center gap-x-2.5 gap-y-1">
                    {r.departures.map((d, j) => (
                      <span key={j} className="inline-flex items-center gap-1">
                        <LinePill line={d.line} color={d.color} textColor={d.text_color} />
                        <span className={cn("font-readout text-[13px] tabular-nums", d.realtime && "text-sodium")}>
                          {d.in_min === 0 ? "now" : `${d.in_min} min`}
                        </span>
                      </span>
                    ))}
                  </span>
                ) : r.departures ? (
                  <span className="text-[12px] text-muted-foreground">No buses in the next hour</span>
                ) : r.lines?.length ? (
                  <span className="text-[12px] text-muted-foreground">Lines {r.lines.slice(0, 6).join(", ")}</span>
                ) : null}
              </span>
              <ChevronRightIcon className="size-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
            </Row>
          ))}
        </RowList>
      )}
      {s.message && <Note past={s.live_only}>{s.message}</Note>}
      {!s.live_only && s.rows.some((r) => r.departures?.length) && (
        <p className="text-[11.5px] text-muted-foreground">Minutes in sodium are live, from the bus on the road; the others are the timetable's.</p>
      )}
    </>
  )
}

function Facts({ facts }: { facts: HereSection["facts"] }) {
  if (!facts.length) return null
  return (
    <dl className="grid grid-cols-2 gap-x-4 gap-y-2">
      {facts.map((f) => (
        <div key={f.label} className="flex min-w-0 flex-col gap-0.5">
          <dt className="truncate font-condensed text-[12px] text-muted-foreground">{f.label}</dt>
          <dd className="truncate font-readout text-[15px] leading-tight font-semibold">{f.value}</dd>
        </div>
      ))}
    </dl>
  )
}

function Body({ s, at, past }: { s: HereSection; at: [number, number] | null; past: boolean }) {
  switch (s.key) {
    case "address":
      return <Address s={s} at={at} />
    case "buses":
      return <Buses s={s} />
    default: {
      const icon = s.key === "parking" ? SquareParkingIcon : s.key === "lamps" ? LampIcon
        : s.key === "registers" ? TriangleAlertIcon : Building2Icon
      const counts = s.key === "parking" || s.key === "lamps"
      return (
        <>
          {s.summary && <p className={cn("text-[14px] leading-snug", s.status === "none" && "text-muted-foreground")}>{s.summary}</p>}
          {s.key === "building" && <Facts facts={s.facts.filter((f) => f.label !== "Use")} />}
          <Things label={TITLES[s.key]} rows={s.rows} icon={icon} />
          {counts && s.facts.length > 0 && (
            <p className="font-condensed text-[12.5px] text-muted-foreground tabular-nums">
              {s.facts.map((f) => `${f.value} ${f.label.toLowerCase()}`).join(", ")}
            </p>
          )}
          {s.message && <Note past={past && s.status === "unknown"}>{s.message}</Note>}
          {s.note && <Note>{s.note}</Note>}
        </>
      )
    }
  }
}

function HereBlock({ id, k, at, moment }: { id: string; k: SectionKey; at: [number, number] | null; moment: string | null }) {
  const { status, section, error, retry } = useSection(id, k, moment)
  if (!section) {
    return (
      <Block k={k} busy={status === "loading"}>
        {status === "error" ? (
          <div className="flex flex-col items-start gap-2">
            <Note>Couldn't read this: {error}. Check the connection and try again.</Note>
            <Button variant="outline" size="xs" onClick={retry}><RotateCwIcon data-icon="inline-start" />Try again</Button>
          </div>
        ) : <Lines wide={k === "address" ? "w-2/3" : "w-3/4"} />}
      </Block>
    )
  }
  return (
    <Block k={k} live={k === "buses"}>
      <Body s={section} at={at} past={!!moment} />
      {section.status === "error" ? (
        <Button variant="outline" size="xs" className="self-start" onClick={retry}><RotateCwIcon data-icon="inline-start" />Try again</Button>
      ) : <From sources={section.sources} />}
    </Block>
  )
}

function HereSections({ entity }: SectionProps) {
  const moment = useInspector((s) => s.moment)
  const at = entity.center
  return (
    <div className="flex flex-col gap-5">
      {SECTION_KEYS.map((k) => <HereBlock key={k} id={entity.id} k={k} at={at} moment={moment} />)}
      {entity.pending && (
        <section aria-label="Sun, shade, air and weather" aria-busy="true" className="flex flex-col gap-2">
          <h3 className="text-[12.5px] text-muted-foreground">Sun, shade, air and weather</h3>
          <div className="grid grid-cols-2 gap-3"><Skeleton className="h-9" /><Skeleton className="h-9" /><Skeleton className="h-9" /><Skeleton className="h-9" /></div>
        </section>
      )}
    </div>
  )
}

registerInspectorSection({ id: "whats-here", order: 11, kinds: ["spot"], Component: HereSections })
