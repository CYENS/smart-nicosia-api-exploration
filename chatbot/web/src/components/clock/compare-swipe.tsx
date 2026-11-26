/* Compare: a swipe across the map. Right of the line, the city at the clock's moment; left of
   it, the compared moment (the same time last week, yesterday, or a moment held). Drag the
   handle, or focus it and use the arrow keys. */
import { ChevronsLeftRightIcon } from "lucide-react"
import { useRef, type KeyboardEvent, type PointerEvent } from "react"

import { compareAt, compareName, fmtMoment, setSwipe, useClock } from "@/lib/clock"
import { useGeometry } from "@/lib/geometry"
import { STRIP_H } from "@/lib/layout"

function Tag({ children, side }: { children: React.ReactNode; side: "left" | "right" }) {
  return (
    <span
      className="panel-float absolute top-2 rounded-md px-2 py-1 font-readout text-[12.5px] font-semibold whitespace-nowrap"
      style={side === "left" ? { right: 10 } : { left: 10 }}
    >
      {children}
    </span>
  )
}

export function CompareSwipe() {
  const g = useGeometry()
  const other = useClock((s) => compareAt(s))
  const compare = useClock((s) => s.compare)
  const at = useClock((s) => s.at)
  const now = useClock((s) => s.now)
  const swipe = useClock((s) => s.swipe)
  const dragging = useRef(false)
  if (other === null) return null

  const left = g.rail + g.insets.left
  const width = Math.max(1, g.vw - left - g.insets.right)
  const x = left + swipe * width
  const top = STRIP_H + g.insets.top
  const share = (clientX: number) => setSwipe((clientX - left) / width)

  const onDown = (e: PointerEvent<HTMLDivElement>) => {
    dragging.current = true
    e.currentTarget.setPointerCapture(e.pointerId)
    share(e.clientX)
  }
  const onMove = (e: PointerEvent<HTMLDivElement>) => { if (dragging.current) share(e.clientX) }
  const onUp = () => { dragging.current = false }
  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    const step = e.shiftKey ? 0.2 : 0.05
    const next = { ArrowLeft: swipe - step, ArrowRight: swipe + step, Home: 0, End: 1 }[e.key]
    if (next === undefined) return
    e.preventDefault()
    setSwipe(next)
  }
  const name = compareName(compare, other)

  return (
    <div className="pointer-events-none absolute z-[5]" style={{ left: x, top, bottom: g.insets.bottom + 8 }}>
      <div className="absolute inset-y-0 -left-px w-0.5 bg-foreground/85 shadow-[0_0_0_1px_rgb(14_24_40_/_0.6)]" />
      <Tag side="left">{name === "last week" || name === "yesterday" ? `${name.replace(/^./, (c) => c.toUpperCase())}, ${fmtMoment(other)}` : fmtMoment(other)}</Tag>
      <Tag side="right"><span className="text-sodium">{at === null ? `Now, ${fmtMoment(now)}` : fmtMoment(at)}</span></Tag>
      <div
        role="slider"
        tabIndex={0}
        aria-label="Compare swipe: left shows the compared moment, right the clock's moment"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(swipe * 100)}
        aria-valuetext={`${Math.round(swipe * 100)}% of the map shows ${fmtMoment(other)}`}
        onPointerDown={onDown}
        onPointerMove={onMove}
        onPointerUp={onUp}
        onPointerCancel={onUp}
        onKeyDown={onKey}
        className="pointer-events-auto absolute top-1/2 left-0 flex size-10 -translate-x-1/2 -translate-y-1/2 cursor-ew-resize touch-none items-center justify-center rounded-full border-2 border-foreground/85 bg-console text-foreground shadow-lg outline-none focus-visible:ring-4 focus-visible:ring-ring/50"
      >
        <ChevronsLeftRightIcon className="size-5" aria-hidden="true" />
      </div>
    </div>
  )
}
