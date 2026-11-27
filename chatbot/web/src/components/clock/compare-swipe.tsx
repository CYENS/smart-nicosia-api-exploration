/* Compare: a swipe across the map. Right of the line, the city at the clock's moment; left of
   it, the compared moment (the same time last week, yesterday, or a moment held), or the DLS
   aerial photo of 1993 or 1963 (lib/map/past-photo.ts). The handle is
   a Base UI slider (the primitive under shadcn's Slider) whose control spans the open map: drag
   the handle, or focus it and use the arrow keys, Page Up and Down, Home and End. Only the
   handle takes the pointer, so the map still pans everywhere else. */
import { Slider as SliderPrimitive } from "@base-ui/react/slider"
import { ChevronsLeftRightIcon } from "lucide-react"

import { compareAt, compareName, comparePhoto, fmtMoment, setSwipe, useClock } from "@/lib/clock"
import { useGeometry } from "@/lib/geometry"

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
  const photo = useClock((s) => comparePhoto(s))
  const compare = useClock((s) => s.compare)
  const at = useClock((s) => s.at)
  const now = useClock((s) => s.now)
  const swipe = useClock((s) => s.swipe)
  if (other === null && photo === null) return null

  const left = g.rail + g.insets.left
  const width = Math.max(1, g.vw - left - g.insets.right)
  const top = g.insets.top
  const bottom = g.insets.bottom + 8
  const name = compareName(compare, other)
  const thenLabel = photo ? `Aerial photo, ${photo}`
    : other === null ? ""
      : name === "last week" || name === "yesterday" ? `${name.replace(/^./, (c) => c.toUpperCase())}, ${fmtMoment(other)}` : fmtMoment(other)
  const nowLabel = at === null ? `Now, ${fmtMoment(now)}` : fmtMoment(at)
  const pct = Math.round(swipe * 100)

  return (
    <div className="pointer-events-none absolute z-[5]" style={{ left, width, top, bottom }}>
      {/* The line and what each side shows. */}
      <div className="absolute inset-y-0" style={{ left: `${swipe * 100}%` }}>
        <div className="absolute inset-y-0 -left-px w-0.5 bg-foreground/85 shadow-[0_0_0_1px_rgb(14_24_40_/_0.6)]" />
        <Tag side="left">{thenLabel}</Tag>
        <Tag side="right"><span className="text-sodium">{nowLabel}</span></Tag>
      </div>
      <SliderPrimitive.Root
        value={pct}
        min={4}
        max={96}
        step={1}
        largeStep={10}
        thumbAlignment="center"
        onValueChange={(v) => setSwipe((Array.isArray(v) ? v[0] : v) / 100)}
        className="pointer-events-none absolute inset-0"
      >
        <SliderPrimitive.Control className="pointer-events-none relative size-full touch-none select-none">
          <SliderPrimitive.Track className="pointer-events-none relative size-full">
            <SliderPrimitive.Thumb
              getAriaLabel={() => "Compare swipe"}
              getAriaValueText={() => `Left ${pct}% of the map shows ${thenLabel}; the rest shows ${nowLabel}`}
              className="group/swipe pointer-events-auto flex size-10 cursor-ew-resize items-center justify-center rounded-full border-2 border-foreground/85 bg-console text-foreground shadow-lg outline-none transition-[border-color,box-shadow,color] hover:border-sodium hover:text-sodium has-[:focus-visible]:border-sodium has-[:focus-visible]:text-sodium has-[:focus-visible]:shadow-[0_0_0_4px_rgb(255_174_69_/_0.45)] active:border-sodium active:bg-raised data-dragging:border-sodium data-dragging:bg-raised"
              style={{ top: "50%" }}
            >
              <ChevronsLeftRightIcon className="size-5" aria-hidden="true" />
            </SliderPrimitive.Thumb>
          </SliderPrimitive.Track>
        </SliderPrimitive.Control>
      </SliderPrimitive.Root>
    </div>
  )
}
