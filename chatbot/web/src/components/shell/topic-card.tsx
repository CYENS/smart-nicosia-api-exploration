/* The open topic's card, at the bottom left of the open map (a bottom sheet on a phone): the
   summary in plain words, the legend, where it comes from, a question to ask about it and one way
   to go deeper. Esc or the close button lets the topic go and gives the keyboard back to the
   chips. */
import { ArrowRightIcon, MessageSquareTextIcon, XIcon } from "lucide-react"
import { createElement } from "react"

import { focusChips } from "@/components/shell/topic-chips"
import { FromSources } from "@/components/sources/from-sources"
import { Button } from "@/components/ui/button"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { send, useChat } from "@/lib/chat"
import { useGeometry } from "@/lib/geometry"
import { PHONE_GAP, setChatOpen } from "@/lib/layout"
import { selectTopic, useTopics } from "@/lib/registries/topics"

export function TopicCard() {
  const topic = useTopics((s) => s.list.find((t) => t.id === s.active) ?? null)
  const sections = useTopics((s) => s.sections)
  const busy = useChat((s) => s.busy)
  const g = useGeometry()
  if (!topic || !g.cardShown) return null

  const close = () => { selectTopic(null); focusChips() }
  const deeper = topic.deeper?.() ?? null
  const question = topic.ask?.()
  const title = topic.title ?? topic.label
  const style = g.desktop
    ? { left: g.left, bottom: g.cardBottom, width: g.sideW }
    : { left: PHONE_GAP, right: PHONE_GAP, bottom: PHONE_GAP, maxHeight: "50%" }

  return (
    <section
      aria-labelledby="topic-card-title"
      className="panel-float absolute z-20 flex flex-col overflow-hidden rounded-xl motion-safe:animate-in motion-safe:fade-in-0 motion-safe:slide-in-from-bottom-2"
      style={style}
      onKeyDown={(e) => { if (e.key === "Escape") { e.preventDefault(); close() } }}
    >
      <header className="flex h-12 shrink-0 items-center gap-2.5 border-b border-line pr-2 pl-4">
        <topic.icon className="size-4 shrink-0 text-sodium" aria-hidden="true" />
        <h2 id="topic-card-title" className="min-w-0 flex-1 truncate text-[14px] font-semibold">{title}</h2>
        <Tooltip>
          <TooltipTrigger render={<Button variant="ghost" size="icon-sm" aria-label={`Close ${title.toLowerCase()}`} onClick={close} />}>
            <XIcon />
          </TooltipTrigger>
          <TooltipContent>Close <span className="text-background/70">Esc</span></TooltipContent>
        </Tooltip>
      </header>
      <div className="flex min-h-0 flex-col gap-3.5 overflow-y-auto px-4 pt-3.5 pb-3">
        <topic.Summary />
        {topic.Legend && <topic.Legend />}
        {sections.filter((s) => s.topic === topic.id).map((s) => <div key={s.id}>{createElement(s.Section)}</div>)}
        <FromSources ids={topic.sources} />
      </div>
      {(question || deeper) && (
        <footer className="flex shrink-0 flex-wrap items-center gap-2 border-t border-line px-3 py-2.5">
          {question && (
            <Button
              variant="secondary"
              size="sm"
              disabled={busy}
              onClick={() => {
                void send(question)
                setChatOpen(true)
              }}
            >
              <MessageSquareTextIcon data-icon="inline-start" />
              Ask about it
            </Button>
          )}
          {deeper && (
            <Button variant="ghost" size="sm" className="ml-auto" onClick={deeper.run}>
              {deeper.label}
              <ArrowRightIcon data-icon="inline-end" />
            </Button>
          )}
        </footer>
      )}
    </section>
  )
}
