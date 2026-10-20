/* The conversation: turns streamed from /api/chat (Server-Sent Events), and the stage that holds
   the charts and custom visuals they bring. Map commands go straight to the map engine. */
import { create } from "zustand"

import type { ChartPayload } from "@/lib/charts"
import { applyMapUi, mapContext, resetForNewChat } from "@/lib/map/engine"
import type { VisualPayload } from "@/lib/visual"

/* eslint-disable @typescript-eslint/no-explicit-any */

export const TOOL_LABELS: Record<string, string> = {
  city_overview: "Reading the city's sensors", find_devices: "Finding devices",
  get_history: "Replaying history",
  get_device: "Reading a device", get_alarms: "Checking alarms", get_traffic: "Counting traffic",
  get_speed: "Checking traffic speed", get_weather: "Checking the weather",
  geocode: "Looking up a place", find_places: "Searching places", plan_route: "Planning the route",
  reachable_area: "Working out the reachable area", show_on_map: "Drawing on the map",
  find_dangerous_buildings: "Reading the dangerous-building registers",
  get_buildings_3d: "Reading the DLS 3D buildings",
  map_fly_to: "Moving the map", map_callouts: "Adding callouts", map_clear: "Clearing the map",
  show_chart: "Drawing a chart", show_visual: "Drawing a custom visual", web_search: "Searching the web",
}

export function toolLabel(name: string) {
  return TOOL_LABELS[name] ?? name.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase())
}

export type TraceItem =
  | { type: "step"; id: string; name: string; status: "running" | "ok" | "bad"; summary?: string; startedAt: number; ms?: number }
  | { type: "thinking"; text: string }

export type Part =
  | { kind: "text"; text: string }
  | { kind: "trace"; items: TraceItem[] }
  | { kind: "stage"; stageId: string; title: string; visual: "chart" | "visual" }
  | { kind: "note"; text: string; error: boolean }

export type Turn = {
  id: string
  question: string
  parts: Part[]
  status: "running" | "done"
  startedAt: number
  endedAt?: number
  usage: { input: number; output: number; cacheRead: number }
}

type ChatState = {
  conversationId: string | null
  busy: boolean
  turns: Turn[]
}

export const useChat = create<ChatState>(() => ({ conversationId: null, busy: false, turns: [] }))

export type StageItem = {
  id: string
  turnId: string
  title: string
  kind: "chart" | "visual"
  chart?: ChartPayload
  visual?: VisualPayload
}

type StageState = {
  items: StageItem[]
  focusId: string | null
  open: boolean
  expanded: boolean
}

export const useStage = create<StageState>(() => ({ items: [], focusId: null, open: false, expanded: false }))

export function focusStage(id: string) {
  useStage.setState({ focusId: id, open: true })
}

export function setStageOpen(open: boolean) {
  useStage.setState(open ? { open } : { open, expanded: false })
}

export function toggleStageExpanded() {
  useStage.setState((s) => ({ expanded: !s.expanded, open: true }))
}

let seq = 0
const nextId = (p: string) => `${p}${++seq}`

// Every change to the running turn goes through here, so React sees a new object each time.
function updateTurn(id: string, fn: (t: Turn) => Turn) {
  useChat.setState((s) => ({ turns: s.turns.map((t) => (t.id === id ? fn(t) : t)) }))
}

function withLastPart(t: Turn, kind: Part["kind"], make: () => Part, edit: (p: any) => Part): Turn {
  const last = t.parts[t.parts.length - 1]
  if (last?.kind === kind) return { ...t, parts: [...t.parts.slice(0, -1), edit(last)] }
  return { ...t, parts: [...t.parts, edit(make())] }
}

function note(turnId: string, text: string, error: boolean) {
  updateTurn(turnId, (t) => ({ ...t, parts: [...t.parts, { kind: "note", text, error }] }))
}

