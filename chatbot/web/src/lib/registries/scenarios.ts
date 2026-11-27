/* The Scenario lab's page contract: the shapes the server sends (chatbot/scenarios/base.py) and
   the registry where a scenario adds its own sections to its result view.

   A scenario's module calls registerScenarioSection({ scenario: "waste", id: "rounds", title,
   order, Section }) at import; the lab draws the generic pieces (the KPI cards, the split map,
   the charts, the assumptions and their sensitivity, the backtest) and then each registered
   section of the open run's scenario, in order. A section gets the run (and the compared run,
   when two are side by side). */
import type { ComponentType } from "react"

export type Kpi = {
  id: string
  label: string
  unit: string
  baseline: number | null
  scenario: number | null
  delta: number | null
  delta_pct: number | null
  better: "lower" | "higher"
  low: number | null // the scenario value's range across the assumptions
  high: number | null
  delta_low: number | null // the change's range
  delta_high: number | null
  digits: number
  note?: string | null
}

export type Assumption = {
  id: string; label: string; value: number; unit: string; source: string
  low: number; high: number; description?: string
}

export type Sensitivity = {
  id: string; label: string; kpi: string; unit: string
  value_low: number; value_high: number; at_low: number | null; at_high: number | null; base: number | null
}

export type Backtest = {
  metric: string; unit: string; measured: number; modelled: number; error_pct: number
  period: string; note: string; rows?: { time: string; measured: number | null; modelled: number | null }[]
  [k: string]: unknown
}

export type ScenarioFeature = {
  type: "Feature"
  geometry: { type: string; coordinates: unknown }
  properties: { id: string; state: string; color: string; label?: string; [k: string]: unknown }
}

export type ScenarioLayer = {
  type: "FeatureCollection"
  features: ScenarioFeature[]
  meta: {
    title: string; legend: Record<string, string>; variant?: "baseline" | "scenario" | null
    value_field?: string; unit?: string; geometry?: string; legend_labels?: Record<string, string>
    show?: boolean // a layer without a variant drawn over both sides of the split by default
    chat?: boolean // run_scenario also hands it to the chat as a layer_ref
  }
}

export type SeriesRow = { metric: string; time: string; variant: string; value: number | null }
export type ChartDef = { metric: string; title: string; unit: string; x: "time" | "date" | "month" | "category"; kind?: string; note?: string }

export type ScenarioResult = {
  kpis: Kpi[]
  layers: Record<string, ScenarioLayer>
  series: SeriesRow[]
  charts: ChartDef[]
  assumptions: Assumption[]
  sensitivity: Sensitivity[]
  backtest: Backtest | null
  details: Record<string, any> // eslint-disable-line @typescript-eslint/no-explicit-any
  sources: string[]
  notes: string[]
}

export type ScenarioPeriod = { name: string | null; start: string; end: string; days: number; label: string }

export type ScenarioRun = {
  id: string
  scenario: string
  scenario_title?: string
  label: string
  params: Record<string, unknown>
  period: ScenarioPeriod
  result: ScenarioResult
  created: string
  saved: boolean
}

export type RunSummary = Pick<ScenarioRun, "id" | "scenario" | "label" | "period" | "created" | "saved"> & { kpis: Kpi[] }

// A JSON schema property as the server writes it (chatbot/scenarios/base.py's docstring).
export type ParamSchema = {
  type: "boolean" | "integer" | "number" | "string"
  title: string
  description?: string
  default: unknown
  minimum?: number
  maximum?: number
  enum?: string[]
  "x-unit"?: string
  "x-step"?: number
  "x-assumption"?: boolean
  "x-source"?: string
  "x-group"?: string
  "x-when"?: string
}

export type ScenarioDef = {
  id: string; title: string; summary: string; question: string; main_kpi: string
  params: { type: "object"; properties: Record<string, ParamSchema> }
  sources: string[]; method: string
}

export type ScenarioSection = {
  scenario: string // the scenario id it belongs to
  id: string
  title: string
  order: number // after the generic pieces, in this order
  Section: ComponentType<{ run: ScenarioRun; compare: ScenarioRun | null }>
}

const sections: ScenarioSection[] = []

export function registerScenarioSection(s: ScenarioSection) {
  const i = sections.findIndex((x) => x.scenario === s.scenario && x.id === s.id)
  if (i >= 0) sections.splice(i, 1)
  sections.push(s)
  sections.sort((a, b) => a.order - b.order)
}

export function scenarioSections(scenario: string): ScenarioSection[] {
  return sections.filter((s) => s.scenario === scenario)
}

// What a group of parameters is called on the form; a scenario adds its own groups here.
export const PARAM_GROUPS: Record<string, string> = {}

export function registerParamGroups(groups: Record<string, string>) {
  Object.assign(PARAM_GROUPS, groups)
}
