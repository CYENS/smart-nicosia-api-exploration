/* The city clock: one time control for the whole twin. Folded into the now line by default; shown,
   it runs across the top of the open map. Live by default; drag the track (or use the arrow
   keys) back to any moment the twin's memory holds.
   A replay from the chat is a range on it with play, pause and speed. Compare holds a second
   moment and splits the map with a swipe. */
import {
  ChevronUpIcon, ColumnsIcon, EllipsisIcon, FilmIcon, HistoryIcon, PauseIcon, PlayIcon, RadioIcon,
  XIcon,
} from "lucide-react"

import { ClockTrack } from "@/components/clock/track"
import { CLOCK_ID } from "@/components/shell/now-line"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuGroup, DropdownMenuLabel, DropdownMenuRadioGroup,
  DropdownMenuRadioItem, DropdownMenuSeparator, DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Kbd } from "@/components/ui/kbd"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import {
  SPANS, ago, clearRange, compareAt, compareName, cycleSpeed, fmtMoment, focusClock, goLive, setCompare, setExpanded,
  setSpan, togglePlay, useClock, type Compare, type Span,
} from "@/lib/clock"
import { useGeometry } from "@/lib/geometry"
import { CLOCK_H } from "@/lib/layout"
import { useMapUi } from "@/lib/map/engine"
import { cn } from "@/lib/utils"

const DAY = 24 * 3600e3

function nothingRecorded(strip: ReturnType<typeof useClock.getState>["strip"]) {
  if (!strip || strip.live) return false
  return [strip.lights, strip.parking, strip.bins, strip.air].every((r) => !r || r.unknown)
}

function MomentLabel() {
  const at = useClock((s) => s.at)
  const now = useClock((s) => s.now)
  const loading = useClock((s) => s.loading)
  const strip = useClock((s) => s.strip)
  const empty = at !== null && !loading && nothingRecorded(strip)
  return (
    <div className="flex min-w-0 flex-col justify-center gap-0.5 leading-none">
      {at === null ? (
        <span className="flex items-center gap-1.5 text-[12px] text-sodium">
          <span className="live-dot size-2 rounded-full bg-sodium" aria-hidden="true" />
          Live
        </span>
      ) : (
        <span className={cn("flex items-center gap-1 truncate text-[12px]", empty ? "text-signal" : "text-muted-foreground")}>
          <HistoryIcon className="size-3.5 shrink-0" aria-hidden="true" />
          {empty ? "No record of this moment" : loading ? "Reading the twin's memory" : ago(at, now)}
        </span>
      )}
      <span className={cn("truncate font-readout text-[17px] font-semibold", at !== null && "text-sodium")}>
        {fmtMoment(at ?? now)}
      </span>
    </div>
  )
}

function Chip({ icon: Icon, children, onClose, closeLabel }: {
  icon: React.ComponentType<{ className?: string }>; children: React.ReactNode; onClose: () => void; closeLabel: string
}) {
  return (
    <span className="flex h-7 min-w-0 items-center gap-1.5 rounded-md border border-line bg-night/60 pr-0.5 pl-2 text-[12.5px]">
      <Icon className="size-3.5 shrink-0 text-muted-foreground" />
      <span className="min-w-0 truncate">{children}</span>
      <Button variant="ghost" size="icon-xs" aria-label={closeLabel} onClick={onClose}><XIcon /></Button>
    </span>
  )
}

function SpanToggle({ className }: { className?: string }) {
  const span = useClock((s) => s.span)
  return (
    <ToggleGroup
      aria-label="How much time the track shows"
      size="sm"
      spacing={0}
      variant="outline"
      value={[span]}
      onValueChange={(v) => { if (v[0]) setSpan(v[0] as Span) }}
      className={className}
    >
      {SPANS.map((s) => (
        <ToggleGroupItem
          key={s.id}
          value={s.id}
          className="h-7 px-2 font-condensed text-[12.5px] hover:text-foreground"
        >
          {s.label}
        </ToggleGroupItem>
      ))}
    </ToggleGroup>
  )
}

function compareValue(c: Compare | null) {
  if (!c) return "off"
  if ("offset" in c) return c.offset === -DAY ? "yesterday" : c.offset === -7 * DAY ? "last_week" : "held"
  return "held"
}

