import { cn } from "@/lib/utils"

// The moon as it looks tonight. `phase` is SunCalc's: 0 new, 0.25 first quarter, 0.5 full,
// 0.75 last quarter. Waxing lights the right side (as seen from Cyprus), waning the left.
export function MoonGlyph({ phase, className }: { phase: number; className?: string }) {
  const r = 10
  const k = Math.cos(2 * Math.PI * phase) // 1 new, -1 full
  const rx = Math.abs(k) * r
  const waxing = phase <= 0.5
  // From the top, along the lit limb to the bottom, then back up along the terminator.
  const limb = waxing ? 1 : 0
  const terminator = waxing ? (k > 0 ? 0 : 1) : (k > 0 ? 1 : 0)
  const lit = `M 0 ${-r} A ${r} ${r} 0 0 ${limb} 0 ${r} A ${rx} ${r} 0 0 ${terminator} 0 ${-r} Z`
  return (
    <svg viewBox="-12 -12 24 24" className={cn("shrink-0", className)} aria-hidden="true">
      <circle r={r} fill="#22324d" />
      <path d={lit} fill="#e8eefb" />
    </svg>
  )
}
