/* Tonight in Nicosia UI rows:
   - Layers sheet group & filterable switch ("Tonight in Nicosia")
   - Outside topic card section with On duty / Events / Open now tabs, loading/empty/error states,
     accessible row buttons, and click-to-inspect + map sync. */
import { CalendarIcon, MapPinIcon, PhoneIcon, RefreshCwIcon } from "lucide-react"
import { useEffect, useId, useState } from "react"

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Empty, EmptyDescription, EmptyHeader, EmptyTitle } from "@/components/ui/empty"
import { Field, FieldLabel } from "@/components/ui/field"
import { Skeleton } from "@/components/ui/skeleton"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { useClock } from "@/lib/clock"
import { inspect } from "@/lib/inspector"
import { skyAt } from "@/lib/map/sky"
import { registerLayerGroup, registerSystemLayer } from "@/lib/registries/layers"
import { registerTopicSection } from "@/lib/registries/topics"
import { TONIGHT_COLORS, setTonightLayer } from "@/lib/tonight/map"
import {
  type CityEvent,
  type DutyPharmacy,
  type OpenNowPlace,
  type Precision,
  type TonightFilter,
  loadTonight,
  setTonightFilter,
  useTonight,
} from "@/lib/tonight/store"

const FILTERS: { value: TonightFilter; label: string }[] = [
  { value: "all", label: "All" },
  { value: "pharmacies", label: "Duty" },
  { value: "events", label: "Events" },
  { value: "open_now", label: "Open" },
]

const PRECISION_SHORT: Record<Precision, string | null> = {
  exact: null,
  street: "Street match",
  area: "Area approx.",
  unplaced: "Unplaced",
}

function TonightLegend() {
  const id = useId()
  const filter = useTonight((s) => s.filter)
  return (
    <div className="flex flex-col gap-2">
      <Field>
        <FieldLabel id={id} className="text-[12.5px] font-normal text-muted-foreground">Show on map</FieldLabel>
        <ToggleGroup
          aria-labelledby={id}
          variant="outline"
          size="sm"
          spacing={0}
          className="w-full"
          value={[filter]}
          onValueChange={(v) => {
            if (v[0]) setTonightFilter(v[0] as TonightFilter)
          }}
        >
          {FILTERS.map((f) => (
            <ToggleGroupItem key={f.value} value={f.value} className="flex-1">
              {f.label}
            </ToggleGroupItem>
          ))}
        </ToggleGroup>
      </Field>
      <ul className="flex flex-wrap gap-3 text-[12px] text-muted-foreground">
        <li className="flex items-center gap-1.5">
          <span className="size-2.5 rounded-full" style={{ background: TONIGHT_COLORS.duty }} aria-hidden="true" />
          On duty
        </li>
        <li className="flex items-center gap-1.5">
          <span className="size-2.5 rounded-full" style={{ background: TONIGHT_COLORS.event }} aria-hidden="true" />
          Event
        </li>
        <li className="flex items-center gap-1.5">
          <span className="size-2.5 rounded-full" style={{ background: TONIGHT_COLORS.open }} aria-hidden="true" />
          Open now
        </li>
      </ul>
    </div>
  )
}

registerLayerGroup({ id: "tonight", label: "Tonight in Nicosia", order: 25 })

registerSystemLayer({
  id: "tonight",
  group: "tonight",
  order: 10,
  label: "Tonight in Nicosia",
  useOn: () => useTonight((s) => s.on),
  setOn: setTonightLayer,
  Summary: function Summary() {
    const status = useTonight((s) => s.status)
    const error = useTonight((s) => s.error)
    const data = useTonight((s) => s.data)
    if (status === "loading" && !data) return <>Reading duty pharmacies, events and open places</>
    if (status === "error" && !data) return <>{error ?? "Could not read Tonight in Nicosia"}</>
    if (!data) return <>Duty pharmacies, Visit Cyprus events, and places open now</>
    if (!data.known && data.pharmacies.length === 0) return <>No duty pharmacy roster recorded for this night</>
    return (
      <>
        {data.counts.pharmacies} pharmacies on duty, {data.counts.events} events tonight, {data.counts.open_now} open now
      </>
    )
  },
  Legend: TonightLegend,
})

function openItem(id: string, lat: number | null, lon: number | null) {
  if (!useTonight.getState().on) setTonightLayer(true)
  inspect(
    {
      id,
      at: lat !== null && lon !== null ? [lon, lat] : undefined,
    },
    { fly: lat !== null && lon !== null, focus: true },
  )
}

