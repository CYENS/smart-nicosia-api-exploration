import DOMPurify from "dompurify"
import {
  ChartSplineIcon, CheckIcon, CopyIcon, CornerDownRightIcon, InfoIcon, PencilIcon, RotateCwIcon, ShapesIcon,
  SquareIcon, TriangleAlertIcon,
} from "lucide-react"
import { marked } from "marked"
import { memo, useEffect, useMemo, useRef, useState } from "react"

import { SubjectChip } from "@/components/chat/composer"
import { Trace, seconds } from "@/components/chat/trace"
import { WallsMark } from "@/components/walls-mark"
import { Bubble, BubbleContent } from "@/components/ui/bubble"
import { Button } from "@/components/ui/button"
import { InputGroup, InputGroupAddon, InputGroupButton, InputGroupText, InputGroupTextarea } from "@/components/ui/input-group"
import { Kbd } from "@/components/ui/kbd"
import { Marker, MarkerContent, MarkerIcon } from "@/components/ui/marker"
import { Message, MessageContent, MessageFooter } from "@/components/ui/message"
import { Spinner } from "@/components/ui/spinner"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { ACTIONS } from "@/lib/actions"
import { answerText, edit, focusComposer, focusStage, retry, runAction, send, useStage, type Part, type Turn } from "@/lib/chat"
import { ID_IN_TEXT, kindMeta, kindOfId } from "@/lib/entities"
import { inspect } from "@/lib/inspector"
import { useRovingList } from "@/lib/roving"
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

// A quiet icon button with its name in a tooltip, under a question or an answer.
function ActionButton({ label, onClick, children, buttonRef }: {
  label: string
  onClick: (e: React.MouseEvent) => void
  children: React.ReactNode
  buttonRef?: React.Ref<HTMLButtonElement>
}) {
  return (
    <Tooltip>
      <TooltipTrigger
        render={
          <Button
            ref={buttonRef}
            variant="ghost"
            size="icon-sm"
            aria-label={label}
            className="text-muted-foreground hover:text-foreground"
            onClick={onClick}
          />
        }
      >
        {children}
      </TooltipTrigger>
      <TooltipContent>{label}</TooltipContent>
    </Tooltip>
  )
}

async function copyText(text: string) {
  try {
    await navigator.clipboard.writeText(text)
    return true
  } catch {
    // No clipboard API here (a page served over plain http on another host): the old way.
    const area = document.createElement("textarea")
    area.value = text
    area.setAttribute("readonly", "")
    area.style.position = "fixed"
    area.style.opacity = "0"
    document.body.append(area)
    area.select()
    const ok = document.execCommand("copy")
    area.remove()
    return ok
  }
}

function CopyAnswer({ text }: { text: string }) {
  const [copied, setCopied] = useState<"" | "ok" | "fail">("")
  useEffect(() => {
    if (!copied) return
    const t = window.setTimeout(() => setCopied(""), 2000)
    return () => window.clearTimeout(t)
  }, [copied])
  return (
    <>
      <ActionButton label="Copy the answer" onClick={async () => setCopied((await copyText(text)) ? "ok" : "fail")}>
        {copied === "ok" ? <CheckIcon /> : <CopyIcon />}
      </ActionButton>
      <span className="sr-only" aria-live="polite">
        {copied === "ok" ? "Copied" : copied === "fail" ? "Couldn't copy here" : ""}
      </span>
    </>
  )
}

