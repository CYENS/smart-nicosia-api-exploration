import DOMPurify from "dompurify"
import { ChartSplineIcon, InfoIcon, ShapesIcon, TriangleAlertIcon } from "lucide-react"
import { marked } from "marked"
import { memo, useMemo } from "react"

import { Trace, seconds } from "@/components/chat/trace"
import { WallsMark } from "@/components/walls-mark"
import { Bubble, BubbleContent } from "@/components/ui/bubble"
import { Marker, MarkerContent, MarkerIcon } from "@/components/ui/marker"
import { Message, MessageContent, MessageFooter } from "@/components/ui/message"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { focusStage, useStage, type Part, type Turn } from "@/lib/chat"
import { cn } from "@/lib/utils"

marked.setOptions({ gfm: true, breaks: false })

const Answer = memo(function Answer({ text }: { text: string }) {
  const html = useMemo(() => DOMPurify.sanitize(marked.parse(text, { async: false })), [text])
  return (
    <Bubble variant="ghost">
      <BubbleContent className="prose-answer text-foreground" dangerouslySetInnerHTML={{ __html: html }} />
    </Bubble>
  )
})

// A link from the conversation to a chart or visual on the stage.
function StageLink({ stageId, title, visual }: { stageId: string; title: string; visual: "chart" | "visual" }) {
  const onStage = useStage((s) => s.open && s.focusId === stageId)
  const Icon = visual === "chart" ? ChartSplineIcon : ShapesIcon
  return (
    <button
      type="button"
      onClick={() => focusStage(stageId)}
      aria-pressed={onStage}
      className={cn(
        "flex w-full items-center gap-2.5 rounded-lg border px-3 py-2 text-left transition-colors outline-none focus-visible:ring-3 focus-visible:ring-ring/50",
        onStage ? "border-sodium/50 bg-sodium/[0.07]" : "border-line hover:bg-accent/60",
      )}
    >
      <Icon className={cn("size-4 shrink-0", onStage ? "text-sodium" : "text-muted-foreground")} />
      <span className="min-w-0 flex-1 truncate text-[13.5px] font-medium">{title}</span>
      <span className="shrink-0 font-condensed text-[12px] text-muted-foreground">{onStage ? "On the stage" : "Show"}</span>
    </button>
  )
}

function PartView({ part }: { part: Part }) {
  switch (part.kind) {
    case "text":
      return <Answer text={part.text} />
    case "trace":
      return <Trace items={part.items} />
    case "stage":
      return <StageLink stageId={part.stageId} title={part.title} visual={part.visual} />
    case "note":
      return (
        <Marker className={cn("items-start text-[13px]", part.error && "text-signal")}>
          <MarkerIcon className="mt-0.5">{part.error ? <TriangleAlertIcon /> : <InfoIcon />}</MarkerIcon>
          <MarkerContent>{part.text}</MarkerContent>
        </Marker>
      )
  }
}

function Working() {
  return (
    <div className="flex items-center gap-2 text-[13.5px] text-muted-foreground" role="status">
      <WallsMark working className="size-5 text-sodium" />
      <span className="shimmer">Working</span>
    </div>
  )
}

function TurnFooter({ turn }: { turn: Turn }) {
  const steps = turn.parts.reduce((n, p) => n + (p.kind === "trace" ? p.items.filter((i) => i.type === "step").length : 0), 0)
  const { input, output, cacheRead } = turn.usage
  const took = seconds((turn.endedAt ?? performance.now()) - turn.startedAt)
  const label = steps ? `${steps} ${steps === 1 ? "step" : "steps"}, ${took}` : took
  if (!input) return <MessageFooter className="px-0 font-condensed">{label}</MessageFooter>
  const cached = Math.round((100 * cacheRead) / input)
  return (
    <MessageFooter className="px-0">
      <Tooltip>
        <TooltipTrigger render={<span className="cursor-default font-condensed tabular-nums" />}>{label}</TooltipTrigger>
        <TooltipContent side="right">
          {input.toLocaleString()} tokens in, {cached}% cached; {output.toLocaleString()} out
        </TooltipContent>
      </Tooltip>
    </MessageFooter>
  )
}

export function TurnView({ turn }: { turn: Turn }) {
  const last = turn.parts[turn.parts.length - 1]
  const running = turn.status === "running"
  // The trace and streaming text show their own progress; between them, the walls trace.
  const quiet = running && (!last || last.kind === "stage" || last.kind === "note"
    || (last.kind === "trace" && !last.items.some((i) => i.type === "step" && i.status === "running")))
  return (
    <div className="flex flex-col gap-4">
      <Message align="end">
        <MessageContent>
          <Bubble variant="secondary" align="end">
            <BubbleContent className="text-[14.5px] whitespace-pre-wrap">{turn.question}</BubbleContent>
          </Bubble>
        </MessageContent>
      </Message>
      <Message align="start">
        <MessageContent className="gap-3">
          {turn.parts.map((p, i) => <PartView key={i} part={p} />)}
          {quiet && <Working />}
          {!running && <TurnFooter turn={turn} />}
        </MessageContent>
      </Message>
    </div>
  )
}
