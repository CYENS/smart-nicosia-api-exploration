/* The conversation: turns streamed from /api/chat (Server-Sent Events), and the stage that holds
   the charts and custom visuals they bring. Map commands go straight to the map engine.
   The latest turn can be stopped while it runs, and asked again (Retry) or changed (Edit) once
   it is over: the server takes it out of its history and the page takes out its charts and its
   layers. */
import { create } from "zustand"

import { ACTIONS, knownActions } from "@/lib/actions"
import type { ChartPayload } from "@/lib/charts"
import { applyClockCommand, clockContext } from "@/lib/clock"
import type { EntityRef } from "@/lib/entities"
import { closeInspector, inspectId, inspectingRef } from "@/lib/inspector"
import { useLayout } from "@/lib/layout"
import { applyMapUi, mapContext, removeLayer, resetForNewChat } from "@/lib/map/engine"
import { turnCard } from "@/lib/registries/turn-cards"
import { openScenarioRun } from "@/lib/scenarios/open"
import { applyViewCommand, viewContext } from "@/lib/view"
import type { VisualPayload } from "@/lib/visual"

/* eslint-disable @typescript-eslint/no-explicit-any */

export const TOOL_LABELS: Record<string, string> = {
  city_overview: "Reading the city's sensors", find_devices: "Finding devices",
  get_history: "Replaying history",
  query_history: "Reading the twin's memory", run_sql: "Querying the twin's memory",
  get_device: "Reading a device", get_alarms: "Checking alarms", get_traffic: "Counting traffic",
  get_speed: "Checking traffic speed", get_weather: "Checking the weather",
  geocode: "Looking up a place", find_places: "Searching places", plan_route: "Planning the route",
  reachable_area: "Working out the reachable area", show_on_map: "Drawing on the map",
  find_dangerous_buildings: "Reading the dangerous-building registers",
  where_am_i: "Working out where you are",
  get_buildings_3d: "Reading the DLS 3D buildings",
  find_height_changes: "Comparing the 2014 survey with the 2019 laser flight",
  get_entity: "Looking it up in the twin", open_inspector: "Opening it in the inspector",
  map_fly_to: "Moving the map", map_callouts: "Adding callouts", map_clear: "Clearing the map",
  show_chart: "Drawing a chart", show_visual: "Drawing a custom visual", web_search: "Searching the web",
  set_city_time: "Setting the city clock",
  get_buses: "Finding the buses on the road", get_departures: "Reading the departures",
  plan_transit: "Planning the bus trip", get_road_speeds: "Reading the road speeds",
  locate_web_places: "Putting the web results on the map",
  list_incidents: "Reading the incident inbox", explain_incident: "Looking at what happened around it",
  create_watch: "Preparing the watch",
  sun_and_shade: "Reading the sun and shade", environment_surface: "Drawing the environment surface",
  find_high_places: "Finding high places with a view", view_from: "Flying you to the view",
  list_scenarios: "Reading the scenario lab", run_scenario: "Running the scenario",
  compare_scenarios: "Comparing the two runs", open_scenario: "Opening it in the scenario lab",
  measured_stations: "Reading the measuring stations", compare_with_reference: "Comparing the sensors with the official stations",
}

export function toolLabel(name: string) {
  return TOOL_LABELS[name] ?? name.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase())
}

export type StepStatus = "running" | "ok" | "bad" | "stopped"

export type TraceItem =
  | { type: "step"; id: string; name: string; status: StepStatus; summary?: string; startedAt: number; ms?: number }
  | { type: "thinking"; text: string }

export type Part =
  | { kind: "text"; text: string }
  | { kind: "trace"; items: TraceItem[] }
  | { kind: "stage"; stageId: string; title: string; visual: "chart" | "visual" }
  | { kind: "note"; text: string; error: boolean }
  | { kind: "card"; card: string; data: Record<string, unknown> } // lib/registries/turn-cards.ts