// Under a finished answer: Copy, Retry on the latest one, and how long it took.
function TurnFooter({ turn, latest }: { turn: Turn; latest: boolean }) {
  const steps = turn.parts.reduce((n, p) => n + (p.kind === "trace" ? p.items.filter((i) => i.type === "step").length : 0), 0)
  const { input, output, cacheRead } = turn.usage
  // The footer shows once the turn is over, and a turn that is over always has its end time.
  const took = seconds((turn.endedAt ?? turn.startedAt) - turn.startedAt)
  const label = steps ? `${steps} ${steps === 1 ? "step" : "steps"}, ${took}` : took
  const text = answerText(turn)
  const cached = input ? Math.round((100 * cacheRead) / input) : 0
  return (
    <MessageFooter className="-ml-1.5 gap-0.5 px-0">
      {text && <CopyAnswer text={text} />}
      {latest && (
        <ActionButton
          label="Retry"
          onClick={(e) => { void retry(turn); if (e.detail === 0) focusComposer() }}
        >
          <RotateCwIcon />
        </ActionButton>
      )}
      {input ? (
        <Tooltip>
          <TooltipTrigger render={<span className="ml-1.5 cursor-default font-condensed tabular-nums" />}>{label}</TooltipTrigger>
          <TooltipContent side="right">
            {input.toLocaleString()} tokens in, {cached}% cached; {output.toLocaleString()} out
          </TooltipContent>
        </Tooltip>
      ) : (
        <span className="ml-1.5 font-condensed tabular-nums">{label}</span>
      )}
    </MessageFooter>
  )
}

// What to do and ask next, or the options of a clarifying question: one group of chips, arrow
// keys inside it. Actions come first (lib/actions.ts: an icon, a stronger outline, and the page
// does them); a question chip asks its question.
function FollowUps({ turnId, actions, items, choices }: { turnId: string; actions: string[]; items: string[]; choices: boolean }) {
  const { ref, onKeyDown, onFocus } = useRovingList<HTMLDivElement>({ orientation: "both" })
  const [pending, setPending] = useState<string | null>(null)
  const act = async (key: string, e: React.MouseEvent<HTMLButtonElement>) => {
    const opener = e.currentTarget
    setPending(key)
    try {
      await runAction(turnId, key, { opener, keyboard: e.detail === 0 })
    } finally {
      setPending(null)
    }
  }
  return (
    <div
      ref={ref}
      role="toolbar"
      aria-label={choices ? "Choose an answer" : "Suggested next steps"}
      onKeyDown={onKeyDown}
      onFocus={onFocus}
      className="flex flex-wrap gap-1.5"
    >
      {actions.map((key) => {
        const { label, icon: Icon, pending: waiting } = ACTIONS[key]
        const busy = pending === key
        return (
          <Button
            key={key}
            data-row
            data-key={`do:${key}`}
            data-action={key}
            variant="outline"
            size="sm"
            aria-busy={busy || undefined}
            className="h-auto min-h-8 max-w-full justify-start rounded-full border-foreground/35 px-3 py-1 text-left text-[13.5px] leading-snug font-medium whitespace-normal"
            onClick={(e) => { if (!pending) void act(key, e) }}
          >
            {busy ? <Spinner className="shrink-0" /> : <Icon className="shrink-0" aria-hidden="true" />}
            {busy && waiting ? waiting : label}
          </Button>
        )
      })}
      {items.map((q) => (
        <Button
          key={q}
          data-row
          data-key={q}
          variant="outline"
          size="sm"
          className={cn(
            "h-auto min-h-8 max-w-full justify-start rounded-full px-3 py-1 text-left text-[13.5px] leading-snug font-normal whitespace-normal",
            !choices && "text-muted-foreground hover:text-foreground",
          )}
          onClick={(e) => { void send(q); if (e.detail === 0) focusComposer() }}
        >
          {!choices && <CornerDownRightIcon className="shrink-0" aria-hidden="true" />}
          {q}
        </Button>
      ))}
    </div>
  )
}