function CompareMenu() {
  const compare = useClock((s) => s.compare)
  const at = useClock((s) => s.at)
  const now = useClock((s) => s.now)
  const value = compareValue(compare)
  const on = compare !== null
  const choose = (v: string) => {
    if (v === "off") setCompare(null)
    else if (v === "yesterday") setCompare({ offset: -DAY })
    else if (v === "last_week") setCompare({ offset: -7 * DAY })
    else if (v === "held") setCompare({ at: at ?? Math.floor(now / 900e3) * 900e3 })
  }
  return (
    <DropdownMenu>
      <Tooltip>
        <TooltipTrigger
          render={
            <DropdownMenuTrigger
              render={
                <Button
                  data-clock-part="compare"
                  variant={on ? "secondary" : "ghost"}
                  size="sm"
                  aria-label={on ? "Compare two moments (on)" : "Compare two moments"}
                  className={cn("h-7 gap-1.5 px-2 data-popup-open:bg-accent", on && "bg-sodium/[0.14] inset-ring-1 inset-ring-sodium/55 hover:bg-sodium/20 *:[svg]:text-sodium")}
                />
              }
            />
          }
        >
          <ColumnsIcon data-icon="inline-start" />
          <span className="hidden @[520px]:inline">Compare</span>
        </TooltipTrigger>
        <TooltipContent>Compare two moments with a swipe across the map</TooltipContent>
      </Tooltip>
      <DropdownMenuContent align="end" className="w-60">
        <DropdownMenuGroup>
          <DropdownMenuLabel>Compare the clock's moment with</DropdownMenuLabel>
          <DropdownMenuRadioGroup value={value} onValueChange={choose}>
            <DropdownMenuRadioItem closeOnClick value="last_week">The same time last week</DropdownMenuRadioItem>
            <DropdownMenuRadioItem closeOnClick value="yesterday">The same time yesterday</DropdownMenuRadioItem>
            <DropdownMenuRadioItem closeOnClick value="held">{value === "held" ? "The moment held" : "This moment, held while you move the clock"}</DropdownMenuRadioItem>
            <DropdownMenuSeparator />
            <DropdownMenuRadioItem closeOnClick value="off">Nothing (no comparison)</DropdownMenuRadioItem>
          </DropdownMenuRadioGroup>
        </DropdownMenuGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

// On a narrow clock: the span and the speed, in a menu.
function MoreMenu() {
  const span = useClock((s) => s.span)
  const speed = useClock((s) => s.speed)
  return (
    <DropdownMenu>
      <DropdownMenuTrigger render={<Button variant="ghost" size="icon-sm" aria-label="More clock settings" className="@[640px]:hidden" />}>
        <EllipsisIcon />
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-48">
        <DropdownMenuGroup>
          <DropdownMenuLabel>The track shows</DropdownMenuLabel>
          <DropdownMenuRadioGroup value={span} onValueChange={(v) => setSpan(v as Span)}>
            {SPANS.map((s) => <DropdownMenuRadioItem closeOnClick key={s.id} value={s.id}>{s.label === "6 h" ? "6 hours" : `A ${s.label.toLowerCase()}`}</DropdownMenuRadioItem>)}
          </DropdownMenuRadioGroup>
        </DropdownMenuGroup>
        <DropdownMenuSeparator />
        <DropdownMenuGroup>
          <DropdownMenuLabel>Playback speed</DropdownMenuLabel>
          <DropdownMenuRadioGroup value={String(speed)} onValueChange={(v) => useClock.setState({ speed: Number(v) })}>
            {[1, 2, 4, 8].map((x) => <DropdownMenuRadioItem closeOnClick key={x} value={String(x)}>{x}×</DropdownMenuRadioItem>)}
          </DropdownMenuRadioGroup>
        </DropdownMenuGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

function LiveButton() {
  const live = useClock((s) => s.at === null)
  if (live) return null
  return (
    <Tooltip>
      <TooltipTrigger render={<Button size="sm" className="h-7 gap-1.5 px-2.5" onClick={() => { goLive(); focusClock(useClock.getState().expanded ? "track" : "toggle") }} />}>
        <RadioIcon data-icon="inline-start" />
        Back to live
      </TooltipTrigger>
      <TooltipContent>Back to now <Kbd>L</Kbd></TooltipContent>
    </Tooltip>
  )
}

function PlayButton() {
  const playing = useClock((s) => s.playing)
  const hasRange = useClock((s) => s.range !== null)
  return (
    <Tooltip>
      <TooltipTrigger
        render={
          <Button
            data-clock-part="play"
            size="icon-sm"
            variant="secondary"
            className={cn("shrink-0 rounded-full", playing && "bg-sodium text-night hover:bg-sodium/85")}
            aria-label={playing ? "Pause" : hasRange ? "Play the replay" : "Play from here"}
            onClick={togglePlay}
          />
        }
      >
        {playing ? <PauseIcon /> : <PlayIcon />}
      </TooltipTrigger>
      <TooltipContent>{playing ? "Pause" : hasRange ? "Play the replay" : "Play the city forward from this moment"} <Kbd>Space</Kbd></TooltipContent>
    </Tooltip>
  )
}

// The city clock's console: across the top of the open map, under the top band, while shown. The
// now line (components/shell/now-line.tsx) is the clock folded up, and holds its keys.
export function CityClock() {
  const g = useGeometry()
  const expanded = useClock((s) => s.expanded)
  const range = useClock((s) => s.range)
  const speed = useClock((s) => s.speed)
  const playing = useClock((s) => s.playing)
  const compare = useClock((s) => s.compare)
  const other = useClock((s) => compareAt(s))
  const replay = useMapUi((s) => (range?.ref ? s.replays[range.ref] : null))

  if (!expanded) return null
  const place = { top: g.clockTop, left: g.clockLeft, right: g.clockRight }
  const vs = compareName(compare, other)

  return (
    <div className="pointer-events-none absolute z-20 flex justify-center" style={place}>
      <section
        id={CLOCK_ID}
        aria-label="City clock"
        className="@container panel-float pointer-events-auto flex w-full max-w-[860px] flex-col justify-between rounded-xl px-2.5 pt-2 pb-1.5 motion-safe:animate-in motion-safe:fade-in-0 motion-safe:slide-in-from-top-1"
        style={{ height: CLOCK_H }}
        onKeyDown={(e) => {
          if (e.key === "Escape" && !e.defaultPrevented && !(e.target as HTMLElement).closest("[role=menu]")) {
            e.preventDefault()
            setExpanded(false)
            focusClock("toggle")
          }
        }}
      >
        <div className="flex h-[34px] min-w-0 items-center gap-1.5">
          <PlayButton />
          <div className="min-w-0 shrink pl-1"><MomentLabel /></div>
          <div className="flex min-w-0 flex-1 items-center justify-end gap-1.5">
            {range && (
              <span className="hidden min-w-0 @[560px]:flex">
                <Chip icon={FilmIcon} onClose={() => { clearRange(); goLive(); focusClock("play") }} closeLabel="Close the replay">
                  {range.title}
                  {replay?.reading && <span className="ml-1.5 font-readout font-semibold">{replay.reading}</span>}
                </Chip>
              </span>
            )}
            {vs && (
              <span className="hidden min-w-0 @[460px]:flex">
                <Chip icon={ColumnsIcon} onClose={() => { setCompare(null); focusClock("compare") }} closeLabel="Stop comparing">vs {vs}</Chip>
              </span>
            )}
            {(range || playing) && (
              <Tooltip>
                <TooltipTrigger render={<Button variant="ghost" size="sm" aria-label={`Playback speed ${speed}×, change it`} className={cn("hidden h-7 px-2 font-readout @[640px]:inline-flex", speed > 1 && "text-sodium")} onClick={cycleSpeed} />}>
                  {speed}×
                </TooltipTrigger>
                <TooltipContent>Playback speed</TooltipContent>
              </Tooltip>
            )}
            <SpanToggle className="hidden @[640px]:flex" />
            <CompareMenu />
            {/* On a desktop the now line keeps its Live button beside it; a phone's console stands in for the now line. */}
            {!g.desktop && <LiveButton />}
            <MoreMenu />
            <Tooltip>
              <TooltipTrigger render={<Button data-clock-part="toggle" variant="ghost" size="icon-sm" aria-label="Hide the city clock" aria-expanded onClick={() => { setExpanded(false); focusClock("toggle") }} />}>
                <ChevronUpIcon />
              </TooltipTrigger>
              <TooltipContent>Hide the city clock <Kbd>Esc</Kbd></TooltipContent>
            </Tooltip>
          </div>
        </div>
        <ClockTrack />
      </section>
    </div>
  )
}
