/* The data sources panel: every source the twin reads, grouped, with its publisher, licence,
   what it gives the twin, how fresh it is and a link to read more. Opened from a "From ..."
   line, the map's credits, the conversation's Sources link or the search menu (Ctrl K). */
import { ExternalLinkIcon } from "lucide-react"
import { useEffect, useMemo, useRef } from "react"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Skeleton } from "@/components/ui/skeleton"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import {
  closeSources, loadSources, readAgo, useActiveSourceIds, useSources,
  type DataSource, type SourcePart, type SourceState, type SourceStatus,
} from "@/lib/sources"
import { cn } from "@/lib/utils"

const DOT: Record<SourceState, string> = {
  live: "bg-sodium shadow-[0_0_6px_1px] shadow-sodium/50",
  stale: "border border-sodium/70 bg-transparent",
  waiting: "border border-muted-foreground/70 bg-transparent",
  unavailable: "bg-signal",
}
const STATE_WORDS: Record<SourceState, string> = {
  live: "Live", stale: "Older copy", waiting: "Not read yet", unavailable: "Not answering",
}

function day(iso: string): string {
  const d = new Date(iso.length <= 10 ? `${iso}T12:00:00` : iso)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleDateString("en-GB", { timeZone: "Asia/Nicosia", weekday: "short", day: "numeric", month: "short" })
}

const upper = (s: string) => s.replace(/^./, (c) => c.toUpperCase())

function statusLine(st: SourceStatus): string | null {
  const ago = readAgo(st.as_of)
  switch (st.state) {
    case "live": return [ago && `Read ${ago}`, st.note && upper(st.note), st.holds_from && `Holds history from ${day(st.holds_from)}`]
      .filter(Boolean).join(". ") || null
    case "stale": return upper([st.note, ago && `last read ${ago}`].filter(Boolean).join(", "))
    case "waiting": return st.note ? `Not read yet: ${st.note}` : "Not read yet"
    case "unavailable": return st.note ? `Not answering: ${st.note}` : "Not answering"
    default: return null
  }
}

function Part({ p }: { p: SourcePart }) {
  return (
    <li className="flex items-center gap-2">
      <span className={cn("size-1.5 shrink-0 rounded-full", DOT[p.state])} aria-hidden="true" />
      <span>
        <span className="text-foreground">{p.name}</span>
        {p.count != null && <>, {p.count.toLocaleString()} buildings</>}
        {p.state === "live" ? (p.as_of ? `, read ${day(p.as_of)}` : "") : `, saved copy${p.as_of ? ` of ${day(p.as_of)}` : ""}`}
      </span>
    </li>
  )
}

function SourceRow({ s, onMap, focused, rowRef }: {
  s: DataSource; onMap: boolean; focused: boolean; rowRef?: React.Ref<HTMLLIElement>
}) {
  const st = s.status
  const state = st?.state ?? null
  const line = st ? statusLine(st) : null
  return (
    <li
      ref={rowRef}
      id={`source-${s.id}`}
      tabIndex={focused ? -1 : undefined}
      aria-current={focused ? "true" : undefined}
      className={cn(
        "flex scroll-my-3 items-start gap-3 rounded-lg border border-transparent px-3 py-2.5 outline-none",
        focused && "border-sodium/50 bg-sodium/[0.06]",
      )}
    >
      <span className="mt-[7px] flex size-2.5 shrink-0 items-center justify-center" aria-hidden="true">
        {state ? <span className={cn("size-2 rounded-full", DOT[state])} /> : <span className="size-1.5 rounded-full bg-muted-foreground/40" />}
      </span>
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <h4 className="text-[14px] leading-snug font-medium" lang={/[Ͱ-Ͽ]/.test(s.name) ? "el" : undefined}>{s.name}</h4>
          {onMap && <Badge variant="outline" className="border-sodium/40 font-normal text-sodium">On the map now</Badge>}
        </div>
        <p className="text-[13px] leading-snug text-muted-foreground">{s.gives}</p>
        <p className="text-[12px] leading-snug text-muted-foreground">
          <span lang={/[Ͱ-Ͽ]/.test(s.publisher) ? "el" : undefined}>{s.publisher}</span>
          {s.licence && (
            <>
              {", "}
              {s.licence_url ? (
                <a href={s.licence_url} target="_blank" rel="noreferrer" className="underline decoration-line underline-offset-3 hover:text-foreground">
                  {s.licence}
                </a>
              ) : s.licence}
            </>
          )}
        </p>
        {(line || state) && (
          <p className="font-condensed text-[12.5px] leading-snug">
            {state && <span className="text-foreground">{STATE_WORDS[state]}. </span>}
            <span className="text-muted-foreground">{line}</span>
          </p>
        )}
        {st?.parts && st.parts.length > 0 && (
          <ul className="flex flex-col gap-0.5 font-condensed text-[12.5px] leading-snug text-muted-foreground">
            {st.parts.map((p) => <Part key={p.name} p={p} />)}
          </ul>
        )}
        {s.refresh && <p className="text-[12px] leading-snug text-muted-foreground/80">{s.refresh}</p>}
      </div>
      {s.url && (
        <Tooltip>
          <TooltipTrigger
            render={
              <Button
                variant="ghost"
                size="icon-sm"
                className="shrink-0"
                aria-label={`Read more about ${s.name} at its publisher (opens a new tab)`}
                render={<a href={s.url} target="_blank" rel="noreferrer" />}
              />
            }
          >
            <ExternalLinkIcon />
          </TooltipTrigger>
          <TooltipContent>Read more at the publisher</TooltipContent>
        </Tooltip>
      )}
    </li>
  )
}

