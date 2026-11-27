/* A stalled road feed: the national access point still answers, but its detectors have sent
   nothing new, so every road's newest measurement is old. The page says when they last sent,
   in the words people use ("14:05" today, "Thursday 23:42" this week, "19 November 23:42"
   before), and draws each road's usual speed for this hour instead, dotted, never as live. */

const TZ = "Asia/Nicosia"

function dayKey(d: Date) {
  return d.toLocaleDateString("en-CA", { timeZone: TZ }) // 2026-11-27
}

export function sinceWords(iso: string, now = new Date()): string {
  const t = new Date(iso)
  if (Number.isNaN(t.getTime())) return iso
  const time = t.toLocaleTimeString("en-GB", { timeZone: TZ, hour: "2-digit", minute: "2-digit", hour12: false })
  const days = Math.round((Date.parse(dayKey(now)) - Date.parse(dayKey(t))) / 86_400_000)
  if (days === 0) return time
  if (days > 0 && days < 7) return `${t.toLocaleDateString("en-GB", { timeZone: TZ, weekday: "long" })} ${time}`
  return `${t.toLocaleDateString("en-GB", { timeZone: TZ, day: "numeric", month: "long" })} ${time}`
}

// "The national road detectors have sent nothing new since Thursday 23:42."
export function stalledSentence(iso: string | null | undefined, now = new Date()): string {
  return iso
    ? `The national road detectors have sent nothing new since ${sinceWords(iso, now)}.`
    : "The national road detectors have sent nothing new for a while."
}

// For tight spaces: "Nothing new from the road detectors since Thursday 23:42".
export function stalledShort(iso: string | null | undefined, now = new Date()): string {
  return `Nothing new from the road detectors${iso ? ` since ${sinceWords(iso, now)}` : ""}`
}

// What the map shows meanwhile, in a line.
export function usualLine(usual: number): string {
  return usual > 0
    ? "Dotted roads show their usual speed for this hour. None of it is live."
    : "The twin hasn't recorded enough of this hour on this weekday to show the usual speeds."
}
