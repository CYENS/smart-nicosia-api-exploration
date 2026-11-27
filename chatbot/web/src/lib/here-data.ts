/* The sections of "What's here?" as the server sends them (chatbot/here.py), one at a time, so
   the inspector shows each as soon as it is read and each can fail on its own. */

export const SECTION_KEYS = ["address", "buses", "parking", "registers", "building", "lamps"] as const
export type SectionKey = (typeof SECTION_KEYS)[number]

export type HereDeparture = {
  line: string; line_id: string; color: string | null; text_color: string | null; headsign: string
  scheduled: string; expected: string | null; realtime: boolean; delay_s: number | null; in_min: number
}
export type HereRow = {
  id?: string; kind?: string; name?: string; detail?: string | null; distance_m?: number
  lon?: number; lat?: number; state?: string; color?: string; departures?: HereDeparture[]; lines?: string[]
}
export type HereSource = { name: string; detail?: string | null; url?: string | null; as_of?: string | null }
export type HereSection = {
  key: SectionKey; title: string; status: "ok" | "none" | "unknown" | "error"
  summary: string | null; detail?: string | null; message: string | null; note?: string
  rows: HereRow[]; facts: { label: string; value: string | number }[]; sources: HereSource[]; as_of: string | null
  live_only?: boolean; night?: boolean; under?: boolean
}

const cache = new Map<string, { at: number; section: HereSection }>()
const LIVE_MS = 20_000
const PAST_MS = 10 * 60_000

// One section of a spot at the clock's moment (null: now), cached briefly so going back to a
// spot is instant. A section that failed is not kept.
export async function fetchHere(id: string, key: SectionKey, moment: string | null, signal?: AbortSignal, fresh = false) {
  const k = `${id}|${key}|${moment ?? ""}`
  const hit = cache.get(k)
  if (!fresh && hit && Date.now() - hit.at < (moment ? PAST_MS : LIVE_MS)) return hit.section
  const p = new URLSearchParams({ section: key, ...(moment ? { at: moment } : {}) })
  const res = await fetch(`/api/entities/${encodeURIComponent(id)}/here?${p}`, { signal })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(typeof body.detail === "string" ? body.detail : `the server answered ${res.status}`)
  }
  const section = (await res.json()).sections[0] as HereSection
  if (section.status !== "error") cache.set(k, { at: Date.now(), section })
  return section
}