function Loading() {
  return (
    <div className="flex flex-col gap-4" aria-busy="true">
      {[0, 1, 2, 3].map((i) => (
        <div key={i} className="flex gap-3 px-3">
          <Skeleton className="mt-1 size-2.5 rounded-full" />
          <div className="flex flex-1 flex-col gap-1.5">
            <Skeleton className="h-4 w-40" />
            <Skeleton className="h-3.5 w-full" />
            <Skeleton className="h-3.5 w-2/3" />
          </div>
        </div>
      ))}
    </div>
  )
}

export function SourcesPanel() {
  const { open, focus, opener, list, groups, status } = useSources()
  const active = useActiveSourceIds()
  const focusRow = useRef<HTMLLIElement | null>(null)
  const listBox = useRef<HTMLDivElement | null>(null)
  const back = useMemo(() => ({ current: opener }), [opener])

  // Fresh while it is open: the feeds' times move on.
  useEffect(() => {
    if (!open) return
    const t = window.setInterval(() => void loadSources(), 30_000)
    return () => window.clearInterval(t)
  }, [open])
  // When the list arrives after the panel opened, bring the asked-for source into view.
  useEffect(() => {
    if (open && focus && focusRow.current) focusRow.current.scrollIntoView({ block: "nearest" })
  }, [open, focus, list.length])

  return (
    <Dialog open={open} onOpenChange={(o) => { if (!o) closeSources() }}>
      <DialogContent
        className="flex max-h-[min(720px,calc(100dvh-32px))] flex-col gap-0 p-0 sm:max-w-xl"
        initialFocus={focus ? focusRow : listBox}
        finalFocus={opener ? back : undefined}
      >
        <DialogHeader className="border-b border-line px-5 pt-5 pb-4">
          <DialogTitle>Where the data comes from</DialogTitle>
          <DialogDescription>
            Every reading, layer and chart in the twin comes from one of these. A lit dot is a live
            feed the twin has read lately.
          </DialogDescription>
        </DialogHeader>
        {/* The list is the first stop: the arrow and page keys scroll it, and no link's tooltip
            opens by itself. */}
        <div
          ref={listBox}
          tabIndex={0}
          role="region"
          aria-label="Sources"
          className="min-h-0 overflow-y-auto px-2 py-3 outline-none focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:ring-inset"
        >
          {status === "error" && !list.length ? (
            <p className="px-3 text-[13.5px] text-muted-foreground" role="alert">
              The list of sources didn't load. Check the connection to the twin's server, then open this again.
            </p>
          ) : !list.length ? <Loading /> : (
            <div className="flex flex-col gap-4">
              {groups.map((g) => {
                const items = list.filter((s) => s.group === g)
                if (!items.length) return null
                const id = `sources-${g.replace(/\W+/g, "-").toLowerCase()}`
                return (
                  <section key={g} aria-labelledby={id} className="flex flex-col gap-0.5">
                    <h3 id={id} className="px-3 text-[12.5px] text-muted-foreground">{g}</h3>
                    <ul className="flex flex-col">
                      {items.map((s) => (
                        <SourceRow
                          key={s.id}
                          s={s}
                          onMap={active.includes(s.id)}
                          focused={focus === s.id}
                          rowRef={focus === s.id ? focusRow : undefined}
                        />
                      ))}
                    </ul>
                  </section>
                )
              })}
            </div>
          )}
        </div>
      </DialogContent>
    </Dialog>
  )
}
