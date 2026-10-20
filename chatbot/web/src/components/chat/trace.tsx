import { CheckIcon, ChevronRightIcon, XIcon } from "lucide-react"

import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible"
import { toolLabel, type TraceItem } from "@/lib/chat"
import { cn } from "@/lib/utils"

export function seconds(ms: number) {
  return ms < 1000 ? `${Math.max(0.1, Math.round(ms / 100) / 10).toFixed(1)} s` : `${(ms / 1000).toFixed(1)} s`
}

function StepIcon({ status }: { status: "running" | "ok" | "bad" }) {
  if (status === "running") {
    return <span className="live-dot mx-[3px] size-2 rounded-full bg-sodium" aria-label="Running" />
  }
  if (status === "bad") return <XIcon className="size-3.5 text-signal" aria-label="Failed" />
  return <CheckIcon className="size-3.5 text-muted-foreground" aria-label="Done" />
}

// The assistant's work as it happens: each tool call with what it found and how long it took.
export function Trace({ items }: { items: TraceItem[] }) {
  return (
    <ol className="flex flex-col gap-1.5 border-l border-line py-0.5 pl-3 font-readout text-[13.5px]" aria-label="What the assistant did">
      {items.map((it, i) => it.type === "thinking" ? (
        <li key={i}>
          <Collapsible>
            <CollapsibleTrigger className="group flex items-center gap-1.5 text-muted-foreground transition-colors hover:text-foreground">
              <ChevronRightIcon className="size-3.5 transition-transform group-data-[panel-open]:rotate-90" />
              Reasoning
            </CollapsibleTrigger>
            <CollapsibleContent>
              <p className="mt-1 max-h-48 overflow-y-auto font-sans text-[13px] leading-relaxed whitespace-pre-wrap text-muted-foreground">
                {it.text}
              </p>
            </CollapsibleContent>
          </Collapsible>
        </li>
      ) : (
        <li key={it.id} className="grid grid-cols-[14px_minmax(0,1fr)_auto] items-center gap-x-2">
          <span className="flex justify-center"><StepIcon status={it.status} /></span>
          <span className={cn("truncate", it.status === "running" && "shimmer text-sodium")}>{toolLabel(it.name)}</span>
          <span className="text-[12.5px] text-muted-foreground">{it.ms !== undefined ? seconds(it.ms) : ""}</span>
          {it.summary && (
            <span className="col-start-2 col-end-4 truncate text-[12.5px] text-muted-foreground" title={it.summary}>
              {it.summary}
            </span>
          )}
        </li>
      ))}
    </ol>
  )
}
