/* The Scenario lab's state: the scenarios and their parameter schemas, the form's values per
   scenario, the open run and the one compared with it, the saved runs, and the calls to the
   server (/api/scenarios). Every number on the lab's page comes from a run the server made. */
import { create } from "zustand"

import type { RunSummary, ScenarioDef, ScenarioRun } from "@/lib/registries/scenarios"

export type Period = { name: string; label: string; days: number }
export type LabTab = "setup" | "results" | "saved"

type Lab = {
  defs: ScenarioDef[]
  periods: Period[]
  status: "idle" | "loading" | "ready" | "error"
  loadError: string | null
  scenario: string
  period: string
  values: Record<string, Record<string, unknown>> // per scenario: what the form holds
  run: ScenarioRun | null
  compare: ScenarioRun | null
  running: boolean
  runError: string | null
  opening: string | null // a run being fetched by its id (a link, the chat)
  saved: RunSummary[]
  savedStatus: "idle" | "loading" | "ready" | "error"
  tab: LabTab
}

export const useLab = create<Lab>(() => ({
  defs: [], periods: [], status: "idle", loadError: null, scenario: "lighting", period: "last_30_days",
  values: {}, run: null, compare: null, running: false, runError: null, opening: null,
  saved: [], savedStatus: "idle", tab: "setup",
}))

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`
    try {
      const body = await res.json()
      if (body?.detail) detail = String(body.detail)
    } catch { /* not JSON */ }
    throw new Error(detail)
  }
  return res.json() as Promise<T>
}

export function defOf(id: string | undefined | null): ScenarioDef | undefined {
  return useLab.getState().defs.find((d) => d.id === id)
}

function defaults(def: ScenarioDef): Record<string, unknown> {
  return Object.fromEntries(Object.entries(def.params.properties).map(([k, p]) => [k, p.default]))
}

export async function loadScenarios() {
  const s = useLab.getState()
  if (s.status === "loading" || s.status === "ready") return
  useLab.setState({ status: "loading", loadError: null })
  try {
    const body = await json<{ scenarios: ScenarioDef[]; periods: Period[] }>(await fetch("/api/scenarios"))
    const values = { ...useLab.getState().values }
    for (const d of body.scenarios) values[d.id] = { ...defaults(d), ...(values[d.id] ?? {}) }
    const scenario = body.scenarios.some((d) => d.id === useLab.getState().scenario)
      ? useLab.getState().scenario : body.scenarios[0]?.id ?? ""
    useLab.setState({ defs: body.scenarios, periods: body.periods, values, scenario, status: "ready" })
  } catch (e) {
    useLab.setState({ status: "error", loadError: (e as Error).message })
  }
}

export function setScenario(id: string) {
  useLab.setState({ scenario: id, runError: null })
}

export function setPeriod(name: string) {
  useLab.setState({ period: name })
}

// `forScenario`: the scenario the value belongs to (an open run's, for its assumptions), else the
// one chosen on Set up.
export function setValue(key: string, value: unknown, forScenario?: string) {
  const { values } = useLab.getState()
  const scenario = forScenario ?? useLab.getState().scenario
  useLab.setState({ values: { ...values, [scenario]: { ...(values[scenario] ?? {}), [key]: value } } })
}

export function resetValues() {
  const { scenario, values } = useLab.getState()
  const def = defOf(scenario)
  if (def) useLab.setState({ values: { ...values, [scenario]: defaults(def) } })
}

export async function loadSaved() {
  useLab.setState({ savedStatus: "loading" })
  try {
    const body = await json<{ runs: RunSummary[] }>(await fetch("/api/scenarios/runs?limit=30"))
    useLab.setState({ saved: body.runs, savedStatus: "ready" })
  } catch {
    useLab.setState({ savedStatus: "error" })
  }
}

// Run the scenario on the form's values. The run opens on the results; returns it, or null.
export async function runScenario(overrides: Record<string, unknown> = {}, forScenario?: string): Promise<ScenarioRun | null> {
  const { period, values, running } = useLab.getState()
  const scenario = forScenario ?? useLab.getState().scenario
  if (running || !scenario) return null
  const params = { ...(values[scenario] ?? {}), ...overrides }
  useLab.setState({ running: true, runError: null })
  try {
    const run = await json<ScenarioRun>(await fetch(`/api/scenarios/${encodeURIComponent(scenario)}/run`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ params, period }),
    }))
    const keep = useLab.getState().compare
    useLab.setState({
      run, running: false, tab: "results",
      compare: keep && keep.id !== run.id ? keep : null,
      values: { ...useLab.getState().values, [scenario]: { ...run.params } },
    })
    void loadSaved()
    return run
  } catch (e) {
    useLab.setState({ running: false, runError: (e as Error).message })
    return null
  }
}

async function fetchRun(id: string): Promise<ScenarioRun> {
  return json<ScenarioRun>(await fetch(`/api/scenarios/runs/${encodeURIComponent(id)}`))
}

// Open a stored run (a link, the chat, the saved list), with another beside it if asked.
export async function openRun(id: string, compare: string | null = null) {
  useLab.setState({ opening: id, runError: null })
  try {
    const [run, other] = await Promise.all([fetchRun(id), compare ? fetchRun(compare) : Promise.resolve(null)])
    const values = { ...useLab.getState().values, [run.scenario]: { ...run.params } }
    useLab.setState({
      run, compare: other, opening: null, tab: "results", values, scenario: run.scenario,
      period: run.period.name ?? useLab.getState().period,
    })
  } catch (e) {
    useLab.setState({ opening: null, runError: `That run can't be opened: ${(e as Error).message}` })
  }
}

export async function setCompare(id: string | null) {
  if (!id) {
    useLab.setState({ compare: null })
    return
  }
  try {
    useLab.setState({ compare: await fetchRun(id) })
  } catch (e) {
    useLab.setState({ runError: `That run can't be compared: ${(e as Error).message}` })
  }
}

export function closeRun() {
  useLab.setState({ run: null, compare: null, tab: "setup" })
}

export function briefUrl(run: ScenarioRun, compare: ScenarioRun | null) {
  return `/api/scenarios/runs/${run.id}/brief${compare ? `?compare=${compare.id}` : ""}`
}

export function csvUrl(run: ScenarioRun) {
  return `/api/scenarios/runs/${run.id}/series.csv`
}

// -- formatting ------------------------------------------------------------------------------

export function fmtNum(v: number | null | undefined, digits = 0) {
  if (v === null || v === undefined || !Number.isFinite(v)) return "n/a"
  return Math.abs(v).toLocaleString("en-GB", { minimumFractionDigits: digits, maximumFractionDigits: digits })
}

export function fmtSigned(v: number | null | undefined, digits = 0) {
  if (v === null || v === undefined || !Number.isFinite(v)) return "n/a"
  const s = fmtNum(v, digits)
  return v > 0 ? `+${s}` : v < 0 ? `-${s}` : s
}

export function fmtWhen(iso: string) {
  return new Date(iso).toLocaleString("en-GB", {
    timeZone: "Asia/Nicosia", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit",
  })
}
