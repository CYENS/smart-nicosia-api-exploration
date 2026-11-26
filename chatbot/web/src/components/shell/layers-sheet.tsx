/* The Layers sheet: everything the map can show, one click away for those who want it. The base
   map and the 3D view; the city's systems in three groups (Sensors, Buildings, Alerts), each layer
   with its switch and its live summary, one group open at a time (remembered on this device);
   the workspaces; what the answers drew; and your position. A sheet that leaves the map usable
   beside it: the right column on a desktop, a bottom sheet on a phone. Esc or the close button
   gives the keyboard back to whatever opened it. */
import { ImageIcon, LocateFixedIcon, MapIcon, MoonIcon, NavigationIcon, SquareIcon, SunIcon, SunMoonIcon, XIcon } from "lucide-react"
import { useMemo, type KeyboardEvent, type ReactNode } from "react"

import { useCameraAngles, set3dView } from "@/components/shell/map-controls"
import { FromSources } from "@/components/sources/from-sources"
import { Accordion, AccordionContent, AccordionItem, AccordionTrigger } from "@/components/ui/accordion"
import { Button } from "@/components/ui/button"
import { Field, FieldContent, FieldDescription, FieldGroup, FieldLabel } from "@/components/ui/field"
import { Item, ItemContent, ItemDescription, ItemGroup, ItemMedia, ItemTitle } from "@/components/ui/item"
import { Kbd } from "@/components/ui/kbd"
import { Separator } from "@/components/ui/separator"
import { Sheet, SheetClose, SheetContent, SheetDescription, SheetTitle } from "@/components/ui/sheet"
import { Switch } from "@/components/ui/switch"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { useClock } from "@/lib/clock"
import { useGeometry } from "@/lib/geometry"
import { closeLayers, layersHandedOff, layersOpener, setLayersGroup, useLayersSheet } from "@/lib/layers-sheet"
import { GAP, PHONE_GAP, STRIP_H, useLayout } from "@/lib/layout"
import type { Basemap } from "@/lib/map/basemap"
import { setGround, useGround, type Ground } from "@/lib/map/ground"
import { skyAt } from "@/lib/map/sky"
import {
  RAMP, REGISTERS_REF, flyRoute, fmt, forgetMyLocation, locateMe, nicosiaTime, removeLayer, setBasemapMode,
  stopFlight, useMapUi, type LayerSummary,
} from "@/lib/map/engine"
import { cap } from "@/lib/readouts"
import { layerKeyFor } from "@/lib/registries/layer-keys"
import { useLayerRegistry, type SystemLayer } from "@/lib/registries/layers"
import { openWorkspace, useWorkspace } from "@/lib/registries/workspaces"
import { useRovingList } from "@/lib/roving"
import { isSystemRef } from "@/lib/system-layers"
import { cn } from "@/lib/utils"

const HEAT = ["#3b82f6", "#22c55e", "#eab308", "#f97316", "#dc2626"]

function Section({ title, children, className }: { title: string; children: ReactNode; className?: string }) {
  return (
    <section aria-label={title} className={cn("flex flex-col gap-3", className)}>
      <h3 className="text-[13px] font-semibold text-muted-foreground">{title}</h3>
      {children}
    </section>
  )
}

// -- Map ----------------------------------------------------------------------------------------

