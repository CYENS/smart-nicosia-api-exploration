import { cn } from "@/lib/utils"
import { WALLS_PATH } from "@/lib/walls"

// The outline of Nicosia's Venetian walls: the logo, and (working) the sign that the assistant
// is busy, tracing itself round the eleven bastions.
export function WallsMark({ className, working = false }: { className?: string; working?: boolean }) {
  return (
    <svg viewBox="-12 -12 24 24" className={cn(working && "walls-working", className)} aria-hidden="true">
      <path d={WALLS_PATH} pathLength={100} fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinejoin="round" />
    </svg>
  )
}