export type Turn = {
  id: string
  question: string
  subject?: EntityRef // the thing the question was asked about (a chip in the composer)
  parts: Part[]
  status: "running" | "done" | "stopped"
  startedAt: number
  endedAt?: number
  usage: { input: number; output: number; cacheRead: number }
  index?: number // the server's number for this turn: Retry and Edit replace it
  run?: string // the server's id for this run of it: Stop names it
  next?: string[] // suggested next questions, written by the model with the answer
  choices?: string[] // the options, when the answer is a clarifying question
  actions?: string[] // what the page can do for it (lib/actions.ts): share the location...
  layers: string[] // the map layers it drew, to take off the map when it is replaced
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
      updateTurn(turnId, (t) => ({ ...t, run: ev.run }))
      break
    case "turn":
      updateTurn(turnId, (t) => ({ ...t, index: ev.index }))
      break
    case "follow_ups":
      updateTurn(turnId, (t) => ({ ...t, next: ev.next, choices: ev.choices, actions: knownActions(ev.do) }))
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
            ? { chart: { title: ev.title, spec: ev.spec, link: ev.link, time_field: ev.time_field, scrub: ev.scrub, sources: ev.sources } }
            : { visual: ev as VisualPayload }),
        }
        useStage.setState((s) => ({ items: [...s.items, item], focusId: item.id, open: true }))
        updateTurn(turnId, (t) => ({ ...t, parts: [...t.parts, { kind: "stage", stageId: item.id, title: item.title, visual: item.kind }] }))
      } else if (ev.action === "clock") {
        applyClockCommand(ev)
      } else if (ev.action === "view") {
        applyViewCommand(ev)
      } else if (ev.action === "inspect") {
        inspectId(String(ev.id), { at: ev.at ?? undefined, fly: ev.fly !== false })
      } else if (ev.action === "scenario") {
        openScenarioRun(String(ev.run), ev.compare ? String(ev.compare) : null)
      } else if (turnCard(ev.action)) {
        updateTurn(turnId, (t) => ({ ...t, parts: [...t.parts, { kind: "card", card: ev.action, data: ev }] }))
      } else {
        if (ev.action === "show_layer" && ev.layer?.ref) {
          updateTurn(turnId, (t) => ({ ...t, layers: [...t.layers, String(ev.layer.ref)] }))
        }
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

// The turn being answered: its fetch, so Stop can abort it.
let running: { turnId: string; controller: AbortController } | null = null

// Steps still running when a turn ends were cut short.
function settle(parts: Part[]): Part[] {
  return parts.map((p) => p.kind !== "trace" || !p.items.some((i) => i.type === "step" && i.status === "running") ? p : {
    ...p,
    items: p.items.map((i) => i.type === "step" && i.status === "running" ? { ...i, status: "stopped" as const } : i),
  })
}

// What a replaced turn put on the page goes with it: its charts leave the stage and its layers
// leave the map (unless another turn drew the same layer). The new turn takes the old one's id
// and place (in send), so the conversation refills that row instead of jumping elsewhere.
function clearTurn(turn: Turn) {
  const others = useChat.getState().turns.filter((t) => t.id !== turn.id)
  const kept = new Set(others.flatMap((t) => t.layers))
  for (const ref of new Set(turn.layers)) if (!kept.has(ref)) removeLayer(ref)
  useStage.setState((s) => {
    const items = s.items.filter((i) => i.turnId !== turn.id)
    if (items.length === s.items.length) return s
    const focusId = items.some((i) => i.id === s.focusId) ? s.focusId : (items[items.length - 1]?.id ?? null)
    return { items, focusId, open: s.open && items.length > 0, expanded: s.expanded && items.length > 0 }
  })
}

export async function send(text: string, opts: { subject?: EntityRef | null; replace?: Turn } = {}) {
  const { busy } = useChat.getState()
  if (busy || !text.trim()) return
  const { replace } = opts
  if (replace) clearTurn(replace)
  const { conversationId } = useChat.getState()
  const subject = opts.subject ?? undefined
  const turn: Turn = {
    id: replace?.id ?? nextId("t"), question: text.trim(), subject, parts: [], status: "running",
    startedAt: performance.now(), usage: { input: 0, output: 0, cacheRead: 0 }, layers: [],
  }
  const controller = new AbortController()
  running = { turnId: turn.id, controller }
  useChat.setState((s) => ({ busy: true, turns: [...s.turns.filter((t) => t.id !== replace?.id), turn] }))
  try {
    const context = { ...mapContext(), clock: clockContext(), inspecting: inspectingRef(), subject: subject ?? null, viewing: viewContext() }
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: turn.question, conversation_id: conversationId, context, replace_turn: replace?.index ?? null }),
      signal: controller.signal,
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
    if (!controller.signal.aborted) note(turn.id, `Connection problem: ${(e as Error).message}`, true)
  } finally {
    const stopped = controller.signal.aborted
    updateTurn(turn.id, (t) => ({ ...t, status: stopped ? "stopped" : "done", endedAt: performance.now(), parts: settle(t.parts) }))
    if (running?.turnId === turn.id) running = null
    useChat.setState({ busy: false })
  }
}

