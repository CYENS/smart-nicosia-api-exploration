/* Small pieces the mobility panels share: lists of rows you can arrow through, a line's number
   in its colour, a road's speed against its free flow, and the road speeds legend. */
import type { ReactNode } from "react"

import { UsualLegend } from "@/components/mobility/stalled"
import { Item, ItemGroup } from "@/components/ui/item"
import { ROAD_BANDS, ROAD_COLORS } from "@/lib/mobility/map"
import { useMapUi } from "@/lib/map/engine"
import { useMobility } from "@/lib/mobility/store"
import { useRovingList } from "@/lib/roving"
import { cn } from "@/lib/utils"

// A list of rows that each open something: Tab enters on one row, the arrow keys move between
// them, Home and End jump, Enter opens.
export function RowList({ label, children, className }: { label: string; children: ReactNode; className?: string }) {
  const { ref, onKeyDown, onFocus } = useRovingList<HTMLDivElement>()
  return (
    <ItemGroup ref={ref} aria-label={label} onKeyDown={onKeyDown} onFocus={onFocus} className={cn("-mx-1.5 w-auto gap-0.5", className)}>
      {children}
    </ItemGroup>
  )
}

// One row: a shadcn Item rendered as a button, with hover, pressed and focus states that read on
// the console, and sodium when it is the current one (a bus's next stop).
export function Row({ rowKey, onSelect, current, label, className, children }: {
  rowKey: string; onSelect: () => void; current?: boolean; label?: string; className?: string; children: ReactNode
}) {
  return (
    <div role="listitem">
      <Item
        size="xs"
        data-row=""
        data-key={rowKey}
        aria-current={current ? "step" : undefined}
        className={cn(
          "cursor-pointer gap-2.5 px-1.5 py-1.5 text-left hover:bg-accent/60 active:bg-accent",
          "aria-[current=step]:border-sodium/35 aria-[current=step]:bg-sodium/10",
          className,
        )}
        render={<button type="button" aria-label={label} onClick={onSelect} />}
      >
        {children}
      </Item>
    </div>
  )
}

// A line's number on its own colour, as on the bus. The colour carries identity, the number
// says it.
export function LinePill({ line, color, textColor, className }: { line: string; color?: string | null; textColor?: string | null; className?: string }) {
  return (
    <span
      className={cn("inline-flex h-5 min-w-7 shrink-0 items-center justify-center rounded-md px-1.5 font-readout text-[12.5px] leading-none font-semibold tabular-nums", className)}
      style={{ background: color ?? "var(--raised)", color: textColor ?? "#0e1828" }}
    >
      {line}
    </span>
  )
}

export function useRoadColors() {
  const base = useMapUi((s) => s.basemap)
  return ROAD_COLORS[base === "day" ? "day" : "night"]
}

// The speed against the road's free flow: a bar to the speed, a tick at free flow and, when the
// memory has it, a hollow tick at the usual for this hour. Read as a meter by screen readers.
export function SpeedMeter({ speed, free, usual, state, label }: {
  speed: number | null; free: number | null; usual?: number | null; state: string; label: string
}) {
  const colors = useRoadColors()
  const top = Math.max(free ?? 0, speed ?? 0, usual ?? 0, 10) * 1.12
  const pct = (v: number) => `${Math.min(100, (100 * v) / top)}%`
  return (
    <div
      role="meter"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={Math.round(top)}
      aria-valuenow={speed ?? undefined}
      aria-valuetext={speed === null ? "no reading" : `${Math.round(speed)} km/h${free ? ` of ${Math.round(free)} free flow` : ""}`}
      className="relative h-2.5 w-full rounded-full bg-raised"
    >
      {speed !== null && (
        <div className="absolute inset-y-0 left-0 rounded-full" style={{ width: pct(speed), background: colors[state] ?? colors.no_reading }} />
      )}
      {usual ? (
        <span className="absolute -top-1 -bottom-1 w-[2px] -translate-x-1/2 rounded-full border border-muted-foreground bg-transparent" style={{ left: pct(usual) }} aria-hidden="true" />
      ) : null}
      {free ? <span className="absolute -top-1.5 -bottom-1.5 w-[2px] -translate-x-1/2 rounded-full bg-foreground" style={{ left: pct(free) }} aria-hidden="true" /> : null}
    </div>
  )
}

export function RoadLegend({ className }: { className?: string }) {
  const colors = useRoadColors()
  const stalled = useMobility((s) => s.roads.status === "stalled")
  if (stalled) return <UsualLegend className={className} /> // no road has a live band to show
  return (
    <ul className={cn("grid grid-cols-2 gap-x-3 gap-y-1.5 text-[12.5px] text-muted-foreground", className)} aria-label="Road speed legend">
      {ROAD_BANDS.map((b, i) => (
        <li key={b.state} className="flex items-center gap-2" title={b.hint}>
          <span className="shrink-0 rounded-full" style={{ background: colors[b.state], width: 18, height: 3 + i }} aria-hidden="true" />
          <span>{b.label}</span>
        </li>
      ))}
      <li className="col-span-2 flex items-center gap-2">
        <span className="h-[2px] w-[18px] shrink-0 border-t-2 border-dashed" style={{ borderColor: colors.no_reading }} aria-hidden="true" />
        <span>No reading in the last 30 minutes</span>
      </li>
    </ul>
  )
}
