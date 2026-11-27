/* The environment twin plugs into the page here: the Environment workspace (the Outside card's
   way deeper, in the Layers sheet, Ctrl K and Alt 3), the spot kind ("What's it like here?")
   and its inspector section, its group in the Layers sheet and its section on the Outside card,
   the legend of its answers' layers, its map layers, its commands in Ctrl K and the sources its
   layers draw from. */
import { CloudSunIcon, MapPinnedIcon, MountainIcon, SunIcon, WindIcon } from "lucide-react"
import { createElement } from "react"

import { ShadeLegend, SunHoursLegend, SurfaceLegend } from "@/components/environment/parts"
import "@/components/environment/rows"
import "@/components/environment/sections"
import { STATION_ICONS } from "@/components/environment/stations"
import { EnvironmentPanel, rememberEnvironmentOpener } from "@/components/environment/workspace"
import { KINDS } from "@/lib/entities"
import { answerMeta, openSpotAtCentre, setShadowsOn, setTerrainOn, setWindOn, startEnvironmentMap } from "@/lib/environment/map"
import { startStations } from "@/lib/environment/stations"
import { useEnvironment } from "@/lib/environment/store"
import { registerLayerKey } from "@/lib/registries/layer-keys"
import { matches, registerSearchProvider, type SearchItem } from "@/lib/registries/search"
import { registerWorkspace } from "@/lib/registries/workspaces"
import { registerActiveSources } from "@/lib/sources"

KINDS.spot = { label: "Spot", icon: CloudSunIcon, chip: "place" }
KINDS.weather_station = { label: "Weather station", icon: STATION_ICONS.weather_station, chip: "device" }
KINDS.reference_air_station = { label: "Reference air station", icon: STATION_ICONS.reference_air_station, chip: "device" }

registerWorkspace({
  id: "environment", label: "Environment", description: "Environment: sun, shade, terrain, air and weather",
  icon: SunIcon, order: 20, key: "3", Side: EnvironmentPanel, onEnter: rememberEnvironmentOpener,
})

// An answer's layer in the Layers sheet: its own legend, in the colours of the base map on show.
registerLayerKey("environment", ({ layer }) => {
  const meta = answerMeta(layer.ref)
  if (!meta) return null
  if (meta.kind === "surface") return createElement(SurfaceLegend, { surface: meta })
  if (meta.kind === "sun_hours") return createElement(SunHoursLegend)
  if (meta.kind === "shade") return createElement(ShadeLegend)
  return createElement("p", { className: "text-[12.5px] text-muted-foreground" }, "Streaks drift with the modelled wind, faster where it is stronger")
})

// What the environment's own layers draw from, for the map's credits and the sources panel.
registerActiveSources(() => {
  const s = useEnvironment.getState()
  const ids: string[] = []
  if (s.terrain.on) ids.push("terrain_tiles")
  if (s.shadows.on || s.sunHours.shown) ids.push("dls")
  if (s.surface.metric) ids.push("smart_nicosia", "open_meteo")
  if (s.wind.on) ids.push("open_meteo")
  if (s.rain.on) ids.push("rainviewer")
  return ids
}, (fn) => useEnvironment.subscribe(fn))

registerSearchProvider({
  id: "environment-commands",
  heading: "Commands",
  order: 14, // before the workspaces: "terrain" shows the terrain rather than opening Environment
  search: (q) => {
    const { terrain, shadows, wind } = useEnvironment.getState()
    const items: (SearchItem & { words: string })[] = [
      { key: "cmd:spot", icon: MapPinnedIcon, group: "Commands", title: "What's it like here?", opens: true,
        detail: "Sun, shade, air and weather at the map's centre", run: () => openSpotAtCentre(), words: "spot here sun shade air noise weather temperature" },
      { key: "cmd:terrain", icon: MountainIcon, group: "Commands", title: terrain.on ? "Flatten the terrain" : "Show the terrain",
        detail: "Hills lit by the sun", run: () => setTerrainOn(!terrain.on), words: "terrain relief hills elevation 3d ground" },
      { key: "cmd:shadows", icon: SunIcon, group: "Commands", title: shadows.on ? "Hide the shadows" : "Show the shadows",
        detail: "Cast by the 3D city at the clock's moment", run: () => setShadowsOn(!shadows.on), words: "shadows shade sun buildings" },
      { key: "cmd:wind", icon: WindIcon, group: "Commands", title: wind.on ? "Hide the wind" : "Show the wind",
        detail: "Moving with the modelled wind", run: () => setWindOn(!wind.on), words: "wind weather breeze particles" },
    ]
    return q ? items.filter((c) => matches(q, c.title, c.detail, c.words)) : []
  },
})

startEnvironmentMap()
startStations()