// Stop the answer being written. The page keeps what came so far; the server forgets the turn.
export function stop() {
  if (!running) return
  const { turnId, controller } = running
  const { conversationId, turns } = useChat.getState()
  const run = turns.find((t) => t.id === turnId)?.run
  controller.abort()
  // Closing the stream stops the turn too, but only once the server next writes to it.
  if (conversationId && run) fetch(`/api/stop/${conversationId}/${run}`, { method: "POST" }).catch(() => {})
}

// Ask the latest question again, in place of its answer.
export function retry(turn: Turn) {
  return send(turn.question, { subject: turn.subject, replace: turn })
}

// An action chip. When it gave the turn what it was missing (the location), the turn's question
// is asked again in its place, so the answer just appears and the history stays clean. When it
// fails, the turn says why and offers what to do instead.
export async function runAction(turnId: string, key: string, opts: { opener?: HTMLElement | null; keyboard?: boolean } = {}) {
  const action = ACTIONS[key]
  if (!action) return
  const outcome = await action.run(opts.opener ?? null)
  const { turns, busy } = useChat.getState()
  const turn = turns.find((t) => t.id === turnId)
  if (!turn) return
  if (outcome === "continue") {
    if (busy || turns[turns.length - 1] !== turn) return // they asked something else meanwhile
    void retry(turn)
    if (opts.keyboard) focusComposer()
  } else if (outcome === "cancelled") {
    if (opts.opener?.isConnected) opts.opener.focus()
  } else if (outcome !== "done") {
    note(turnId, outcome.failed, false)
    updateTurn(turnId, (t) => ({
      ...t,
      actions: [...outcome.offer, ...(t.actions ?? []).filter((k) => k !== key && !outcome.offer.includes(k))],
    }))
    // The chip that failed is gone: the keyboard goes to what replaced it.
    if (opts.keyboard) requestAnimationFrame(() => document.querySelector<HTMLElement>(`[data-action="${outcome.offer[0]}"]`)?.focus())
  }
}

// Ask a changed question in place of the latest one.
export function edit(turn: Turn, text: string) {
  return send(text, { subject: turn.subject, replace: turn })
}

// The answer as markdown, for Copy: the text the user read, without the trace or the tags.
export function answerText(turn: Turn) {
  return turn.parts.flatMap((p) => (p.kind === "text" && p.text.trim() ? [p.text.trim()] : [])).join("\n\n")
}

export function newChat() {
  const { busy, conversationId } = useChat.getState()
  if (busy) return
  if (conversationId) fetch(`/api/reset/${conversationId}`, { method: "POST" })
  useChat.setState({ conversationId: null, turns: [] })
  useStage.setState({ items: [], focusId: null, open: false, expanded: false })
  useComposer.setState({ subject: null })
  closeInspector()
  resetForNewChat()
}

// The composer's chip: the thing the next question is about ("Ask about this" in the inspector).
type ComposerState = { subject: EntityRef | null; focusAt: number }
export const useComposer = create<ComposerState>(() => ({ subject: null, focusAt: 0 }))

// Put the keyboard in the composer (after a turn the keyboard was in went away).
export function focusComposer() {
  useComposer.setState({ focusAt: Date.now() })
}

export function askAbout(ref: EntityRef) {
  useComposer.setState({ subject: ref, focusAt: Date.now() })
  useLayout.setState({ chatOpen: true })
  // A phone has room for one sheet: the conversation takes over from the inspector.
  if (!window.matchMedia("(min-width: 768px)").matches) closeInspector()
}