function MapSection() {
  const mode = useMapUi((s) => s.basemapMode)
  const clockAt = useClock((s) => s.at)
  const skyNow = useMapUi((s) => s.skyNow)
  const sky = useMemo(() => (clockAt === null ? skyNow : skyAt(new Date(clockAt))), [clockAt, skyNow])
  const { pitch } = useCameraAngles()
  const ground = useGround((s) => s.ground)
  const groundLoading = useGround((s) => s.loading)
  return (
    <Section title="Map">
      <FieldGroup className="gap-4">
        <Field>
          <FieldLabel id="basemap-label">Base map</FieldLabel>
          <ToggleGroup
            aria-labelledby="basemap-label"
            variant="outline"
            size="sm"
            spacing={0}
            className="w-full"
            value={[mode]}
            onValueChange={(v) => { if (v[0] && v[0] !== mode) setBasemapMode(v[0] as "auto" | Basemap) }}
          >
            <ToggleGroupItem value="auto" className="flex-1"><SunMoonIcon />Auto</ToggleGroupItem>
            <ToggleGroupItem value="night" className="flex-1"><MoonIcon />Night</ToggleGroupItem>
            <ToggleGroupItem value="day" className="flex-1"><SunIcon />Day</ToggleGroupItem>
          </ToggleGroup>
          <FieldDescription>
            {mode === "auto"
              ? `Follows the sun${clockAt === null ? "" : " at the clock's moment"}: ${sky.dark ? "night" : "day"} until ${nicosiaTime(sky.next.at)}`
              : mode === "night" ? "Night, whatever the time" : "Day, whatever the time"}
          </FieldDescription>
        </Field>
        <Field>
          <FieldLabel id="ground-label">Ground</FieldLabel>
          <ToggleGroup
            aria-labelledby="ground-label"
            variant="outline"
            size="sm"
            spacing={0}
            className="w-full"
            value={[ground]}
            onValueChange={(v) => { if (v[0] && v[0] !== ground) setGround(v[0] as Ground) }}
          >
            <ToggleGroupItem value="map" className="flex-1"><MapIcon />Map</ToggleGroupItem>
            <ToggleGroupItem value="photo" className="flex-1"><ImageIcon />Aerial photo</ToggleGroupItem>
          </ToggleGroup>
          <FieldDescription aria-live="polite">
            {ground === "map"
              ? "The base map's streets and fields"
              : groundLoading ? "Loading the photo for this area" : "The DLS aerial photo of 2019, dimmed after dark"}
          </FieldDescription>
        </Field>
        <Field orientation="horizontal">
          <FieldContent>
            <FieldLabel htmlFor="ls-3d">3D buildings</FieldLabel>
            <FieldDescription>Tilt the map so the buildings stand up</FieldDescription>
          </FieldContent>
          <Switch id="ls-3d" checked={pitch > 10} onCheckedChange={(on) => set3dView(on)} />
        </Field>
      </FieldGroup>
    </Section>
  )
}

// -- City systems -------------------------------------------------------------------------------

// One row: its switch, its live summary and, while it is on, its legend. The open workspace's
// panel lists its own group's rows too (the environment's), with its own id prefix.
export function SystemRow({ layer, idPrefix = "ls" }: { layer: SystemLayer; idPrefix?: string }) {
  const on = layer.useOn()
  const id = `${idPrefix}-${layer.id}`
  return (
    <div className="flex flex-col gap-2">
      <Field orientation="horizontal">
        <FieldContent>
          <FieldLabel htmlFor={id}>{layer.label}</FieldLabel>
          <FieldDescription aria-live="polite"><layer.Summary /></FieldDescription>
        </FieldContent>
        <Switch id={id} checked={on} onCheckedChange={(v) => layer.setOn(v)} />
      </Field>
      {on && layer.Legend && <layer.Legend />}
    </div>
  )
}

// How many of a group's layers are on (said on its header while it is closed).
function OnCount({ layers }: { layers: SystemLayer[] }) {
  const n = layers.map((l) => l.useOn()).filter(Boolean).length
  if (!n) return null
  return <span className="ml-auto pr-2 font-condensed text-[13px] font-normal text-sodium">{n} on</span>
}

// WAI-ARIA's optional accordion keys: Up and Down move between the group headers, Home and End
// to the first and last (Tab still goes through each open group's switches).
function headerKeys(e: KeyboardEvent<HTMLDivElement>) {
  const t = e.target as HTMLElement
  if (t.dataset.slot !== "accordion-trigger" || !["ArrowDown", "ArrowUp", "Home", "End"].includes(e.key)) return
  const all = [...e.currentTarget.querySelectorAll<HTMLElement>("[data-slot=accordion-trigger]")]
  const i = all.indexOf(t)
  const n = all.length
  const next = e.key === "Home" ? 0 : e.key === "End" ? n - 1 : (i + (e.key === "ArrowDown" ? 1 : -1) + n) % n
  e.preventDefault()
  all[next]?.focus()
}

function CitySystems() {
  const groups = useLayerRegistry((s) => s.groups)
  const layers = useLayerRegistry((s) => s.layers)
  const open = useLayersSheet((s) => s.group)
  return (
    <Section title="City systems">
      <Accordion
        value={open ? [open] : []}
        onValueChange={(v) => setLayersGroup((v[0] as string | undefined) ?? null)}
        onKeyDown={headerKeys}
        className="-mx-1 rounded-lg border border-line"
      >
        {groups.map((g) => {
          const mine = layers.filter((l) => l.group === g.id)
          if (!mine.length) return null
          return (
            <AccordionItem key={g.id} value={g.id} className="border-line px-3">
              <AccordionTrigger className="items-center py-3 text-[14px] hover:no-underline focus-visible:ring-inset">
                <span>{g.label}</span>
                {open !== g.id && <OnCount key={mine.map((l) => l.id).join()} layers={mine} />}
              </AccordionTrigger>
              <AccordionContent className="flex flex-col gap-4 pt-1 pb-4">
                {mine.map((l) => <SystemRow key={l.id} layer={l} />)}
              </AccordionContent>
            </AccordionItem>
          )
        })}
      </Accordion>
    </Section>
  )
}