function PharmacyRow({ p }: { p: DutyPharmacy }) {
  const precNote = PRECISION_SHORT[p.precision]
  return (
    <li>
      <button
        type="button"
        onClick={() => openItem(p.id, p.lat, p.lon)}
        className="flex w-full items-start gap-2.5 rounded-lg border border-transparent px-2.5 py-2 text-left transition-colors hover:bg-raised focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <span
          className="mt-1.5 size-2.5 shrink-0 rounded-full"
          style={{ background: TONIGHT_COLORS.duty }}
          aria-hidden="true"
        />
        <span className="min-w-0 flex-1">
          <span className="flex items-baseline justify-between gap-2">
            <span className="truncate text-[13px] font-medium text-foreground">{p.name}</span>
            <span className="shrink-0 font-readout text-[12px] tabular-nums text-muted-foreground">
              {p.walk_min ? `${p.walk_min} min` : precNote ?? ""}
            </span>
          </span>
          <span className="mt-0.5 block truncate text-[12px] text-muted-foreground">
            {[p.municipality, p.address].filter(Boolean).join(", ")}
          </span>
          <span className="mt-0.5 flex flex-wrap items-center gap-x-2.5 gap-y-0.5 text-[11.5px] text-muted-foreground">
            {p.pharmacy_tel && (
              <span className="inline-flex items-center gap-1 font-readout tabular-nums">
                <PhoneIcon className="size-3" aria-hidden="true" />
                {p.pharmacy_tel}
              </span>
            )}
            {precNote && (
              <span className="text-sodium">{precNote}</span>
            )}
          </span>
        </span>
      </button>
    </li>
  )
}

function EventRow({ ev, showDate = false }: { ev: CityEvent; showDate?: boolean }) {
  const startLabel = showDate
    ? ev.start_ts.slice(0, 10)
    : ev.all_day
      ? "All day"
      : ev.start_ts.slice(11, 16)
  return (
    <li>
      <button
        type="button"
        onClick={() => openItem(ev.id, ev.lat, ev.lon)}
        className="flex w-full items-start gap-2.5 rounded-lg border border-transparent px-2.5 py-2 text-left transition-colors hover:bg-raised focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <span
          className="mt-1.5 size-2.5 shrink-0 rounded-full"
          style={{ background: TONIGHT_COLORS.event }}
          aria-hidden="true"
        />
        <span className="min-w-0 flex-1">
          <span className="flex items-baseline justify-between gap-2">
            <span className="line-clamp-1 text-[13px] font-medium text-foreground">{ev.title}</span>
            <span className="shrink-0 font-readout text-[12px] tabular-nums text-muted-foreground">{startLabel}</span>
          </span>
          <span className="mt-0.5 flex items-center gap-1 truncate text-[12px] text-muted-foreground">
            <CalendarIcon className="size-3 shrink-0" aria-hidden="true" />
            <span className="truncate">{[ev.venue_name, ev.municipality].filter(Boolean).join(", ")}</span>
          </span>
        </span>
      </button>
    </li>
  )
}

function OpenRow({ pl }: { pl: OpenNowPlace }) {
  return (
    <li>
      <button
        type="button"
        onClick={() => openItem(pl.id, pl.lat, pl.lon)}
        className="flex w-full items-start gap-2.5 rounded-lg border border-transparent px-2.5 py-2 text-left transition-colors hover:bg-raised focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <span
          className="mt-1.5 size-2.5 shrink-0 rounded-full"
          style={{ background: TONIGHT_COLORS.open }}
          aria-hidden="true"
        />
        <span className="min-w-0 flex-1">
          <span className="flex items-baseline justify-between gap-2">
            <span className="truncate text-[13px] font-medium text-foreground">{pl.name}</span>
            <span className="shrink-0 font-readout text-[12px] tabular-nums text-muted-foreground">
              {pl.walk_min} min
            </span>
          </span>
          <span className="mt-0.5 block truncate text-[12px] text-muted-foreground">
            {[pl.category.replace(/_/g, " "), pl.status_text].filter(Boolean).join(", ")}
          </span>
        </span>
      </button>
    </li>
  )
}

