/* The card of a view from a place (lib/view.ts): where you stand in words, when it is, and the
   controls to look about. In the right column on a desktop, a short bottom sheet on a phone. Every
   control is a button (the arrow keys look too, and dragging the map); Esc or "Back to the map"
   puts the map back as it was. */
import {
  ArrowRightIcon, ChevronDownIcon, ChevronLeftIcon, ChevronRightIcon, ChevronUpIcon, EyeIcon,
  MapIcon, RotateCwIcon, XIcon,
} from "lucide-react"
import { useEffect, useRef } from "react"

import { MoonGlyph } from "@/components/moon-glyph"
import { FromSources } from "@/components/sources/from-sources"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { ButtonGroup } from "@/components/ui/button-group"
import { Kbd } from "@/components/ui/kbd"
import { Spinner } from "@/components/ui/spinner"
import { Toggle } from "@/components/ui/toggle"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { fmtMoment, useClock } from "@/lib/clock"
import { useGeometry } from "@/lib/geometry"
import { GAP, PHONE_GAP, STRIP_H, useLayout } from "@/lib/layout"
import { moonName, skyAt } from "@/lib/map/sky"
import { bearingTo, compass, leaveView, look, nextView, toggleLookAround, useView } from "@/lib/view"

const CENTRE: [number, number] = [33.3615, 35.1725] // Eleftheria Square, as the server has it

function metres(lon1: number, lat1: number, lon2: number, lat2: number) {
  const k = Math.cos((((lat1 + lat2) / 2) * Math.PI) / 180)
  return Math.hypot((lon2 - lon1) * k, lat2 - lat1) * 111320
}

function km(m: number) {
  return m >= 1000 ? `${(m / 1000).toFixed(1)} km` : `${Math.round(m / 10) * 10} m`
}

// "4.7 km north-east of the centre, 182 m up"
function whereWords(lon: number, lat: number, eyeM: number) {
  const d = metres(CENTRE[0], CENTRE[1], lon, lat)
  const where = d < 450 ? "In the centre" : `${km(d)} ${compass(bearingTo(CENTRE[0], CENTRE[1], lon, lat))} of the centre`
  return `${where}, ${Math.round(eyeM)} m up`
}

function LookButton({ label, keys, onClick, children }: { label: string; keys: string; onClick: () => void; children: React.ReactNode }) {
  return (
    <Tooltip>
      <TooltipTrigger render={<Button variant="outline" size="icon" aria-label={label} onClick={onClick} />}>
        {children}
      </TooltipTrigger>
      <TooltipContent>{label} <Kbd>{keys}</Kbd></TooltipContent>
    </Tooltip>
  )
}

