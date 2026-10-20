import { PauseIcon, PlayIcon, XIcon } from "lucide-react"

import { Button } from "@/components/ui/button"
import { Slider } from "@/components/ui/slider"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { useGeometry } from "@/lib/geometry"
import { GAP, STRIP_H } from "@/lib/layout"
import {
  cycleSpeed, pauseTimelapse, playTimelapse, seekFrame, stopTimelapse, useMapUi,
} from "@/lib/map/engine"

// The replay console: a time-lapse's clock, big, over the open part of the map.
export function TimelapsePlayer() {
  const tl = useMapUi((s) => s.timelapse)
  const g = useGeometry()
  if (!tl) return null
  return (
    <div
      className="pointer-events-none absolute z-10 flex justify-center"
      style={{ top: STRIP_H + GAP, left: g.left, right: g.topRight }}
    >
      <div role="group" aria-label="Time-lapse" className="panel-float pointer-events-auto flex w-full max-w-[600px] items-center gap-3 rounded-xl py-2 pr-2 pl-2.5">
        <Button
          size="icon"
          className="rounded-full"
          aria-label={tl.playing ? "Pause" : "Play"}
          onClick={() => (tl.playing ? pauseTimelapse() : playTimelapse())}
        >
          {tl.playing ? <PauseIcon /> : <PlayIcon />}
        </Button>
        <div className="flex min-w-0 flex-1 flex-col gap-1.5">
          <div className="flex items-baseline justify-between gap-3">
            <span className="truncate text-[12.5px] text-muted-foreground">{tl.title}</span>
            <span className="shrink-0 font-readout text-[22px] leading-none font-semibold text-sodium" aria-live="off">{tl.time}</span>
          </div>
          <Slider
            aria-label="Time"
            min={0}
            max={Math.max(0, tl.frames - 1)}
            step={1}
            value={tl.frame}
            onValueChange={(v) => seekFrame(Array.isArray(v) ? v[0] : v)}
          />
        </div>
        <Tooltip>
          <TooltipTrigger render={<Button variant="ghost" size="sm" className="font-readout" onClick={cycleSpeed} />}>
            {tl.speed}×
          </TooltipTrigger>
          <TooltipContent>Playback speed</TooltipContent>
        </Tooltip>
        <Tooltip>
          <TooltipTrigger render={<Button variant="ghost" size="icon-sm" aria-label="Close the time-lapse" onClick={stopTimelapse} />}>
            <XIcon />
          </TooltipTrigger>
          <TooltipContent>Close the time-lapse</TooltipContent>
        </Tooltip>
      </div>
    </div>
  )
}
