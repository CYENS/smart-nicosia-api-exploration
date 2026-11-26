/* The Environment workspace: the physical city at the clock's moment. Where the sun is and what
   it is like at the map's centre, the environment's layers (the same rows as the Layers sheet's
   Environment group), the sun hours of a square or street on any date with a chart of its day,
   and questions to ask. It takes the right column (a sheet on a phone); the inspector still
   opens over it. Esc or the close button goes back to Live and gives the keyboard back to
   whatever opened the workspace. */
import { MapPinnedIcon, MessageSquareTextIcon, SunIcon, SunMediumIcon, XIcon } from "lucide-react"
import { useEffect, useMemo, useRef } from "react"

import { ENV_GROUP, compass } from "@/components/environment/rows"
import { DayChart, SunHoursLegend } from "@/components/environment/parts"
import { SystemRow } from "@/components/shell/layers-sheet"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Field, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Kbd } from "@/components/ui/kbd"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Separator } from "@/components/ui/separator"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { send, useChat } from "@/lib/chat"
import { fmtMoment, goLive, useClock } from "@/lib/clock"
import { clearSunHours, mapSunHours, openSpotAtCentre } from "@/lib/environment/map"
import { PLACES, fmtValue, patch, useEnvironment, type Place } from "@/lib/environment/store"
import { useGeometry } from "@/lib/geometry"
import { typing } from "@/lib/hooks"
import { GAP, STRIP_H, setChatOpen } from "@/lib/layout"
import { getMap, useMapUi } from "@/lib/map/engine"
import { skyAt } from "@/lib/map/sky"
import { useLayerRegistry } from "@/lib/registries/layers"
import { openWorkspace } from "@/lib/registries/workspaces"
import { cn } from "@/lib/utils"

const QUESTIONS = [
  "Where is the shade around Eleftheria Square at 15:00 in July?",
  "Plan a shaded walk from Ledras Street to the Cyprus Museum",
  "Where is the air worst right now?",
]
const CENTRE = "__centre"
const CENTRE_NAME = "The map's centre"

// What had the keyboard when the workspace opened (a topic card's link, the Layers sheet's row,
// Ctrl K): leaving gives it back there, or to the map when it has gone.
let opener: HTMLElement | null = null
export function rememberEnvironmentOpener() {
  const a = document.activeElement
  opener = a instanceof HTMLElement && a !== document.body ? a : null
}

// The keyboard goes back here when the inspector opened from this panel closes.
let returnTo: string | null = null

function nicosiaDate(ms: number) {
  return new Date(ms).toLocaleDateString("en-CA", { timeZone: "Asia/Nicosia" })
}

function longDate(iso: string) {
  return new Date(`${iso}T12:00`).toLocaleDateString("en-GB", { day: "numeric", month: "long", year: "numeric" })
}

function SunNow() {
  const at = useClock((s) => s.at)
  const skyNow = useMapUi((s) => s.skyNow)
  const sky = useMemo(() => (at === null ? skyNow : skyAt(new Date(at))), [at, skyNow])
  const up = sky.sun.altitude > 0
  const next = sky.next.at.toLocaleTimeString("en-GB", { timeZone: "Asia/Nicosia", hour: "2-digit", minute: "2-digit" })
  return (
    <div className="flex items-start gap-3" aria-live="polite">
      <span className={cn("mt-0.5 flex size-9 shrink-0 items-center justify-center rounded-full", up ? "bg-sodium/15 text-sodium" : "bg-muted text-muted-foreground")} aria-hidden="true">
        {up ? <SunIcon className="size-5" /> : <SunMediumIcon className="size-5" />}
      </span>
      <p className="min-w-0 text-[13.5px] leading-snug">
        {up ? (
          <>
            <span className="font-readout text-[17px] font-semibold">{Math.round(sky.sun.altitude)}°</span>
            <span className="text-muted-foreground"> up, from the {compass(sky.sun.bearing)}</span>
          </>
        ) : <span className="font-medium">The sun is down</span>}
        <span className="block text-[12.5px] text-muted-foreground">
          {sky.dark ? `Dark until ${next}` : `Light until ${next}`}{at === null ? "" : `, at ${fmtMoment(at)}`}
        </span>
      </p>
    </div>
  )
}

