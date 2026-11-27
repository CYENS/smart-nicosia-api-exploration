/* The Layers sheet's row for what the 2019 LiDAR flight found changed since the 2014 building
   survey: every building of house size that grew by a floor or more, lost height, or is gone,
   drawn in 3D at its 2019 height (the gone ones at the height they had). The server holds the
   comparison (chatbot/lidar.py, /api/buildings/changes); the assistant's find_height_changes
   tool draws the same buildings for one place or one kind of change. */
import { create } from "zustand"

import { DotLegend } from "@/components/topics/builtin"
import { removeLayer, showLayer, useMapUi, type FC } from "@/lib/map/engine"
import { fmtCount } from "@/lib/readouts"
import { registerSystemLayer } from "@/lib/registries/layers"

const REF = "height_changes"
const WORDS: Record<string, string> = { taller: "Taller", lower: "Lower", gone: "Gone" }

type Meta = { count: number; measured: number; by_change: Record<string, number>; legend: Record<string, string> }

const useChanges = create<{ status: string; meta: Meta | null }>(() => ({ status: "", meta: null }))

async function showChanges(on: boolean) {
  if (!on) { removeLayer(REF); return }
  useChanges.setState({ status: "Reading what the 2019 flight measured" })
  try {
    const res = await fetch("/api/buildings/changes")
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `HTTP ${res.status}`)
    const data = (await res.json()) as FC
    await showLayer({
      ref: REF, url: "/api/buildings/changes", style: "buildings_3d", title: data.meta.title,
      fit: false, labels: false, legend: data.meta.legend, count: data.meta.count, value_field: null,
      sources: data.meta.sources,
    }, data)
    useChanges.setState({ status: "", meta: data.meta as Meta })
  } catch (e) {
    useChanges.setState({ status: `Couldn't load the changes: ${(e as Error).message}. Switch it off and on to try again.` })
  }
}

registerSystemLayer({
  id: "height_changes", group: "buildings", order: 20, label: "Changed since 2014",
  useOn: () => useMapUi((s) => s.layers.some((l) => l.ref === REF)),
  setOn: (on) => { void showChanges(on) },
  Summary: function Summary() {
    const on = useMapUi((s) => s.layers.some((l) => l.ref === REF))
    const { status, meta } = useChanges()
    if (status) return <>{status}</>
    if (on && meta) {
      const b = meta.by_change
      return <>{`${fmtCount(b.taller ?? 0)} taller, ${fmtCount(b.gone ?? 0)} gone and ${fmtCount(b.lower ?? 0)} lower, of ${fmtCount(meta.measured)} measured`}</>
    }
    return <>What the 2019 laser survey found against the 2014 one</>
  },
  Legend: function Legend() {
    const meta = useChanges((s) => s.meta)
    if (!meta) return null
    return <DotLegend label="Changed since 2014" items={Object.entries(meta.legend).map(([k, c]): [string, string] => [c, WORDS[k] ?? k])} />
  },
})