function TonightSection() {
  const clockAt = useClock((s) => s.at)
  const clockNow = useClock((s) => s.now)
  const layerOn = useTonight((s) => s.on)
  const status = useTonight((s) => s.status)
  const error = useTonight((s) => s.error)
  const data = useTonight((s) => s.data)
  const [tab, setTab] = useState("pharmacies")

  useEffect(() => {
    void loadTonight()
  }, [clockAt])

  const sky = skyAt(new Date(clockAt ?? clockNow))

  return (
    <section aria-label="Tonight in Nicosia" className="flex flex-col gap-2.5 border-t border-line pt-3">
      <div className="flex items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          <h3 className="truncate text-[13px] font-semibold text-foreground">Tonight in Nicosia</h3>
          {sky.dark && <Badge variant="secondary">After dusk</Badge>}
        </div>
        <Button
          variant={layerOn ? "secondary" : "outline"}
          size="xs"
          aria-pressed={layerOn}
          onClick={() => setTonightLayer(!layerOn)}
        >
          <MapPinIcon data-icon="inline-start" />
          {layerOn ? "On map" : "Show on map"}
        </Button>
      </div>

      {data && (
        <p className="text-[12px] leading-snug text-muted-foreground">{data.shift_note}</p>
      )}

      {status === "loading" && !data && (
        <div className="flex flex-col gap-2" aria-label="Loading tonight listings">
          <Skeleton className="h-12 w-full rounded-lg" />
          <Skeleton className="h-12 w-full rounded-lg" />
          <Skeleton className="h-12 w-full rounded-lg" />
        </div>
      )}

      {status === "error" && !data && (
        <Alert variant="destructive">
          <AlertTitle>Tonight unavailable</AlertTitle>
          <AlertDescription className="flex items-center justify-between gap-2">
            <span>{error ?? "Could not reach the duty pharmacy or events feed."}</span>
            <Button size="xs" variant="outline" onClick={() => void loadTonight({ force: true })}>
              <RefreshCwIcon data-icon="inline-start" />
              Retry
            </Button>
          </AlertDescription>
        </Alert>
      )}

      {data && (
        <Tabs value={tab} onValueChange={(v) => setTab(String(v))}>
          <TabsList className="w-full">
            <TabsTrigger value="pharmacies" className="text-[12px]">
              On duty ({data.counts.pharmacies})
            </TabsTrigger>
            <TabsTrigger value="events" className="text-[12px]">
              Events ({data.counts.events})
            </TabsTrigger>
            <TabsTrigger value="open_now" className="text-[12px]">
              Open now ({data.counts.open_now})
            </TabsTrigger>
          </TabsList>

          <TabsContent value="pharmacies" className="mt-1.5">
            {!data.known || data.pharmacies.length === 0 ? (
              <Empty className="py-4">
                <EmptyHeader>
                  <EmptyTitle>No duty roster for this night</EmptyTitle>
                  <EmptyDescription>
                    {data.status.error ?? "The twin's memory has no duty pharmacy schedule for that date."}
                  </EmptyDescription>
                </EmptyHeader>
              </Empty>
            ) : (
              <ul className="flex max-h-56 flex-col gap-1 overflow-y-auto pr-0.5">
                {data.pharmacies.map((p) => (
                  <PharmacyRow key={p.id} p={p} />
                ))}
              </ul>
            )}
          </TabsContent>

          <TabsContent value="events" className="mt-1.5">
            {data.events.length > 0 ? (
              <ul className="flex max-h-56 flex-col gap-1 overflow-y-auto pr-0.5">
                {data.events.map((ev) => (
                  <EventRow key={ev.id} ev={ev} />
                ))}
              </ul>
            ) : data.upcoming_events.length > 0 ? (
              <div className="flex flex-col gap-1.5">
                <p className="text-[12px] text-muted-foreground">
                  No Nicosia events listed for tonight. Coming up on Visit Cyprus:
                </p>
                <ul className="flex max-h-52 flex-col gap-1 overflow-y-auto pr-0.5">
                  {data.upcoming_events.map((ev) => (
                    <EventRow key={ev.id} ev={ev} showDate />
                  ))}
                </ul>
              </div>
            ) : (
              <Empty className="py-4">
                <EmptyHeader>
                  <EmptyTitle>No Nicosia events listed</EmptyTitle>
                  <EmptyDescription>Visit Cyprus lists no Nicosia events in this window.</EmptyDescription>
                </EmptyHeader>
              </Empty>
            )}
          </TabsContent>

          <TabsContent value="open_now" className="mt-1.5">
            {data.open_now.length === 0 ? (
              <Empty className="py-4">
                <EmptyHeader>
                  <EmptyTitle>No open venues matched</EmptyTitle>
                  <EmptyDescription>
                    No OpenStreetMap places report open hours for this time of night.
                  </EmptyDescription>
                </EmptyHeader>
              </Empty>
            ) : (
              <ul className="flex max-h-56 flex-col gap-1 overflow-y-auto pr-0.5">
                {data.open_now.map((pl) => (
                  <OpenRow key={pl.id} pl={pl} />
                ))}
              </ul>
            )}
          </TabsContent>
        </Tabs>
      )}
    </section>
  )
}

registerTopicSection({
  id: "tonight",
  topic: "outside",
  order: 20,
  Section: TonightSection,
})
