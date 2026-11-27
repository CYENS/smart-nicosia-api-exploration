/* Weather on the map (lib/weather/map.ts), started with the page, and the "Weather on the map"
   switch in the Layers sheet's Map section (components/weather-field.tsx). */
import { useEnvironment } from "@/lib/environment/store"
import { registerActiveSources } from "@/lib/sources"
import { onRadar, refreshWeather, startWeatherMap, useWeatherMap, weatherShows } from "@/lib/weather/map"

onRadar(() => useEnvironment.getState().rain.on)
useEnvironment.subscribe((s, p) => { if (s.rain.on !== p.rain.on) refreshWeather() })
// The map credits Open-Meteo while it shows the weather.
registerActiveSources(
  () => (weatherShows() ? ["open_meteo"] : []),
  (fn) => {
    let was = weatherShows()
    useWeatherMap.subscribe(() => { if (weatherShows() !== was) { was = weatherShows(); fn() } })
  },
)
startWeatherMap()
