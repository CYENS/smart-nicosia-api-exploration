/* The city in plain words, from what /api/overview says (through the city clock, so at a past
   moment it reads as it was then). The now line, the topic cards and the Layers sheet's summaries
   all say it the same way. */
import { useMemo } from "react"

import { useClock, type Strip } from "@/lib/clock"
import { skyAt } from "@/lib/map/sky"

export const AIR_WORDS: Record<string, string> = {
  very_low: "Very good", low: "Good", medium: "Moderate", high: "Poor", very_high: "Very poor",
}

// The air stations' bands, best first, as the map colours them (STATE_COLORS on the server).
export const AIR_BANDS = ["very_low", "low", "medium", "high", "very_high"] as const

export const cap = (s: string) => s.replace(/^./, (c) => c.toUpperCase())

// The strip at the clock's moment, and whether that moment is now.
export function useStrip() {
  const strip = useClock((s) => s.strip)
  const past = useClock((s) => s.at !== null)
  return { strip, past, loading: strip === null }
}

// The real sky at the clock's moment: when it gets dark (or light) next.
export function useSky() {
  const at = useClock((s) => s.at)
  const now = useClock((s) => s.now)
  return useMemo(() => skyAt(new Date(at ?? now)), [at, now])
}

export function airWords(air: Strip["air"]) {
  if (!air || air.unknown || !air.band) return null
  return AIR_WORDS[air.band] ?? null
}

// "Easy", "Getting busy" or "Hard": how parking is, from the share of bays free.
export function parkingWord(p: Strip["parking"]) {
  if (!p || p.unknown || !p.total) return null
  const share = (p.free ?? 0) / p.total
  return share >= 0.4 ? "Easy" : share >= 0.15 ? "Getting busy" : "Hard"
}

export function fmtCount(n: number | undefined | null) {
  return (n ?? 0).toLocaleString("en-GB")
}
