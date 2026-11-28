/* Inspector section for duty_pharmacy and city_event entities:
   - Walking time, distance, and Valhalla step directions from your location or Nicosia centre
   - Official Ministry of Health landmark directions and pharmacy phone (never personal home phone)
   - Precision provenance (exact / street DLS INSPIRE AD_Addresses / area / unplaced)
   - Event timing and Visit Cyprus link. */
import { ExternalLinkIcon, FootprintsIcon, MapPinIcon, PhoneIcon } from "lucide-react"
import { useEffect, useState } from "react"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import type { Entity } from "@/lib/entities"
import { useMapUi } from "@/lib/map/engine"
import { registerInspectorSection, type SectionProps } from "@/lib/registries/inspector-sections"
import { fetchWalk, type Precision, type WalkInfo } from "@/lib/tonight/store"

type TonightEntityData = {
  kind: "duty_pharmacy" | "city_event"
  duty_date?: string
  on_duty?: boolean
  address?: string | null
  directions?: string | null
  municipality?: string | null
  pharmacy_tel?: string | null
  start_ts?: string
  end_ts?: string
  venue_name?: string | null
  venue_address?: string | null
  categories?: string | null
  url?: string | null
  description?: string | null
  precision?: Precision
  precision_label?: string | null
  located_by?: string | null
}

function TonightInspectorSection({ entity }: SectionProps) {
  const info = (entity as Entity & { tonight?: TonightEntityData }).tonight
  const meLoc = useMapUi((s) => s.me.loc)
  const walkKey = `${entity.id}:${meLoc ? `${meLoc.lat.toFixed(4)},${meLoc.lon.toFixed(4)}` : "center"}`
  const [state, setState] = useState<{ key: string; walk: WalkInfo | null; failed: boolean } | null>(null)

  useEffect(() => {
    const ac = new AbortController()
    fetchWalk(entity.id, ac.signal)
      .then((res) => {
        if (ac.signal.aborted) return
        setState({ key: walkKey, walk: res, failed: false })
      })
      .catch(() => {
        if (ac.signal.aborted) return
        setState({ key: walkKey, walk: null, failed: true })
      })
    return () => ac.abort()
  }, [entity.id, walkKey])

  if (!info) return null

  const loading = !state || state.key !== walkKey
  const walk = state?.key === walkKey ? state.walk : null
  const failed = state?.key === walkKey && state.failed
  const cleanTel = info.pharmacy_tel ? info.pharmacy_tel.replace(/[^\d+]/g, "") : ""

  return (
    <section aria-label="Tonight details and walking directions" className="flex flex-col gap-3.5">
      {info.kind === "duty_pharmacy" && (
        <>
          {info.directions && (
            <div className="rounded-lg bg-raised px-3 py-2.5 text-[13px]">
              <p className="mb-1 text-[11.5px] font-medium uppercase tracking-wide text-muted-foreground">
                Official landmark directions
              </p>
              <p className="leading-snug text-foreground">{info.directions}</p>
            </div>
          )}

          <div className="flex flex-wrap items-center gap-2">
            {cleanTel && (
              <Button variant="outline" size="sm" nativeButton={false} render={<a href={`tel:${cleanTel}`} />}>
                <PhoneIcon data-icon="inline-start" />
                Call pharmacy ({info.pharmacy_tel})
              </Button>
            )}
            {info.precision && (
              <Badge variant={info.precision === "exact" ? "secondary" : "outline"}>
                <MapPinIcon data-icon="inline-start" />
                {info.precision === "exact"
                  ? "Exact location"
                  : info.precision === "street"
                    ? "Street match"
                    : info.precision === "area"
                      ? "Area approximate"
                      : "Unplaced on map"}
              </Badge>
            )}
          </div>
          {info.located_by && (
            <p className="text-[12px] text-muted-foreground">
              Map placement: {info.precision_label ?? info.precision} ({info.located_by})
            </p>
          )}
        </>
      )}

      {info.kind === "city_event" && (
        <div className="flex flex-col gap-2">
          {info.description && (
            <p className="text-[13px] leading-snug text-foreground">{info.description}</p>
          )}
          <div className="flex flex-wrap items-center gap-2">
            {info.url && (
              <Button
                variant="outline"
                size="sm"
                nativeButton={false}
                render={<a href={info.url} target="_blank" rel="noreferrer" />}
              >
                <ExternalLinkIcon data-icon="inline-start" />
                Visit Cyprus event page
              </Button>
            )}
            {info.located_by && (
              <Badge variant="outline">{info.precision_label ?? "Venue placed"}</Badge>
            )}
          </div>
        </div>
      )}

      <div className="flex flex-col gap-2 rounded-lg border border-line bg-console/60 p-3">
        <div className="flex items-center gap-1.5 text-[12.5px] text-muted-foreground">
          <FootprintsIcon className="size-3.5 text-sodium" aria-hidden="true" />
          <span>On foot</span>
        </div>
        {loading && <Skeleton className="h-10 w-full rounded-md" />}
        {!loading && failed && (
          <p className="text-[12.5px] text-muted-foreground">Walking time unavailable right now.</p>
        )}
        {!loading && !failed && walk && !walk.placed && (
          <p className="text-[12.5px] text-muted-foreground">
            Address is not placed on the map yet; follow the official landmark directions above.
          </p>
        )}
        {!loading && !failed && walk && walk.placed && walk.duration_min !== null && (
          <div className="flex flex-col gap-2">
            <div className="flex items-baseline justify-between gap-2">
              <span className="font-readout text-[20px] font-semibold tabular-nums text-foreground">
                {walk.duration_min} min
              </span>
              <span className="font-readout text-[12.5px] tabular-nums text-muted-foreground">
                {walk.distance_m} m from {walk.from_label ?? "Nicosia centre"}
                {walk.source === "estimate" ? " (straight line)" : ""}
              </span>
            </div>
            {walk.directions.length > 0 && (
              <ol className="flex flex-col gap-1 border-t border-line pt-2 text-[12.5px] text-muted-foreground">
                {walk.directions.slice(0, 8).map((step, i) => (
                  <li key={i} className="leading-snug">
                    <span className="mr-1.5 font-readout text-[11.5px] text-foreground">{i + 1}.</span>
                    {step}
                  </li>
                ))}
              </ol>
            )}
          </div>
        )}
      </div>
    </section>
  )
}

registerInspectorSection({
  id: "tonight-detail",
  order: 15,
  kinds: ["duty_pharmacy", "city_event"],
  Component: TonightInspectorSection,
})
