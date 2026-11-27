/* The Scenario lab plugs into the page here: its workspace (Layers sheet, Ctrl K, Alt 5), the
   swipe over the map while a run is open, its map layers and clicks, the lighting scenario's own
   sections, and the saved runs in Ctrl K. Links (#/s/<run>) and the chat's open_scenario come
   through openScenarioRun. */
import { FlaskConicalIcon } from "lucide-react"

import "@/components/scenarios/lighting"
import "@/components/scenarios/waste-sections"
import { ScenarioLab, rememberLabOpener } from "@/components/scenarios/lab"
import { ScenarioSwipe } from "@/components/scenarios/parts"
import { matches, registerSearchProvider, type SearchItem } from "@/lib/registries/search"
import { registerWorkspace } from "@/lib/registries/workspaces"
import { enterLabMap, leaveLabMap, startLabMap } from "@/lib/scenarios/map"
import { openScenarioRun } from "@/lib/scenarios/open"
import { fmtWhen, loadSaved, useLab } from "@/lib/scenarios/store"

registerWorkspace({
  id: "scenarios", label: "Scenario lab", description: "Scenario lab: try a change on the twin before making it on the street",
  icon: FlaskConicalIcon, order: 40, key: "5", Side: ScenarioLab, Panels: ScenarioSwipe,
  onEnter: () => { rememberLabOpener(); enterLabMap() },
  onLeave: leaveLabMap,
})

registerSearchProvider({
  id: "scenario-runs",
  heading: "Scenario runs",
  order: 40,
  search: (q) => {
    if (!q) return []
    const { saved, savedStatus } = useLab.getState()
    if (savedStatus === "idle") void loadSaved()
    const items: (SearchItem & { words: string })[] = saved.slice(0, 20).map((r) => ({
      key: `run:${r.id}`, icon: FlaskConicalIcon, group: "Scenario runs", title: r.label, opens: true,
      detail: `Run ${r.id}, ${fmtWhen(r.created)}`, run: () => openScenarioRun(r.id), words: `scenario run ${r.scenario} ${r.id}`,
    }))
    return items.filter((c) => matches(q, c.title, c.detail, c.words)).slice(0, 5)
  },
})

startLabMap()
