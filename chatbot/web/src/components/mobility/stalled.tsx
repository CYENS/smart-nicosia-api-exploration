/* What the road speeds show while the national detectors have stalled: the legend for the
   dotted usual speeds, and the sentence that says when the detectors last sent. */
import { useMapUi } from "@/lib/map/engine"
import { ROAD_COLORS, USUAL_COLORS } from "@/lib/mobility/map"
import { stalledSentence, usualLine } from "@/lib/mobility/stalled"
import { useMobility } from "@/lib/mobility/store"
import { cn } from "@/lib/utils"

// A row of round dots, as the map draws the usual: wider is usually slower.
function Dots({ color, size }: { color: string; size: number }) {
  return (
    <span className="flex w-[22px] shrink-0 items-center justify-between" aria-hidden="true">
      {[0, 1, 2].map((i) => <span key={i} className="shrink-0 rounded-full" style={{ background: color, width: size, height: size }} />)}
    </span>
  )
}

export function UsualLegend({ className }: { className?: string }) {
  const base = useMapUi((s) => s.basemap) === "day" ? "day" : "night"
  const colors = ROAD_COLORS[base]
  const usual = USUAL_COLORS[base]
  const shown = useMobility((s) => s.roads.usual)
  return (
    <ul className={cn("flex flex-col gap-1.5 text-[12.5px] text-muted-foreground", className)} aria-label="Road speed legend while the detectors have stalled">
      {shown > 0 && (
        <li className="flex items-center gap-2">
          <Dots color={usual} size={4} />
          <span><span className="text-foreground">Usual for this hour</span>, not live. Wider is usually slower.</span>
        </li>
      )}
      <li className="flex items-center gap-2">
        <span className="h-[2px] w-[22px] shrink-0 border-t-2 border-dashed" style={{ borderColor: colors.no_reading }} aria-hidden="true" />
        <span>No reading</span>
      </li>
    </ul>
  )
}

// The sentence and what the map shows meanwhile, for panels with room for both.
export function StalledNote({ className }: { className?: string }) {
  const roads = useMobility((s) => s.roads)
  const summary = useMobility((s) => s.summary?.roads)
  const at = roads.lastMeasuredAt ?? summary?.last_measured_at ?? null
  const usual = roads.status === "stalled" ? roads.usual : (summary?.usual ?? 0)
  return (
    <div className={cn("flex flex-col gap-1", className)} role="status">
      <p className="text-[13px] leading-snug font-medium text-signal">{stalledSentence(at)}</p>
      <p className="text-[12.5px] leading-snug text-muted-foreground">
        The feed still answers, with the same old measurement. {usualLine(usual)}
      </p>
    </div>
  )
}