export function ViewCard() {
  const open = useView((s) => s.open)
  const spec = useView((s) => s.spec)
  const yaw = useView((s) => s.yaw)
  const arriving = useView((s) => s.arriving)
  const turning = useView((s) => s.turning)
  const tour = useView((s) => s.tour)
  const index = useView((s) => s.index)
  const loading = useView((s) => s.loading)
  const error = useView((s) => s.error)
  const clockAt = useClock((s) => s.at)
  const g = useGeometry()
  const box = useRef<HTMLElement>(null)
  const title = useRef<HTMLHeadingElement>(null)
  const reduced = typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches

  // A phone's sheet: its height keeps the camera and the credits clear of it.
  const shown = open || loading || !!error
  useEffect(() => {
    const el = box.current
    if (!shown || !el || g.desktop) { useLayout.setState({ cardH: 0 }); return }
    const ro = new ResizeObserver(() => useLayout.setState({ cardH: Math.round(el.offsetHeight) }))
    ro.observe(el)
    return () => { ro.disconnect(); useLayout.setState({ cardH: 0 }) }
  }, [shown, g.desktop])
  // The keyboard comes to the card when a view opens from a click (not while typing a question).
  const specId = spec?.id
  useEffect(() => {
    if (!open || !specId) return
    const a = document.activeElement
    if (!a || a === document.body || box.current?.contains(a)) title.current?.focus({ preventScroll: true })
  }, [open, specId])

  if (!shown) return null
  const style = g.desktop
    ? { top: STRIP_H + GAP, right: GAP, width: g.sideW }
    : { left: PHONE_GAP, right: PHONE_GAP, bottom: PHONE_GAP }
  const close = () => {
    if (open) leaveView()
    else useView.setState({ error: null, loading: false })
  }

  // The moment the view shows: the clock's (a night held from last night), or now.
  const at = clockAt
  const sky = skyAt(at === null ? new Date() : new Date(at))
  const when = at === null
    ? (sky.dark ? "Now, live" : null)
    : `${fmtMoment(at)}, as the twin's memory has it`
  const held = spec?.at && spec.when && !spec.when.startsWith("now") ? spec.when : null
  const marks = spec?.landmarks ?? []
  const target = spec?.toward
  const inSight = marks.filter((m) => m.visible).map((m, i) => (i ? m.title.replace(/^The /, "the ") : m.title))

  return (
    <section
      ref={box}
      data-view-card
      aria-labelledby="view-card-title"
      className="panel-float absolute z-20 flex max-h-[calc(100%-24px)] flex-col overflow-hidden rounded-xl motion-safe:animate-in motion-safe:fade-in-0 motion-safe:slide-in-from-bottom-2"
      style={style}
    >
      <header className="flex h-12 shrink-0 items-center gap-2.5 border-b border-line pr-2 pl-4">
        <EyeIcon className="size-4 shrink-0 text-sodium" aria-hidden="true" />
        <h2 ref={title} tabIndex={-1} id="view-card-title" className="min-w-0 flex-1 truncate text-[14px] font-semibold outline-none">
          {spec ? `View from ${spec.name}` : "View from here"}
        </h2>
        <Tooltip>
          <TooltipTrigger render={<Button variant="ghost" size="icon-sm" aria-label="Back to the map" onClick={close} />}>
            <XIcon />
          </TooltipTrigger>
          <TooltipContent>Back to the map <Kbd>Esc</Kbd></TooltipContent>
        </Tooltip>
      </header>

      <div className="flex min-h-0 flex-col gap-3 overflow-y-auto px-4 pt-3 pb-3">
        {error && (
          <Alert variant="destructive">
            <AlertTitle>No view</AlertTitle>
            <AlertDescription>{error} Try another place, or ask in the chat.</AlertDescription>
          </Alert>
        )}
        {loading && !open && (
          <p className="flex items-center gap-2 text-[13px] text-muted-foreground"><Spinner /> Working out the view</p>
        )}
        {spec && (
          <>
            <div className="flex flex-col gap-1">
              <p className="font-condensed text-[15px] leading-snug tabular-nums" aria-live="polite">
                {whereWords(spec.lon, spec.lat, spec.eye_m)}, looking {compass(yaw)}
              </p>
              <p className="text-[13px] leading-snug text-muted-foreground">
                {spec.height_m > 2 ? `${Math.round(spec.height_m)} m above the ground, ` : "At eye level, "}
                {Math.abs(Math.round(spec.above_centre_m)) < 1
                  ? "level with the centre's streets."
                  : `${Math.abs(Math.round(spec.above_centre_m))} m ${spec.above_centre_m > 0 ? "above" : "below"} the centre's streets.`}
              </p>
              {target && (
                <p className="text-[13px] leading-snug text-muted-foreground">
                  {target.in_sight ? `${cap(target.name)} is in sight.` : `${cap(target.name)} is hidden from here.`}
                </p>
              )}
              {!target && inSight.length > 0 && (
                <p className="text-[13px] leading-snug text-muted-foreground">In sight: {inSight.join(", ")}.</p>
              )}
            </div>

            {(when || held) && (
              <div className="flex items-start gap-2.5 rounded-lg border border-line bg-raised/60 px-3 py-2 text-[13px]">
                {sky.dark && <MoonGlyph phase={sky.moon.phase} className="mt-0.5 size-4 shrink-0" />}
                <div className="flex flex-col gap-0.5">
                  <span>{held ? cap(held) : when}</span>
                  {sky.dark && (
                    <span className="text-muted-foreground">
                      {moonName(sky.moon.phase)}, {Math.round(sky.moon.lit * 100)}% lit
                      {sky.moon.altitude > 0 ? `, ${Math.round(sky.moon.altitude)}° up in the ${compass(sky.moon.bearing)}` : ", below the horizon"}.
                    </span>
                  )}
                </div>
              </div>
            )}

            {arriving ? (
              <p className="flex items-center gap-2 text-[13px] text-muted-foreground" aria-live="polite"><Spinner /> Flying there</p>
            ) : (
              <div className="flex flex-col gap-2">
                <div className="flex flex-wrap items-center gap-2">
                  <ButtonGroup aria-label="Look">
                    <LookButton label="Look left" keys="←" onClick={() => look(-15)}><ChevronLeftIcon /></LookButton>
                    <LookButton label="Look right" keys="→" onClick={() => look(15)}><ChevronRightIcon /></LookButton>
                  </ButtonGroup>
                  <ButtonGroup aria-label="Look up or down">
                    <LookButton label="Look up" keys="↑" onClick={() => look(0, 4)}><ChevronUpIcon /></LookButton>
                    <LookButton label="Look down" keys="↓" onClick={() => look(0, -4)}><ChevronDownIcon /></LookButton>
                  </ButtonGroup>
                  {!reduced && (
                    <Toggle variant="outline" pressed={turning} onPressedChange={() => toggleLookAround()} aria-label="Look around">
                      <RotateCwIcon data-icon="inline-start" />
                      Look around
                    </Toggle>
                  )}
                </div>
                <p className="text-[12px] text-muted-foreground">Drag the map or use the arrow keys to look about.</p>
              </div>
            )}
          </>
        )}
        {spec && <FromSources ids={["terrain", "dls", "openfreemap", ...(sky.dark ? ["smart_nicosia"] : [])]} />}
      </div>

      {spec && (
        <footer className="flex shrink-0 flex-wrap items-center gap-2 border-t border-line px-3 py-2.5">
          {tour.length > 1 && (
            <Button variant="secondary" size="sm" disabled={arriving} onClick={nextView}>
              Next view
              <span className="text-muted-foreground tabular-nums">{index + 1} of {tour.length}</span>
              <ArrowRightIcon data-icon="inline-end" />
            </Button>
          )}
          <Button variant="ghost" size="sm" className="ml-auto" onClick={() => leaveView()}>
            <MapIcon data-icon="inline-start" />
            Back to the map
          </Button>
        </footer>
      )}
    </section>
  )
}

function cap(s: string) {
  return s ? s[0].toUpperCase() + s.slice(1) : s
}