function SunHoursTool() {
  const sh = useEnvironment((s) => s.sunHours)
  const at = useClock((s) => s.at)
  const clockNow = useClock((s) => s.now)
  const today = nicosiaDate(at ?? clockNow)
  const year = today.slice(0, 4)
  const presets = [
    { value: today, label: "Today" },
    { value: `${year}-06-21`, label: "21 June" },
    { value: `${year}-12-21`, label: "21 December" },
  ]
  const placeValue = PLACES.some((p) => p.name === sh.place.name) ? sh.place.name : CENTRE

  const run = () => {
    let place: Place = sh.place
    if (placeValue === CENTRE) {
      const c = getMap().getCenter()
      place = { name: CENTRE_NAME, lat: c.lat, lon: c.lng }
    }
    void mapSunHours({ place, date: sh.date })
  }
  const d = sh.data
  return (
    <div className="flex flex-col gap-3">
      <Field>
        <FieldLabel htmlFor="ews-place" className="text-[12.5px] font-normal text-muted-foreground">Around</FieldLabel>
        <Select
          value={placeValue}
          onValueChange={(v) => patch("sunHours", { place: PLACES.find((p) => p.name === v) ?? { name: CENTRE_NAME, lat: 0, lon: 0 } })}
        >
          <SelectTrigger id="ews-place" size="sm" className="w-full"><SelectValue>{placeValue === CENTRE ? CENTRE_NAME : placeValue}</SelectValue></SelectTrigger>
          <SelectContent>
            {PLACES.map((p) => <SelectItem key={p.name} value={p.name}>{p.name}</SelectItem>)}
            <SelectItem value={CENTRE}>{CENTRE_NAME}</SelectItem>
          </SelectContent>
        </Select>
      </Field>
      <Field>
        <FieldLabel id="ews-date-label" className="text-[12.5px] font-normal text-muted-foreground">On</FieldLabel>
        <ToggleGroup aria-labelledby="ews-date-label" variant="outline" size="sm" spacing={0} className="w-full"
          value={presets.some((p) => p.value === sh.date) ? [sh.date] : []}
          onValueChange={(v) => { if (v[0]) patch("sunHours", { date: v[0] }) }}>
          {presets.map((p) => <ToggleGroupItem key={p.label} value={p.value} className="flex-1 px-1.5">{p.label}</ToggleGroupItem>)}
        </ToggleGroup>
        <Input type="date" aria-label="Or any date" value={sh.date} className="h-8"
          onChange={(e) => { if (e.target.value) patch("sunHours", { date: e.target.value }) }} />
      </Field>
      <div className="flex gap-2">
        <Button size="sm" onClick={run} disabled={sh.status === "loading"} className="flex-1">
          {sh.status === "loading" ? <Spinner data-icon="inline-start" /> : <SunIcon data-icon="inline-start" />}
          {sh.status === "loading" ? "Reading the buildings" : "Map the sun hours"}
        </Button>
        {d && <Button size="sm" variant="outline" onClick={clearSunHours}>Clear</Button>}
      </div>
      {sh.status === "loading" && !d && <Skeleton className="h-24 w-full" />}
      {sh.status === "error" && (
        <Alert>
          <AlertTitle>Couldn't read the sun hours</AlertTitle>
          <AlertDescription>{sh.error}. The 3D buildings come from the DLS and may be slow; try again in a moment.</AlertDescription>
        </Alert>
      )}
      {d && (
        <div className="flex flex-col gap-3" aria-live="polite">
          <p className="text-[13px] leading-snug">
            <span className="font-readout text-[24px] font-semibold text-sodium">{d.hours.mean ?? "?"}</span>
            <span className="text-muted-foreground"> hours of direct sun on average, {fmtValue(d.hours.min)} to {fmtValue(d.hours.max)} across the open ground, of {d.sun.daylight_h} hours of daylight on {longDate(d.date)}</span>
          </p>
          <SunHoursLegend />
          <DayChart points={d.profile} title={`${sh.place.name}, open ground in sun`} />
          <p className="text-[12px] leading-snug text-muted-foreground">
            Sunrise {d.sun.sunrise}, sunset {d.sun.sunset}. Only buildings cast shade here, not trees or awnings{d.tiles_missing ? "; some buildings around couldn't be read" : ""}.
          </p>
        </div>
      )}
    </div>
  )
}

function leave() {
  const inside = document.activeElement?.closest("[data-workspace]")
  openWorkspace("live")
  if (!inside) return
  const back = opener?.isConnected ? opener : document.querySelector<HTMLElement>(".maplibregl-canvas")
  opener = null
  back?.focus({ preventScroll: true })
}

