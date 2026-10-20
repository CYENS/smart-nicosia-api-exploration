import { LayersIcon, LocateFixedIcon, MoonIcon, PanelRightCloseIcon, SunIcon, SunMoonIcon, XIcon } from "lucide-react"

import { Button } from "@/components/ui/button"
import { Field, FieldContent, FieldDescription, FieldGroup, FieldLabel } from "@/components/ui/field"
import { Separator } from "@/components/ui/separator"
import { Switch } from "@/components/ui/switch"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { useGeometry } from "@/lib/geometry"
import { useLayout } from "@/lib/layout"
import type { Basemap } from "@/lib/map/basemap"
import {
  RAMP, cityHeights, fmt, forgetMyLocation, locateMe, nicosiaTime, removeLayer, setBasemapMode,
  setCity3d, setNightLights, showRegisters, useMapUi,
  type LayerSummary,
} from "@/lib/map/engine"

const HEAT = ["#3b82f6", "#22c55e", "#eab308", "#f97316", "#dc2626"]

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

function LayerRow({ title, count, onRemove, removeLabel, children }: {
  title: string; count?: number; onRemove: () => void; removeLabel: string; children: React.ReactNode
}) {
  return (
    <li className="flex flex-col gap-2">
      <div className="flex items-start gap-2">
        <span className="min-w-0 flex-1 text-[13.5px] leading-snug font-medium">
          {title}
          {count !== undefined && <span className="ml-1.5 font-readout font-normal text-muted-foreground">{count.toLocaleString()}</span>}
        </span>
        <Tooltip>
          <TooltipTrigger render={<Button variant="ghost" size="icon-xs" aria-label={removeLabel} onClick={onRemove} />}>
            <XIcon />
          </TooltipTrigger>
          <TooltipContent>{removeLabel}</TooltipContent>
        </Tooltip>
      </div>
      {children}
    </li>
  )
}

function Keys({ layer }: { layer: LayerSummary }) {
  if ((layer.style === "columns_3d" || layer.style === "heatmap") && layer.stats) {
    return (
      <Ramp
        colors={layer.style === "heatmap" ? HEAT : RAMP}
        from={fmt(layer.stats.min)}
        to={fmt(layer.stats.max)}
        what={(layer.valueField ?? "").replace(/_/g, " ")}
      />
    )
  }
  const keys = Object.entries(layer.legend)
  if (!keys.length) return null
  return (
    <ul className="flex flex-wrap gap-x-3 gap-y-1 text-[12.5px] text-muted-foreground">
      {keys.map(([k, c]) => (
        <li key={k} className="flex items-center gap-1.5">
          <span className="size-2.5 rounded-full" style={{ background: c }} aria-hidden="true" />
          {k.replace(/_/g, " ")}
        </li>
      ))}
    </ul>
  )
}

