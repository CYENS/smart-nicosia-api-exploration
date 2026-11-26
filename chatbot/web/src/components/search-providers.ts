/* The search menu's built-in providers: things opened lately, everything in the twin (from the
   server's caches), commands, and the prepared questions. */
import {
  Building2Icon, CopyIcon, KeyboardIcon, LayersIcon, LocateFixedIcon, MessageSquareTextIcon, MoonIcon,
  RotateCcwIcon, ShieldAlertIcon, SunIcon, SunMoonIcon, XIcon,
} from "lucide-react"

import { newChat, send } from "@/lib/chat"
import { linkToView } from "@/lib/deeplink"
import { kindMeta, searchEntities } from "@/lib/entities"
import { closeInspector, inspect, useInspector } from "@/lib/inspector"
import { useLayout } from "@/lib/layout"
import {
  flyToBounds, locateMe, resetView, setBasemapMode, setCity3d, showRegisters, useMapUi,
} from "@/lib/map/engine"
import { QUESTION_GROUPS } from "@/lib/questions"
import { matches, registerSearchProvider, type SearchItem } from "@/lib/registries/search"

registerSearchProvider({
  id: "recent",
  heading: "Opened lately",
  order: 0,
  search: (q) => useInspector.getState().recent
    .filter((r) => matches(q, r.id, r.name, kindMeta(r.kind).label))
    .slice(0, q ? 3 : 5)
    .map((r) => ({
      key: `recent:${r.id}`, title: r.name ?? r.id, detail: `${kindMeta(r.kind).label}, ${r.id}`,
      icon: kindMeta(r.kind).icon, opens: true, run: () => inspect({ id: r.id }, { fly: true, focus: true }),
    })),
})

registerSearchProvider({
  id: "twin",
  heading: "In the twin",
  order: 10,
  remote: true,
  search: async (q, signal) => {
    if (q.trim().length < 2) return []
    const results = await searchEntities(q.trim(), signal)
    return results.map((r): SearchItem => {
      const meta = kindMeta(r.kind)
      return {
        key: `twin:${r.group}:${r.id ?? r.name}`, title: r.name, group: r.group, icon: meta.icon, color: r.color,
        opens: r.kind !== "street",
        detail: [r.id && !r.name.includes(r.id) ? r.id : null, r.detail].filter(Boolean).join(", "),
        run: () => {
          if (r.kind === "street" && r.bbox) flyToBounds(r.bbox)
          else if (r.id) inspect({ id: r.id, at: r.lon != null && r.lat != null ? [r.lon, r.lat] : undefined }, { fly: true, focus: true })
        },
      }
    })
  },
})

function commands(): SearchItem[] {
  const ui = useMapUi.getState()
  const layout = useLayout.getState()
  const inspecting = useInspector.getState().open
  const list: (SearchItem & { words?: string })[] = [
    {
      key: "cmd:city", icon: Building2Icon, title: ui.city.on ? "Turn off the 3D city" : "Turn on the 3D city",
      detail: "Every building at its DLS height", run: () => setCity3d(!ui.city.on), words: "3d buildings dls",
    },
    {
      key: "cmd:registers", icon: ShieldAlertIcon, title: ui.registers.on ? "Hide the dangerous buildings" : "Show the dangerous buildings",
      detail: "The ΕΟΑ registers of four districts", run: () => showRegisters(!ui.registers.on), words: "registers eoa dangerous",
    },
    { key: "cmd:night", icon: MoonIcon, title: "Night base map", detail: "Whatever the time", run: () => setBasemapMode("night"), words: "dark base map" },
    { key: "cmd:day", icon: SunIcon, title: "Day base map", detail: "Whatever the time", run: () => setBasemapMode("day"), words: "light base map" },
    { key: "cmd:auto", icon: SunMoonIcon, title: "Base map follows the sun", detail: "Night from dusk to dawn", run: () => setBasemapMode("auto"), words: "auto" },
    { key: "cmd:locate", icon: LocateFixedIcon, title: "Locate me", detail: "Share this device's position with the assistant", run: locateMe, words: "where am i position gps" },
    {
      key: "cmd:layers", icon: LayersIcon, title: layout.layersOpen ? "Hide the map panel" : "Show the map panel",
      detail: "Base map, 3D city, registers and legends",
      run: () => { if (inspecting) closeInspector(); useLayout.setState({ layersOpen: !layout.layersOpen || inspecting }) },
      words: "layers legend",
    },
    {
      key: "cmd:chat", icon: MessageSquareTextIcon, title: layout.chatOpen ? "Hide the conversation" : "Show the conversation",
      run: () => useLayout.setState({ chatOpen: !layout.chatOpen }), words: "chat",
    },
    { key: "cmd:reset", icon: RotateCcwIcon, title: "Back to the whole city", detail: "Reset the camera", run: resetView, words: "home view zoom out" },
    {
      key: "cmd:link", icon: CopyIcon, title: "Copy a link to this view", detail: "The camera and the open thing",
      run: () => { void navigator.clipboard?.writeText(linkToView()).catch(() => {}) }, words: "share url",
    },
    { key: "cmd:new", icon: RotateCcwIcon, title: "Start a new chat", detail: "Clears the map and the stage", run: newChat, words: "reset conversation" },
    { key: "cmd:keys", icon: KeyboardIcon, title: "Keyboard shortcuts", run: () => useLayout.setState({ shortcutsOpen: true }), words: "keys help" },
  ]
  if (inspecting) list.unshift({ key: "cmd:close", icon: XIcon, title: "Close the inspector", run: closeInspector })
  return list
}

registerSearchProvider({
  id: "commands",
  heading: "Commands",
  order: 20,
  search: (q) => commands().filter((c) => matches(q, c.title, c.detail, (c as { words?: string }).words)).slice(0, q ? 5 : 20),
})

registerSearchProvider({
  id: "questions",
  heading: "Questions",
  order: 30,
  search: (q) => QUESTION_GROUPS.flatMap((g) => g.questions
    .filter((question) => matches(q, question, g.title))
    .map((question) => ({
      key: `q:${question}`, title: question, icon: g.icon, group: q ? "Questions" : g.title,
      run: () => { useLayout.setState({ chatOpen: true }); send(question) },
    }))),
})