export function EnvironmentPanel() {
  const g = useGeometry()
  const busy = useChat((s) => s.busy)
  const clockAt = useClock((s) => s.at)
  const layers = useLayerRegistry((s) => s.layers)
  const rows = useMemo(() => layers.filter((l) => l.group === ENV_GROUP), [layers])
  const title = useRef<HTMLHeadingElement>(null)
  useEffect(() => { title.current?.focus({ preventScroll: true }) }, [])
  useEffect(() => {
    if (g.inspecting || !returnTo) return
    const key = returnTo
    returnTo = null
    document.querySelector<HTMLElement>(`[data-workspace] [data-return="${CSS.escape(key)}"]`)?.focus({ preventScroll: true })
  }, [g.inspecting])
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !typing(e) && !g.inspecting && document.activeElement?.closest("[data-workspace]")
        && !document.activeElement?.closest("[role=listbox],[role=group][tabindex]")) leave()
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [g.inspecting])

  if (g.inspecting) return null
  const style = g.desktop
    ? { top: STRIP_H + GAP, right: GAP, width: g.workspaceW, maxHeight: `calc(100% - ${STRIP_H + 2 * GAP + g.layersBottom}px)` }
    : { left: 8, right: 8, bottom: 8, height: g.sheetH }

  return (
    <aside data-workspace aria-labelledby="environment-title" className={cn("panel-float absolute flex flex-col overflow-hidden rounded-xl", g.desktop ? "z-10" : "z-20")} style={style}>
      <div className="flex h-14 shrink-0 items-center gap-3 border-b border-line pr-2 pl-4">
        <SunIcon className="size-[18px] text-sodium" aria-hidden="true" />
        <div className="min-w-0 flex-1 leading-tight">
          <h2 ref={title} tabIndex={-1} id="environment-title" className="text-[14.5px] font-semibold outline-none">Environment</h2>
          <p className="truncate text-[12.5px] text-muted-foreground">Sun, shade, terrain, air and weather</p>
        </div>
        <Tooltip>
          <TooltipTrigger render={<Button variant="ghost" size="icon-sm" aria-label="Close Environment" onClick={leave} />}>
            <XIcon />
          </TooltipTrigger>
          <TooltipContent>Back to Live <Kbd>Esc</Kbd></TooltipContent>
        </Tooltip>
      </div>
      <div className="flex min-h-0 flex-col gap-4 overflow-y-auto p-4">
        {clockAt !== null && (
          <p className="rounded-lg border border-line bg-night/50 px-3 py-2 text-[12.5px] leading-snug text-muted-foreground" aria-live="polite">
            The clock shows {fmtMoment(clockAt, { weekday: true })}: the sun, the shadows, the surfaces and the wind are from then.{" "}
            <button type="button" className="rounded-sm text-foreground underline underline-offset-3 outline-none hover:text-sodium focus-visible:ring-2 focus-visible:ring-ring/60" onClick={goLive}>Back to now</button>
          </p>
        )}
        <section aria-label="The sun now" className="flex flex-col gap-3">
          <SunNow />
          <Button variant="outline" size="sm" className="self-start" data-return="centre"
            onClick={(e) => { returnTo = "centre"; openSpotAtCentre(e.currentTarget) }}>
            <MapPinnedIcon data-icon="inline-start" />
            What's it like at the map's centre?
          </Button>
        </section>
        <Separator />
        <section aria-labelledby="ews-layers" className="flex flex-col gap-4">
          <h3 id="ews-layers" className="text-[13px] font-semibold">On the map</h3>
          {rows.map((l) => <SystemRow key={l.id} layer={l} idPrefix="ews" />)}
        </section>
        <Separator />
        <section aria-labelledby="ews-sunhours" className="flex flex-col gap-2.5">
          <h3 id="ews-sunhours" className="text-[13px] font-semibold">Sun hours of a square</h3>
          <SunHoursTool />
        </section>
        <Separator />
        <section aria-label="Ask" className="flex flex-col gap-1.5">
          <h3 className="text-[12.5px] text-muted-foreground">Ask about it</h3>
          {QUESTIONS.map((q) => (
            <Button key={q} variant="ghost" size="sm" disabled={busy} className="h-auto justify-start py-1.5 text-left whitespace-normal"
              onClick={() => {
                setChatOpen(true)
                if (!g.desktop) openWorkspace("live")
                void send(q)
              }}>
              <MessageSquareTextIcon data-icon="inline-start" />{q}
            </Button>
          ))}
        </section>
      </div>
    </aside>
  )
}