// -- Workspaces ---------------------------------------------------------------------------------

function Workspaces() {
  const list = useWorkspace((s) => s.list)
  const active = useWorkspace((s) => s.active)
  const { ref, onKeyDown, onFocus } = useRovingList<HTMLDivElement>()
  if (list.length < 2) return null
  return (
    <Section title="Workspaces">
      <ItemGroup ref={ref} aria-label="Workspaces" onKeyDown={onKeyDown} onFocus={onFocus} className="-mx-1.5 w-auto gap-0.5">
        {list.map((w) => (
          <div role="listitem" key={w.id}>
            <Item
              size="xs"
              data-row=""
              data-key={w.id}
              aria-current={w.id === active ? "page" : undefined}
              className="cursor-pointer gap-3 px-1.5 py-2 text-left hover:bg-accent/60 active:bg-accent aria-[current=page]:border-sodium/35 aria-[current=page]:bg-sodium/10"
              render={<button type="button" onClick={() => { closeLayers(true); openWorkspace(w.id) }} />}
            >
              <ItemMedia className="text-muted-foreground [&_svg]:size-[18px]"><w.icon /></ItemMedia>
              <ItemContent className="gap-0.5">
                <ItemTitle className="text-[14px]">{w.label}</ItemTitle>
                <ItemDescription className="text-[12.5px]">{cap(w.description.split(": ").slice(1).join(": ") || w.description)}</ItemDescription>
              </ItemContent>
              {w.key && <Kbd className="max-md:hidden">Alt {w.key}</Kbd>}
            </Item>
          </div>
        ))}
      </ItemGroup>
    </Section>
  )
}

// -- On the map now -----------------------------------------------------------------------------

function Ramp({ colors, from, to, what }: { colors: string[]; from: string; to: string; what: string }) {
  return (
    <div className="flex flex-col gap-1">
      <div className="h-2 rounded-full" style={{ background: `linear-gradient(90deg, ${colors.join(", ")})` }} />
      <div className="flex justify-between gap-2 font-readout text-[12px] text-muted-foreground">
        <span>{from}</span>
        <span className="truncate">{what}</span>
        <span>{to}</span>
      </div>
    </div>
  )
}

// The street a layer is about, drawn as a line of light on the map.
function StreetKey({ street }: { street: NonNullable<LayerSummary["street"]> }) {
  return (
    <p className="flex items-center gap-2 text-[12.5px] leading-snug text-muted-foreground">
      <span className="h-[3px] w-5 shrink-0 rounded-full bg-sodium shadow-[0_0_6px_1px] shadow-sodium/60" aria-hidden="true" />
      <span>
        {street.name}
        {street.follows ? `, brighter in busier hours (${street.follows.replace(/_/g, " ")})` : ", the street these stand along"}
      </span>
    </p>
  )
}

function Keys({ layer }: { layer: LayerSummary }) {
  const street = layer.street && <StreetKey street={layer.street} />
  if ((layer.style === "columns_3d" || layer.style === "heatmap") && layer.stats) {
    return (
      <>
        <Ramp colors={layer.style === "heatmap" ? HEAT : RAMP} from={fmt(layer.stats.min)} to={fmt(layer.stats.max)} what={(layer.valueField ?? "").replace(/_/g, " ")} />
        {street}
      </>
    )
  }
  const keys = Object.entries(layer.legend)
  if (!keys.length) return street || null
  return (
    <>
      <ul className="flex flex-wrap gap-x-3 gap-y-1 text-[12.5px] text-muted-foreground">
        {keys.map(([k, c]) => (
          <li key={k} className="flex items-center gap-1.5">
            <span className="size-2.5 rounded-full" style={{ background: c }} aria-hidden="true" />
            {k.replace(/_/g, " ")}
          </li>
        ))}
      </ul>
      {street}
    </>
  )
}

