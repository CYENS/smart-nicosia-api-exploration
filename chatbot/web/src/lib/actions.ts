/* What the assistant can offer to do on the page, as action chips beside its suggested questions.
   It names an action by its key (its <do> tag, or the server's own when a tool needs the user's
   location); the label, the icon and what a click does live here. The server keeps the same
   keys (chatbot/followups.py ACTIONS) and the prompt lists them. A key the page doesn't know is
   dropped. To add an action, add it here, there and to the prompt. */
import { BoxIcon, InfoIcon, LocateFixedIcon, MapPinIcon, RadioIcon, type LucideIcon } from "lucide-react"

import { goLive } from "@/lib/clock"
import { pickMyLocation, requestMyLocation, setCity3d } from "@/lib/map/engine"
import { openSources } from "@/lib/sources"

// How it went: "done"; "continue" when the turn now has what it was missing (the location), so
// its question is asked again; "cancelled"; or why it failed and what to offer instead.
export type ActionOutcome = "done" | "continue" | "cancelled" | { failed: string; offer: string[] }

export type Action = {
  label: string
  pending?: string // said while it waits (for the browser, for a click on the map)
  icon: LucideIcon
  run: (opener: HTMLElement | null) => Promise<ActionOutcome> | ActionOutcome
}

const LOCATION_FAILED = {
  denied: "Your browser didn't share your location.",
  unavailable: "Your browser couldn't find your location.",
  timeout: "Finding your location took too long.",
}

export const ACTIONS: Record<string, Action> = {
  share_location: {
    label: "Share my location",
    pending: "Finding your location",
    icon: LocateFixedIcon,
    run: async () => {
      const r = await requestMyLocation()
      return r === "ok" ? "continue"
        : { failed: `${LOCATION_FAILED[r]} You can pick your spot on the map instead.`, offer: ["pick_on_map"] }
    },
  },
  pick_on_map: {
    label: "Pick my spot on the map",
    pending: "Click the map where you are",
    icon: MapPinIcon,
    run: async () => ((await pickMyLocation()) ? "continue" : "cancelled"),
  },
  city_3d: { label: "Show the 3D city", icon: BoxIcon, run: () => { setCity3d(true); return "done" } },
  go_live: { label: "Back to now", icon: RadioIcon, run: () => { goLive(); return "done" } },
  data_sources: {
    label: "Where the data comes from",
    icon: InfoIcon,
    run: (opener) => { openSources(null, opener); return "done" },
  },
}

export const knownActions = (keys: unknown): string[] =>
  Array.isArray(keys) ? [...new Set(keys.filter((k): k is string => typeof k === "string" && k in ACTIONS))] : []
