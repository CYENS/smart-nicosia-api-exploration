/* The inspector's Incidents section: the incidents a device was part of in the last two weeks,
   the unresolved first. A row opens the incident in the inbox. Drawn only when there are some. */
import { useEffect, useState } from "react"

import { RowList } from "@/components/mobility/parts"
import { IncidentRowView } from "@/components/watch/parts"
import { inspectId } from "@/lib/inspector"
import { registerInspectorSection, type SectionProps } from "@/lib/registries/inspector-sections"
import { useWatch, type IncidentRow } from "@/lib/watch/store"

const DEVICE_KINDS = ["street_light", "parking_sensor", "waste_bin", "air_station", "info_screen", "camera"]
async function involving(id: string, signal: AbortSignal) {
  const q = new URLSearchParams({ entity: id, status: "all", since_hours: String(14 * 24), limit: "6" })
  const res = await fetch(`/api/incidents?${q}`, { signal })
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return ((await res.json()) as { incidents: IncidentRow[] }).incidents
}

function Incidents({ entity }: SectionProps) {
  const [rows, setRows] = useState<IncidentRow[] | null>(null)
  // The live stream bumps the counts when anything changes: read again then.
  const counts = useWatch((s) => s.counts)
  useEffect(() => {
    const ctrl = new AbortController()
    involving(entity.id, ctrl.signal).then(setRows).catch(() => { if (!ctrl.signal.aborted) setRows([]) })
    return () => ctrl.abort()
  }, [entity.id, counts])
  if (!rows?.length) return null
  return (
    <section aria-labelledby="inspector-incidents" className="flex flex-col gap-2">
      <h3 id="inspector-incidents" className="text-[13px] font-semibold">
        Incidents <span className="font-normal text-muted-foreground">in the last two weeks</span>
      </h3>
      <RowList label="Incidents it was part of">
        {rows.map((r) => <IncidentRowView key={r.id} r={r} onSelect={() => inspectId(r.id, { fly: true, focus: true })} />)}
      </RowList>
    </section>
  )
}

registerInspectorSection({ id: "incidents", order: 45, kinds: DEVICE_KINDS, Component: Incidents })