function handleEvent(turnId: string, ev: any) {
  switch (ev.type) {
    case "conversation":
      useChat.setState({ conversationId: ev.id })
      break
    case "text":
      updateTurn(turnId, (t) => withLastPart(t, "text", () => ({ kind: "text", text: "" }), (p) => ({ ...p, text: p.text + ev.text })))
      break
    case "thinking_start":
      updateTurn(turnId, (t) => withLastPart(t, "trace", () => ({ kind: "trace", items: [] }), (p) => ({ ...p, items: [...p.items, { type: "thinking", text: "" }] })))
      break
    case "thinking":
      updateTurn(turnId, (t) => withLastPart(t, "trace", () => ({ kind: "trace", items: [] }), (p) => {
        const items = [...p.items]
        const last = items[items.length - 1]
        if (last?.type === "thinking") items[items.length - 1] = { ...last, text: last.text + ev.text }
        else items.push({ type: "thinking", text: ev.text })
        return { ...p, items }
      }))
      break
    case "tool_start":
      updateTurn(turnId, (t) => withLastPart(t, "trace", () => ({ kind: "trace", items: [] }), (p) => ({
        ...p, items: [...p.items, { type: "step", id: ev.id, name: ev.name, status: "running", startedAt: performance.now() }],
      })))
      break
    case "tool_end":
      updateTurn(turnId, (t) => ({
        ...t,
        parts: t.parts.map((p) => p.kind !== "trace" ? p : {
          ...p,
          items: p.items.map((it) => it.type === "step" && it.id === ev.id
            ? { ...it, status: ev.ok ? "ok" : "bad", summary: ev.summary || undefined, ms: performance.now() - it.startedAt }
            : it),
        }),
      }))
      break
    case "ui":
      if (ev.action === "chart" || ev.action === "visual") {
        const item: StageItem = {
          id: nextId("v"), turnId, title: ev.title, kind: ev.action,
          ...(ev.action === "chart"
            ? { chart: { title: ev.title, spec: ev.spec, link: ev.link, time_field: ev.time_field } }
            : { visual: ev as VisualPayload }),
        }
        useStage.setState((s) => ({ items: [...s.items, item], focusId: item.id, open: true }))
        updateTurn(turnId, (t) => ({ ...t, parts: [...t.parts, { kind: "stage", stageId: item.id, title: item.title, visual: item.kind }] }))
      } else {
        applyMapUi(ev, (text) => note(turnId, text, true))
      }
      break
    case "usage":
      updateTurn(turnId, (t) => ({
        ...t,
        usage: {
          input: t.usage.input + ev.input + ev.cache_read + (ev.cache_write || 0),
          output: t.usage.output + ev.output,
          cacheRead: t.usage.cacheRead + ev.cache_read,
        },
      }))
      break
    case "notice":
      note(turnId, ev.message, false)
      break
    case "error":
      note(turnId, ev.message, true)
      break
  }
}

export async function send(text: string) {
  const { busy, conversationId } = useChat.getState()
  if (busy || !text.trim()) return
  const turn: Turn = {
    id: nextId("t"), question: text.trim(), parts: [], status: "running",
    startedAt: performance.now(), usage: { input: 0, output: 0, cacheRead: 0 },
  }
  useChat.setState((s) => ({ busy: true, turns: [...s.turns, turn] }))
  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: turn.question, conversation_id: conversationId, context: mapContext() }),
    })
    if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`)
    const reader = res.body.getReader()
    const decoder = new TextDecoder()
    let buf = ""
    for (;;) {
      const { value, done } = await reader.read()
      if (done) break
      buf += decoder.decode(value, { stream: true })
      let cut
      while ((cut = buf.indexOf("\n\n")) >= 0) {
        const chunk = buf.slice(0, cut)
        buf = buf.slice(cut + 2)
        for (const line of chunk.split("\n")) {
          if (line.startsWith("data: ")) handleEvent(turn.id, JSON.parse(line.slice(6)))
        }
      }
    }
  } catch (e) {
    note(turn.id, `Connection problem: ${(e as Error).message}`, true)
  } finally {
    updateTurn(turn.id, (t) => ({ ...t, status: "done", endedAt: performance.now() }))
    useChat.setState({ busy: false })
  }
}

export function newChat() {
  const { busy, conversationId } = useChat.getState()
  if (busy) return
  if (conversationId) fetch(`/api/reset/${conversationId}`, { method: "POST" })
  useChat.setState({ conversationId: null, turns: [] })
  useStage.setState({ items: [], focusId: null, open: false, expanded: false })
  resetForNewChat()
}
