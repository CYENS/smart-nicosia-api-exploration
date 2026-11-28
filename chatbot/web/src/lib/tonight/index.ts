/* Tonight in Nicosia plugs into the page here:
   - Entity kinds duty_pharmacy and city_event
   - Layers sheet row & filter toggles
   - Outside topic card Tonight section
   - Inspector walking time & landmark directions section
   - Ctrl K search commands and loaded duty pharmacies / events
   - Map layer & click handler. */
import { CalendarIcon, CrossIcon, MoonIcon, StoreIcon } from "lucide-react"

import "@/components/tonight/rows"
import "@/components/tonight/sections"
import { KINDS } from "@/lib/entities"
import { inspect } from "@/lib/inspector"
import { matches, registerSearchProvider, type SearchItem } from "@/lib/registries/search"
import { selectTopic } from "@/lib/registries/topics"
import { setTonightLayer, startTonightMap } from "@/lib/tonight/map"
import { setTonightFilter, useTonight } from "@/lib/tonight/store"

KINDS.duty_pharmacy = { label: "Pharmacy on duty", icon: CrossIcon, chip: "place" }
KINDS.city_event = { label: "City event", icon: CalendarIcon, chip: "place" }

registerSearchProvider({
  id: "tonight-commands",
  heading: "Tonight in Nicosia",
  order: 18,
  search: (q) => {
    const commands: (SearchItem & { words: string })[] = [
      {
        key: "tonight:pharmacies",
        icon: CrossIcon,
        group: "Tonight in Nicosia",
        title: "Pharmacies on duty tonight",
        detail: "Show tonight's 6 Nicosia overnight pharmacies on the map",
        words: "pharmacy on duty tonight night chemist φαρμακείο διημερεύον",
        run: () => {
          setTonightFilter("pharmacies")
          setTonightLayer(true)
          selectTopic("outside")
        },
      },
      {
        key: "tonight:all",
        icon: MoonIcon,
        group: "Tonight in Nicosia",
        title: "Tonight in Nicosia",
        detail: "Duty pharmacies, Visit Cyprus events, and open venues on the map",
        words: "tonight nicosia whats on events duty pharmacies open now",
        run: () => {
          setTonightFilter("all")
          setTonightLayer(true)
          selectTopic("outside")
        },
      },
      {
        key: "tonight:open",
        icon: StoreIcon,
        group: "Tonight in Nicosia",
        title: "Open now in Nicosia",
        detail: "Places open right now from OpenStreetMap opening_hours",
        words: "open now tonight late restaurant cafe pharmacy 24/7",
        run: () => {
          setTonightFilter("open_now")
          setTonightLayer(true)
          selectTopic("outside")
        },
      },
    ]

    const data = useTonight.getState().data
    const loaded: (SearchItem & { words: string })[] = []
    if (data) {
      for (const p of data.pharmacies) {
        loaded.push({
          key: `tonight:${p.id}`,
          icon: CrossIcon,
          group: "Tonight in Nicosia",
          title: p.name,
          detail: [p.municipality, p.address, p.pharmacy_tel].filter(Boolean).join(" · "),
          opens: true,
          words: `pharmacy on duty tonight ${p.municipality ?? ""} ${p.address ?? ""}`,
          run: () => {
            setTonightLayer(true)
            inspect(
              { id: p.id, at: p.lat !== null && p.lon !== null ? [p.lon, p.lat] : undefined },
              { fly: p.lat !== null && p.lon !== null, focus: true },
            )
          },
        })
      }
      for (const ev of [...data.events, ...data.upcoming_events].slice(0, 6)) {
        loaded.push({
          key: `tonight:${ev.id}`,
          icon: CalendarIcon,
          group: "Tonight in Nicosia",
          title: ev.title,
          detail: [ev.start_ts.slice(0, 10), ev.venue_name, ev.municipality].filter(Boolean).join(" · "),
          opens: true,
          words: `event tonight visit cyprus ${ev.venue_name ?? ""} ${ev.categories ?? ""}`,
          run: () => {
            setTonightLayer(true)
            inspect(
              { id: ev.id, at: ev.lat !== null && ev.lon !== null ? [ev.lon, ev.lat] : undefined },
              { fly: ev.lat !== null && ev.lon !== null, focus: true },
            )
          },
        })
      }
    }

    const pool = [...commands, ...loaded]
    if (!q.trim()) return commands.slice(0, 2)
    return pool.filter((item) => matches(q, item.title, item.detail, item.words)).slice(0, 6)
  },
})

startTonightMap()
