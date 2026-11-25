import { useEffect, useState } from "react"

// The time now, moved on every `ms`, for "12 min ago" readouts that stay true.
export function useNow(ms = 30_000) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), ms)
    return () => window.clearInterval(t)
  }, [ms])
  return now
}

// Whether a key press belongs to a field (so single-key shortcuts leave it alone).
export function typing(e: KeyboardEvent) {
  const t = e.target as HTMLElement | null
  return !!t?.closest("input, textarea, select, [contenteditable], [role=dialog]")
}
