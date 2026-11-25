import DOMPurify from "dompurify"
import { ChartSplineIcon, InfoIcon, ShapesIcon, TriangleAlertIcon } from "lucide-react"
import { marked } from "marked"
import { memo, useMemo } from "react"

import { SubjectChip } from "@/components/chat/composer"
import { Trace, seconds } from "@/components/chat/trace"
import { WallsMark } from "@/components/walls-mark"
import { Bubble, BubbleContent } from "@/components/ui/bubble"
import { Marker, MarkerContent, MarkerIcon } from "@/components/ui/marker"
import { Message, MessageContent, MessageFooter } from "@/components/ui/message"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { focusStage, useStage, type Part, type Turn } from "@/lib/chat"
import { ID_IN_TEXT, kindMeta, kindOfId } from "@/lib/entities"
import { inspect } from "@/lib/inspector"
import { cn } from "@/lib/utils"

marked.setOptions({ gfm: true, breaks: false })

// A table cell that holds only a number, with or without its unit ("45 m", "~20 m", "8 floors",
// "12.7 min", "61%"), which reads badly when it wraps.
const NUMBER_CELL = /^[~≈<>±+−-]?\s*\d[\d.,]*\s*(%|°C|m²|m³|m|km|min|s|h|kW|W|dB|floors?)?$/

function markNumberCells(html: string) {
  if (!html.includes("<td")) return html
  const t = document.createElement("template")
  t.innerHTML = html
  for (const td of t.content.querySelectorAll("td")) {
    if (NUMBER_CELL.test(td.textContent?.trim() ?? "")) td.classList.add("num")
  }
  return t.innerHTML
}

// Ids the assistant names (YL1015, NIC-409, B255563) become chips that open the inspector. Only
// text outside code and links is touched, and the chips are built from the matched id alone.
function chipIds(html: string) {
  ID_IN_TEXT.lastIndex = 0
  if (!ID_IN_TEXT.test(html)) return html
  const t = document.createElement("template")
  t.innerHTML = html
  const walker = document.createTreeWalker(t.content, NodeFilter.SHOW_TEXT)
  const texts: Text[] = []
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    if (!(n.parentElement?.closest("code, pre, a, button"))) texts.push(n as Text)
  }
  for (const node of texts) {
    const s = node.data
    ID_IN_TEXT.lastIndex = 0
    if (!ID_IN_TEXT.test(s)) continue
    ID_IN_TEXT.lastIndex = 0 // matchAll starts where test() stopped otherwise
    const frag = document.createDocumentFragment()
    let last = 0
    for (const m of s.matchAll(ID_IN_TEXT)) {
      const kind = kindOfId(m[0])
      if (!kind) continue
      frag.append(s.slice(last, m.index))
      const b = document.createElement("button")
      b.type = "button"
      b.className = "entity-chip"
      b.dataset.entity = m[0]
      b.dataset.chip = kindMeta(kind).chip
      b.title = `Open ${m[0]} (${kindMeta(kind).label.toLowerCase()}) in the inspector`
      b.textContent = m[0]
      frag.append(b)
      last = (m.index ?? 0) + m[0].length
    }
    frag.append(s.slice(last))
    node.replaceWith(frag)
  }
  return t.innerHTML
}

function openChip(e: React.MouseEvent) {
  const chip = (e.target as HTMLElement).closest<HTMLElement>("[data-entity]")
  if (chip?.dataset.entity) inspect({ id: chip.dataset.entity }, { fly: true, focus: e.detail === 0 })
}

const Answer = memo(function Answer({ text }: { text: string }) {
  const html = useMemo(() => chipIds(markNumberCells(DOMPurify.sanitize(marked.parse(text, { async: false })))), [text])
  return (
    <Bubble variant="ghost">
      <BubbleContent className="prose-answer text-foreground" onClick={openChip} dangerouslySetInnerHTML={{ __html: html }} />
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
  // The footer shows once the turn is done, and a done turn always has its end time.
  const took = seconds((turn.endedAt ?? turn.startedAt) - turn.startedAt)
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
          {turn.subject && <div className="flex justify-end"><SubjectChip subject={turn.subject} /></div>}
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