function OnTheMap() {
  const all = useMapUi((s) => s.layers)
  const flying = useMapUi((s) => s.flying)
  // The city systems have their switches above: here, what the answers drew.
  const layers = all.filter((l) => !isSystemRef(l.ref) && l.ref !== REGISTERS_REF)
  return (
    <Section title="On the map now">
      {layers.length === 0 ? (
        <p className="text-[13px] leading-relaxed text-muted-foreground">
          Nothing from your answers yet. Ask a question and the layers it draws appear here with their legends.
        </p>
      ) : (
        <ul className="flex flex-col gap-4">
          {layers.map((l) => {
            // A layer drawn its own way (an environment answer's grid) brings its own legend.
            const Own = layerKeyFor(l.renderer)
            return (
            <li key={l.ref} className="flex flex-col gap-2">
              <div className="flex items-start gap-2">
                <span className="min-w-0 flex-1 text-[13.5px] leading-snug font-medium">
                  {l.title}
                  <span className="ml-1.5 font-readout font-normal text-muted-foreground">{l.count.toLocaleString()}</span>
                </span>
                <Tooltip>
                  <TooltipTrigger render={<Button variant="ghost" size="icon-xs" aria-label={`Remove ${l.title}`} onClick={() => removeLayer(l.ref)} />}>
                    <XIcon />
                  </TooltipTrigger>
                  <TooltipContent>Remove this layer</TooltipContent>
                </Tooltip>
              </div>
              {Own ? <Own layer={l} /> : <Keys layer={l} />}
              <FromSources ids={l.sources} />
              {l.style === "route" && (
                <Button variant="outline" size="sm" className="self-start" aria-pressed={flying === l.ref} onClick={() => (flying === l.ref ? stopFlight() : flyRoute(l.ref))}>
                  {flying === l.ref ? <SquareIcon data-icon="inline-start" /> : <NavigationIcon data-icon="inline-start" />}
                  {flying === l.ref ? "Stop the flight" : "Fly the route"}
                </Button>
              )}
            </li>
            )
          })}
        </ul>
      )}
    </Section>
  )
}

// -- Your position ------------------------------------------------------------------------------

function Position() {
  const me = useMapUi((s) => s.me)
  return (
    <Section title="Your position">
      <p className="text-[13px] leading-relaxed text-muted-foreground" aria-live="polite">
        {me.loc ? (
          <>
            {me.loc.source === "device location" ? "From this device" : "Set on the map"}
            {me.loc.accuracy_m ? `, within ${me.loc.accuracy_m} m` : ""}, shared with the assistant.{" "}
            <button type="button" className="rounded-sm underline underline-offset-3 outline-none hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring/60" onClick={forgetMyLocation}>Forget it</button>
          </>
        ) : me.error || "Right-click the map where you are, or locate this device."}
      </p>
      {!me.loc && (
        <Button variant="outline" size="sm" className="self-start" onClick={locateMe}>
          <LocateFixedIcon data-icon="inline-start" />
          Locate me
        </Button>
      )}
    </Section>
  )
}

// -- the sheet ----------------------------------------------------------------------------------

export function LayersSheet() {
  const open = useLayout((s) => s.layersOpen)
  const g = useGeometry()
  const style = g.desktop
    ? { top: STRIP_H + GAP, right: GAP, bottom: GAP + g.layersBottom, width: g.sideW, height: "auto", maxWidth: "none" }
    : { left: PHONE_GAP, right: PHONE_GAP, bottom: PHONE_GAP, maxHeight: "62%", width: "auto" }
  return (
    <Sheet
      open={open}
      onOpenChange={(o) => useLayout.setState({ layersOpen: o })}
      modal={false}
      disablePointerDismissal
    >
      <SheetContent
        id="layers-sheet"
        side={g.desktop ? "right" : "bottom"}
        showOverlay={false}
        showCloseButton={false}
        // The keyboard goes to the sheet's title, so a screen reader starts there, and Tab goes on in.
        initialFocus={() => document.getElementById("layers-title")}
        finalFocus={() => (layersHandedOff() ? false : layersOpener() ?? document.querySelector<HTMLElement>("[data-layers-button]") ?? true)}
        className="panel-float z-30 gap-0 overflow-hidden rounded-xl border bg-console p-0 motion-reduce:transition-none"
        style={style}
      >
        <div className="flex h-12 shrink-0 items-center justify-between border-b border-line pr-2 pl-4">
          <SheetTitle id="layers-title" tabIndex={-1} className="text-[14px] font-semibold outline-none">Layers</SheetTitle>
          <SheetDescription className="sr-only">The base map, the city's systems, what answers drew and your position</SheetDescription>
          <Tooltip>
            <TooltipTrigger render={<SheetClose render={<Button variant="ghost" size="icon-sm" aria-label="Close the layers" />} />}>
              <XIcon />
            </TooltipTrigger>
            <TooltipContent>Close <span className="text-background/70">Esc</span></TooltipContent>
          </Tooltip>
        </div>
        <div className="flex min-h-0 flex-1 flex-col gap-5 overflow-y-auto p-4">
          <MapSection />
          <Separator />
          <CitySystems />
          <Workspaces />
          <Separator />
          <OnTheMap />
          <Separator />
          <Position />
        </div>
      </SheetContent>
    </Sheet>
  )
}
