/* Road reports plug into the twin here: their kind in the inspector (and its "As published"
   section), a row in the Layers sheet beside the road speeds, a section on the Traffic card,
   the answer layers' legend, and their commands in Ctrl K. Reports themselves are searched by
   the server's entity search (chatbot/mobility/report_entities.py). */
import { TriangleAlertIcon } from "lucide-react"

import { AsPublished, ReportLegend, TrafficReportsSection, useReportsLine } from "@/components/reports/parts"
import { KINDS } from "@/lib/entities"
import { setReportsOn, startReportMap } from "@/lib/reports/map"
import { REPORT_KINDS, useReports, type ReportKind } from "@/lib/reports/store"
import { registerInspectorSection } from "@/lib/registries/inspector-sections"
import { registerLayerKey } from "@/lib/registries/layer-keys"
import { registerSystemLayer } from "@/lib/registries/layers"
import { matches, registerSearchProvider, type SearchItem } from "@/lib/registries/search"
import { registerTopicSection } from "@/lib/registries/topics"
import { registerActiveSources } from "@/lib/sources"

KINDS.road_report = { label: "Road report", icon: TriangleAlertIcon, chip: "place" }

registerInspectorSection({ id: "road-report-text", order: 16, kinds: ["road_report"], Component: AsPublished })

// -- the Layers sheet: beside the road speeds ---------------------------------------------------------

registerSystemLayer({
  id: "road_reports", group: "sensors", order: 61, label: "Road reports",
  useOn: () => useReports((s) => s.on),
  setOn: setReportsOn,
  Summary: function Summary() {
    const on = useReports((s) => s.on)
    const line = useReportsLine()
    if (!on || !line) return <>Closures, jams, accidents and hazards from Waze drivers, and the roadworks of Public Works</>
    return <>{line.lead.replace(/\.$/, "")}</>
  },
  Legend: function Legend() {
    const counts = useReports((s) => s.body?.meta.counts)
    const shown = REPORT_KINDS.filter((k) => counts?.[k])
    return <ReportLegend kinds={shown.length ? shown : REPORT_KINDS} />
  },
})

// The map's corner names Waze and Public Works while their reports are drawn.
registerActiveSources(() => (useReports.getState().on ? ["waze_alerts", "traffic_events"] : []),
                      (fn) => { useReports.subscribe((s, prev) => { if (s.on !== prev.on) fn() }) })

// -- the Traffic card ------------------------------------------------------------------------------

registerTopicSection({ id: "road-reports", topic: "traffic", order: 10, Section: TrafficReportsSection })

// -- an answer's layer, in "On the map now" -----------------------------------------------------------

registerLayerKey("road-reports", function Key({ layer }) {
  const shown = REPORT_KINDS.filter((k) => Object.keys(layer.legend).some((l) => l === WORDS[k]))
  return <ReportLegend kinds={shown.length ? shown : REPORT_KINDS} />
})
const WORDS: Record<ReportKind, string> = {
  accident: "Accident", closure: "Road closed", jam: "Jam", hazard: "Hazard", roadworks: "Roadworks", diversion: "Diversion",
}

// -- Ctrl K ---------------------------------------------------------------------------------------------

registerSearchProvider({
  id: "road-report-commands",
  heading: "Commands",
  order: 22,
  search: (q) => {
    const on = useReports.getState().on
    const items: (SearchItem & { words: string })[] = [
      { key: "cmd:road-reports", icon: TriangleAlertIcon, group: "Commands",
        title: on ? "Hide road reports" : "Show road reports", detail: "Closures, jams, accidents and roadworks",
        run: () => setReportsOn(!on), words: "road reports closures closed jams accidents hazards roadworks works diversion waze traffic" },
    ]
    return q ? items.filter((c) => matches(q, c.title, c.detail, c.words)) : []
  },
})

startReportMap()
