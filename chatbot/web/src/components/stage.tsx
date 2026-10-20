import { ChartSplineIcon, ChevronDownIcon, Maximize2Icon, Minimize2Icon, ShapesIcon } from "lucide-react"
import { useEffect, useRef, useState } from "react"

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { drawChart, type ChartPayload } from "@/lib/charts"
import { focusStage, setStageOpen, toggleStageExpanded, useStage, type StageItem } from "@/lib/chat"
import { useGeometry } from "@/lib/geometry"
import { GAP, STRIP_H } from "@/lib/layout"
import { registerVisual, unregisterVisual, type Visual } from "@/lib/map/engine"
import { cn } from "@/lib/utils"
import { visualDoc, type VisualPayload } from "@/lib/visual"

function Problem({ title, text }: { title: string; text: string }) {
  return (
    <Alert variant="destructive">
      <AlertTitle>{title}</AlertTitle>
      <AlertDescription className="font-condensed">{text}</AlertDescription>
    </Alert>
  )
}

function ChartView({ chart, size }: { chart: ChartPayload; size: string }) {
  const ref = useRef<HTMLDivElement>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    const el = ref.current!
    let cleanup: (() => void) | null = null
    let cancelled = false
    // Room for the axes and a legend under the plot itself.
    const height = Math.max(150, (el.parentElement?.clientHeight ?? 300) - 84)
    drawChart(el, chart, height)
      .then((c) => { if (cancelled) c(); else cleanup = c })
      .catch((e: Error) => setError(e.message))
    return () => { cancelled = true; cleanup?.() }
  }, [chart, size])
  if (error) return <Problem title="This chart didn't draw" text={error} />
  return <div ref={ref} className="w-full" />
}

function VisualView({ visual, size }: { visual: VisualPayload; size: string }) {
  const frame = useRef<HTMLIFrameElement>(null)
  const box = useRef<HTMLDivElement>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    const iframe = frame.current!, container = box.current!
    // The visual fills the stage: WIDTH and HEIGHT are the space it actually has.
    const height = Math.max(200, (container.parentElement?.clientHeight ?? visual.height) - 8)
    iframe.style.height = `${height}px`
    setError(null)
    const v: Visual = {
      iframe, title: visual.title,
      onSize: (h) => { iframe.style.height = `${Math.max(80, Math.min(4000, h))}px` },
      onError: setError,
    }
    registerVisual(v)
    visualDoc(visual, Math.max(200, container.clientWidth), height)
      .then((doc) => { iframe.srcdoc = doc })
      .catch((e: Error) => setError(e.message))
    return () => unregisterVisual(v)
  }, [visual, size])
  return (
    <div ref={box} className="flex flex-col gap-2">
      {error && <Problem title="This visual hit an error" text={error} />}
      <iframe
        ref={frame}
        sandbox="allow-scripts"
        title={visual.title}
        referrerPolicy="no-referrer"
        className="block w-full rounded-md border-0 bg-console"
      />
    </div>
  )
}

const KIND = {
  chart: { icon: ChartSplineIcon, label: "Chart" },
  visual: { icon: ShapesIcon, label: "Custom visual" },
}

function Filmstrip({ items, focusId }: { items: StageItem[]; focusId: string }) {
  return (
    <nav
      aria-label="Visuals in this conversation"
      className="flex shrink-0 gap-1.5 border-line p-2 max-md:overflow-x-auto max-md:border-t md:w-48 md:flex-col md:overflow-y-auto md:border-l"
    >
      {items.map((it) => {
        const Icon = KIND[it.kind].icon
        const on = it.id === focusId
        return (
          <button
            key={it.id}
            type="button"
            aria-current={on}
            onClick={() => focusStage(it.id)}
            className={cn(
              "flex items-start gap-2 rounded-lg border px-2.5 py-2 text-left text-[12.5px] leading-snug transition-colors outline-none focus-visible:ring-3 focus-visible:ring-ring/50 max-md:w-44 max-md:shrink-0",
              on ? "border-sodium/50 bg-sodium/[0.07]" : "border-line hover:bg-accent/60",
            )}
          >
            <Icon className={cn("mt-0.5 size-3.5 shrink-0", on ? "text-sodium" : "text-muted-foreground")} />
            <span className="line-clamp-2">{it.title}</span>
          </button>
        )
      })}
    </nav>
  )
}

// The stage: charts and custom visuals get the width of the map instead of a chat column.
export function Stage() {
  const { items, focusId, open, expanded } = useStage()
  const g = useGeometry()
  if (!items.length) return null
  const focused = items.find((i) => i.id === focusId) ?? items[items.length - 1]
  const { icon: Icon, label } = KIND[focused.kind]

  if (!open) {
    return (
      <Button
        variant="secondary"
        className="panel-float absolute z-10 h-10 gap-2 rounded-xl px-3.5"
        style={{ left: g.left, bottom: g.desktop ? GAP : undefined, top: g.desktop ? undefined : STRIP_H + 8 }}
        onClick={() => setStageOpen(true)}
      >
        <ChartSplineIcon data-icon="inline-start" />
        {items.length === 1 ? "Show the chart" : `Show ${items.length} visuals`}
      </Button>
    )
  }

  const size = `${g.left}:${g.right}:${g.stageH}:${expanded}`
  const style = g.desktop
    ? { left: g.left, right: g.right, bottom: GAP, top: expanded ? STRIP_H + GAP : undefined, height: expanded ? undefined : g.stageH }
    : { left: 8, right: 8, top: STRIP_H + 8, height: expanded ? `calc(100% - ${STRIP_H + 16}px)` : g.stageH }

  return (
    <section aria-label="Visual stage" className="panel-float absolute z-10 flex overflow-hidden rounded-xl max-md:flex-col" style={style}>
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-11 shrink-0 items-center gap-2.5 border-b border-line pr-2 pl-4">
          <Icon className="size-4 shrink-0 text-sodium" />
          <h2 className="min-w-0 truncate text-[14px] font-semibold">{focused.title}</h2>
          <span className="shrink-0 font-condensed text-[12px] text-muted-foreground">{label}</span>
          <div className="ml-auto flex shrink-0 items-center gap-0.5">
            <Tooltip>
              <TooltipTrigger render={<Button variant="ghost" size="icon-sm" aria-label={expanded ? "Shrink the stage" : "Enlarge the stage"} onClick={toggleStageExpanded} />}>
                {expanded ? <Minimize2Icon /> : <Maximize2Icon />}
              </TooltipTrigger>
              <TooltipContent>{expanded ? "Shrink the stage" : "Enlarge the stage"}</TooltipContent>
            </Tooltip>
            <Tooltip>
              <TooltipTrigger render={<Button variant="ghost" size="icon-sm" aria-label="Hide the stage" onClick={() => setStageOpen(false)} />}>
                <ChevronDownIcon />
              </TooltipTrigger>
              <TooltipContent>Hide the stage</TooltipContent>
            </Tooltip>
          </div>
        </header>
        <div className="relative min-h-0 flex-1 overflow-auto px-4 py-3">
          {focused.kind === "chart"
            ? <ChartView key={focused.id} chart={focused.chart!} size={size} />
            : <VisualView key={focused.id} visual={focused.visual!} size={size} />}
        </div>
      </div>
      {items.length > 1 && <Filmstrip items={items} focusId={focused.id} />}
    </section>
  )
}