export function LayersPanel() {
  const open = useLayout((s) => s.layersOpen)
  const mode = useMapUi((s) => s.basemapMode)
  const sky = useMapUi((s) => s.skyNow)
  const nightLights = useMapUi((s) => s.nightLights)
  const layers = useMapUi((s) => s.layers)
  const city = useMapUi((s) => s.city)
  const registers = useMapUi((s) => s.registers)
  const me = useMapUi((s) => s.me)
  const { layersBottom } = useGeometry()

  if (!open) {
    return (
      <Button
        variant="secondary"
        className="panel-float absolute top-[64px] right-3 z-10 h-10 gap-2 rounded-xl px-3.5"
        onClick={() => useLayout.setState({ layersOpen: true })}
      >
        <LayersIcon data-icon="inline-start" />
        Map layers
        {layers.length + (city.on ? 1 : 0) > 0 && (
          <span className="font-readout text-muted-foreground">{layers.length + (city.on ? 1 : 0)}</span>
        )}
      </Button>
    )
  }

  const heights = cityHeights()
  const stops: string[] = []
  for (let i = 0; i < heights.length; i += 2) stops.push(`${heights[i + 1]} ${Math.round((100 * (heights[i] as number)) / 60)}%`)
  const status = [registers.status, city.status].filter(Boolean)

  return (
    <aside
      aria-label="Map layers"
      className="panel-float absolute top-[64px] right-3 z-10 flex w-[min(264px,calc(100%-24px))] flex-col overflow-hidden rounded-xl max-md:z-30"
      style={{ maxHeight: `calc(100% - ${76 + layersBottom}px)` }}
    >
      <div className="flex h-12 shrink-0 items-center justify-between border-b border-line pr-2 pl-4">
        <h2 className="text-[14px] font-semibold">Map</h2>
        <Tooltip>
          <TooltipTrigger render={<Button variant="ghost" size="icon-sm" aria-label="Hide the map panel" onClick={() => useLayout.setState({ layersOpen: false })} />}>
            <PanelRightCloseIcon />
          </TooltipTrigger>
          <TooltipContent>Hide the map panel</TooltipContent>
        </Tooltip>
      </div>

      <div className="flex min-h-0 flex-col gap-4 overflow-y-auto p-4">
        <FieldGroup className="gap-4">
          <Field>
            <FieldLabel>Base map</FieldLabel>
            <ToggleGroup
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
                ? `Follows the sun: ${sky.dark ? "night" : "day"} until ${nicosiaTime(sky.next.at)}`
                : mode === "night" ? "Night, whatever the time" : "Day, whatever the time"}
            </FieldDescription>
          </Field>
          <Field orientation="horizontal">
            <FieldContent>
              <FieldLabel htmlFor="tg-lights">Street lights</FieldLabel>
              <FieldDescription>Lit lamps glow after dark</FieldDescription>
            </FieldContent>
            <Switch id="tg-lights" checked={nightLights} onCheckedChange={(on) => setNightLights(on)} />
          </Field>
          <Field orientation="horizontal">
            <FieldContent>
              <FieldLabel htmlFor="tg-city3d">3D city</FieldLabel>
              <FieldDescription>Every building at its DLS height</FieldDescription>
            </FieldContent>
            <Switch id="tg-city3d" checked={city.on} onCheckedChange={(on) => setCity3d(on)} />
          </Field>
          <Field orientation="horizontal">
            <FieldContent>
              <FieldLabel htmlFor="tg-dangerous">Dangerous buildings</FieldLabel>
              <FieldDescription>The ΕΟΑ registers of four districts</FieldDescription>
            </FieldContent>
            <Switch id="tg-dangerous" checked={registers.on} onCheckedChange={(on) => showRegisters(on)} />
          </Field>
          <Field>
            <FieldLabel>Your position</FieldLabel>
            <FieldDescription aria-live="polite">
              {me.loc ? (
                <>
                  {me.loc.source === "device location" ? "From this device" : "Set on the map"}
                  {me.loc.accuracy_m ? `, within ${me.loc.accuracy_m} m` : ""}, shared with the assistant.{" "}
                  <button type="button" className="underline underline-offset-3 hover:text-foreground" onClick={forgetMyLocation}>Forget it</button>
                </>
              ) : me.error || "Right-click the map where you are, or locate this device."}
            </FieldDescription>
            {!me.loc && (
              <Button variant="outline" size="sm" className="self-start" onClick={locateMe}>
                <LocateFixedIcon data-icon="inline-start" />
                Locate me
              </Button>
            )}
          </Field>
        </FieldGroup>
        {status.length > 0 && (
          <p className="font-condensed text-[12.5px] text-muted-foreground" aria-live="polite">{status.join(". ")}</p>
        )}

        <Separator />

        <section aria-label="On the map" className="flex flex-col gap-3">
          <h3 className="text-[12.5px] text-muted-foreground">On the map</h3>
          {layers.length === 0 && !city.on ? (
            <p className="text-[13px] leading-relaxed text-muted-foreground">
              Nothing drawn yet. Layers from your answers appear here with their legends.
            </p>
          ) : (
            <ul className="flex flex-col gap-4">
              {layers.map((l) => (
                <LayerRow key={l.ref} title={l.title} count={l.count} removeLabel="Remove this layer" onRemove={() => removeLayer(l.ref)}>
                  <Keys layer={l} />
                </LayerRow>
              ))}
              {city.on && (
                <LayerRow title="3D city (DLS)" removeLabel="Turn off the 3D city" onRemove={() => setCity3d(false)}>
                  <Ramp colors={stops} from="0 m" to="60 m+" what="height to roof" />
                </LayerRow>
              )}
            </ul>
          )}
        </section>
      </div>
    </aside>
  )
}
