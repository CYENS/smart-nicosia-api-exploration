/* The environment twin plugs into the page here: the Environment workspace on the rail, the
   spot kind ("What's it like here?") and its inspector section, the legend of its answer layers
   in the map panel, its map layers and the right-click menu item, and its commands in Ctrl K. */
import { CloudSunIcon, MapPinnedIcon, MountainIcon, SunIcon, WindIcon } from "lucide-react"
import { createElement } from "react"

import { ShadeLegend, SunHoursLegend, SurfaceLegend } from "@/components/environment/parts"
import "@/components/environment/sections"
import { EnvironmentPanel } from "@/components/environment/workspace"
import { KINDS } from "@/lib/entities"
import { answerMeta, openSpotAtCentre, setShadowsOn, setTerrainOn, setWindOn, startEnvironmentMap } from "@/lib/environment/map"
import { useEnvironment } from "@/lib/environment/store"
import { registerLayerKey } from "@/lib/registries/layer-keys"
import { matches, registerSearchProvider, type SearchItem } from "@/lib/registries/search"
import { openWorkspace, registerWorkspace, useWorkspace } from "@/lib/registries/workspaces"

KINDS.spot = { label: "Spot", icon: CloudSunIcon, chip: "place" }

registerWorkspace({
  id: "environment", label: "Environment", description: "Environment: sun, shade, terrain, air and weather",
  icon: SunIcon, order: 20, key: "3", Side: EnvironmentPanel,
})

// An answer's layer in the map panel: its own legend, in the colours of the base map on show.
registerLayerKey("environment", ({ layer }) => {
  const meta = answerMeta(layer.ref)
  if (!meta) return null
  if (meta.kind === "surface") return createElement(SurfaceLegend, { surface: meta })
  if (meta.kind === "sun_hours") return createElement(SunHoursLegend)
  if (meta.kind === "shade") return createElement(ShadeLegend)
  return createElement("p", { className: "text-[12.5px] text-muted-foreground" }, "Particles move with the modelled wind, faster where it is stronger")
})

registerSearchProvider({
  id: "environment-commands",
  heading: "Commands",
  order: 22,
  search: (q) => {
    const { terrain, shadows, wind } = useEnvironment.getState()
    const inEnv = useWorkspace.getState().active === "environment"
    const items: (SearchItem & { words: string })[] = [
      { key: "cmd:spot", icon: MapPinnedIcon, group: "Commands", title: "What's it like here?", opens: true,
        detail: "Sun, shade, air and weather at the map's centre", run: () => openSpotAtCentre(), words: "spot here sun shade air noise weather temperature" },
      { key: "cmd:terrain", icon: MountainIcon, group: "Commands", title: terrain.on ? "Flatten the terrain" : "Show the terrain",
        detail: "Hills shaded by the sun", run: () => setTerrainOn(!terrain.on), words: "terrain relief hills elevation 3d ground" },
      { key: "cmd:shadows", icon: SunIcon, group: "Commands", title: shadows.on ? "Hide the shadows" : "Show the shadows",
        detail: "Cast by the 3D city at the clock's moment", run: () => setShadowsOn(!shadows.on), words: "shadows shade sun buildings" },
      { key: "cmd:wind", icon: WindIcon, group: "Commands", title: wind.on ? "Hide the wind" : "Show the wind",
        detail: "Moving with the modelled wind", run: () => setWindOn(!wind.on), words: "wind weather breeze particles" },
      { key: "cmd:environment", icon: CloudSunIcon, group: "Commands", title: inEnv ? "Back to Live" : "Open the Environment workspace",
        detail: inEnv ? "The city as it is now" : "Sun, shade, terrain, air and weather", run: () => openWorkspace(inEnv ? "live" : "environment"),
        words: "workspace environment sun shade air noise rain radar weather" },
    ]
    return q ? items.filter((c) => matches(q, c.title, c.detail, c.words)) : []
  },
})

startEnvironmentMap()