// The question as asked, and on the latest turn a way to change it: Edit turns the bubble into a
// field like the composer's (Enter asks it, Shift+Enter adds a line, Esc puts it back).
function Question({ turn, editable }: { turn: Turn; editable: boolean }) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(turn.question)
  const area = useRef<HTMLTextAreaElement>(null)
  const editButton = useRef<HTMLButtonElement>(null)
  const refocus = useRef(false)
  const open = editing && editable

  useEffect(() => {
    if (open) {
      const a = area.current
      a?.focus()
      a?.setSelectionRange(a.value.length, a.value.length)
    } else if (refocus.current) {
      refocus.current = false
      editButton.current?.focus()
    }
  }, [open])

  const cancel = () => {
    refocus.current = true
    setEditing(false)
  }
  const submit = () => {
    if (!draft.trim()) return
    setEditing(false)
    void edit(turn, draft)
    focusComposer()
  }

  if (open) {
    return (
      <Message align="end">
        <form className="w-full" onSubmit={(e) => { e.preventDefault(); submit() }}>
          <InputGroup className="rounded-xl bg-night/70 dark:bg-night/70">
            <InputGroupTextarea
              ref={area}
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); submit() }
                if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); cancel() }
              }}
              rows={1}
              aria-label="Edit your question"
              className="max-h-48 min-h-11 px-3.5 pt-3 text-[14.5px]"
            />
            <InputGroupAddon align="block-end" className="pb-2">
              <InputGroupText className="text-[12px] max-md:hidden">
                <Kbd>Enter</Kbd> asks, <Kbd>Esc</Kbd> cancels
              </InputGroupText>
              <InputGroupButton variant="ghost" size="sm" className="ml-auto" onClick={cancel}>Cancel</InputGroupButton>
              <InputGroupButton type="submit" variant="default" size="sm" disabled={!draft.trim()}>Ask</InputGroupButton>
            </InputGroupAddon>
          </InputGroup>
        </form>
      </Message>
    )
  }

  return (
    <Message align="end">
      <MessageContent>
        {turn.subject && <div className="flex justify-end"><SubjectChip subject={turn.subject} /></div>}
        <Bubble variant="secondary" align="end">
          <BubbleContent className="text-[14.5px] whitespace-pre-wrap">{turn.question}</BubbleContent>
        </Bubble>
        {editable && (
          <MessageFooter className="-mr-1.5 px-0">
            <ActionButton
              label="Edit the question"
              buttonRef={editButton}
              onClick={() => { setDraft(turn.question); setEditing(true) }}
            >
              <PencilIcon />
            </ActionButton>
          </MessageFooter>
        )}
      </MessageContent>
    </Message>
  )
}

export function TurnView({ turn, latest }: { turn: Turn; latest: boolean }) {
  const last = turn.parts[turn.parts.length - 1]
  const running = turn.status === "running"
  // Only the latest turn, once it is over, can be asked again or changed, and offers follow-ups.
  const current = latest && !running
  // The trace and streaming text show their own progress; between them, the walls trace.
  const quiet = running && (!last || last.kind === "stage" || last.kind === "note"
    || (last.kind === "trace" && !last.items.some((i) => i.type === "step" && i.status === "running")))
  const offer = current && turn.status === "done"
  const choices = offer ? turn.choices : undefined
  const next = offer && !choices?.length ? turn.next : undefined
  const actions = offer ? turn.actions ?? [] : []
  return (
    <div className="flex flex-col gap-4">
      <Question turn={turn} editable={current} />
      <Message align="start">
        <MessageContent className="gap-3">
          {turn.parts.map((p, i) => <PartView key={i} part={p} />)}
          {quiet && <Working />}
          {turn.status === "stopped" && (
            <Marker className="text-[13px] text-muted-foreground">
              <MarkerIcon><SquareIcon /></MarkerIcon>
              <MarkerContent>Stopped</MarkerContent>
            </Marker>
          )}
          {!!choices?.length && <FollowUps turnId={turn.id} actions={actions} items={choices} choices />}
          {!running && <TurnFooter turn={turn} latest={current} />}
          {!choices?.length && (actions.length > 0 || !!next?.length) && (
            <FollowUps turnId={turn.id} actions={actions} items={next ?? []} choices={false} />
          )}
        </MessageContent>
      </Message>
    </div>
  )
}
