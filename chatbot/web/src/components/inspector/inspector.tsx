import { CheckIcon, CrosshairIcon, HistoryIcon, LinkIcon, MessageSquarePlusIcon, RotateCwIcon, SearchIcon, XIcon } from "lucide-react"
import { useEffect, useRef, useState } from "react"

import "@/components/inspector/sections"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Empty, EmptyContent, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty"
import { Kbd } from "@/components/ui/kbd"
import { Skeleton } from "@/components/ui/skeleton"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { askAbout } from "@/lib/chat"
import { linkTo } from "@/lib/deeplink"
import { ago, kindMeta, type Entity, type LocalThing } from "@/lib/entities"
import { useGeometry } from "@/lib/geometry"
import { typing, useNow } from "@/lib/hooks"
import { closeInspector, retryInspector, useInspector } from "@/lib/inspector"
import { GAP, STRIP_H, openSearch } from "@/lib/layout"
import { flyToThing } from "@/lib/map/engine"
import { sectionsFor } from "@/lib/registries/inspector-sections"
import { cn } from "@/lib/utils"

// Closing from the keyboard hands the keyboard to the map (arrow keys pan it), not to the page.
function close() {
  const inside = document.activeElement?.closest("[data-inspector]")
  closeInspector()
  if (inside) document.querySelector<HTMLElement>(".maplibregl-canvas")?.focus({ preventScroll: true })
}

const TONE: Record<string, string> = {
  alarm: "border-signal/40 text-signal", warn: "border-line text-foreground",
  live: "border-sodium/40 text-sodium", ok: "border-line text-foreground", muted: "border-line text-muted-foreground",
}

function Header({ icon: Icon, color, title, subtitle, titleRef }: {
  icon: React.ComponentType<{ className?: string }>; color?: string; title: string; subtitle?: string | null
  titleRef: React.RefObject<HTMLHeadingElement | null>
}) {
  return (
    <div className="flex items-start gap-3 pt-3.5 pr-2 pl-4">
      <span
        className="mt-0.5 flex size-10 shrink-0 items-center justify-center rounded-lg border bg-raised"
        style={{ borderColor: color ? `${color}66` : undefined }}
        aria-hidden="true"
      >
        <Icon className="size-[18px]" />
      </span>
      <div className="flex min-w-0 flex-1 flex-col gap-0.5 pt-0.5">
        <h2
          ref={titleRef}
          tabIndex={-1}
          id="inspector-title"
          className="line-clamp-2 text-[16px] leading-snug font-semibold outline-none"
          lang={/[Ͱ-Ͽ]/.test(title) ? "el" : undefined}
        >
          {title}
        </h2>
        {subtitle && <p className="truncate text-[13px] text-muted-foreground" lang={/[Ͱ-Ͽ]/.test(subtitle) ? "el" : undefined}>{subtitle}</p>}
      </div>
      <Tooltip>
        <TooltipTrigger render={<Button variant="ghost" size="icon-sm" aria-label="Close the inspector" onClick={close} />}>
          <XIcon />
        </TooltipTrigger>
        <TooltipContent>Close <Kbd>Esc</Kbd></TooltipContent>
      </Tooltip>
    </div>
  )
}

function Status({ entity }: { entity: Entity }) {
  const now = useNow()
  const s = entity.state
  if (!s) return null
  const age = s.as_of ? ago(s.as_of, now) : null
  return (
    <div className="flex flex-wrap items-center gap-2 px-4" aria-live="polite">
      <Badge variant="outline" className={cn("h-6 gap-1.5 px-2.5 text-[12.5px]", TONE[s.tone])}>
        <span className="size-2 rounded-full" style={{ background: s.color }} aria-hidden="true" />
        {s.label}
        {s.local && <span lang="el" className="font-normal opacity-80">{s.local}</span>}
      </Badge>
      {s.stale && entity.kind !== "street_light" && (
        <Badge variant="outline" className="h-6 px-2.5 text-[12.5px] text-muted-foreground">
          <HistoryIcon data-icon="inline-start" />
          Stale reading
        </Badge>
      )}
      {age && <span className="font-condensed text-[12.5px] text-muted-foreground">Last report {age}</span>}
    </div>
  )
}

function Actions({ entity }: { entity: Entity }) {
  const [copied, setCopied] = useState<"" | "ok" | "fail">("")
  useEffect(() => {
    if (!copied) return
    const t = window.setTimeout(() => setCopied(""), 2200)
    return () => window.clearTimeout(t)
  }, [copied])
  const copy = async () => {
    const url = linkTo(entity.id)
    try {
      await navigator.clipboard.writeText(url)
      setCopied("ok")
    } catch {
      setCopied("fail")
    }
  }
  return (
    <div className="flex shrink-0 flex-col gap-1.5 border-t border-line p-3">
      <div className="flex gap-2">
        <Button className="flex-1" onClick={() => askAbout({ id: entity.id, kind: entity.kind, name: entity.name })}>
          <MessageSquarePlusIcon data-icon="inline-start" />
          Ask about this
        </Button>
        <Tooltip>
          <TooltipTrigger render={<Button variant="outline" size="icon" aria-label="Fly to it" disabled={!entity.center} onClick={() => entity.center && flyToThing(entity.center, entity.kind)} />}>
            <CrosshairIcon />
          </TooltipTrigger>
          <TooltipContent>Fly to it <Kbd>F</Kbd></TooltipContent>
        </Tooltip>
        <Tooltip>
          <TooltipTrigger render={<Button variant="outline" size="icon" aria-label="Copy a link to it" onClick={copy} />}>
            {copied === "ok" ? <CheckIcon className="text-sodium" /> : <LinkIcon />}
          </TooltipTrigger>
          <TooltipContent>Copy a link to it</TooltipContent>
        </Tooltip>
      </div>
      <p className="min-h-[1lh] px-0.5 text-[12px] text-muted-foreground" aria-live="polite">
        {copied === "ok" ? "Link copied. It opens this view with the inspector on it." : copied === "fail" ? "Couldn't copy here. The address bar has the link." : ""}
      </p>
    </div>
  )
}

function Loading() {
  return (
    <div className="flex flex-col gap-4 p-4" aria-label="Loading">
      <div className="flex items-start gap-3">
        <Skeleton className="size-10 rounded-lg" />
        <div className="flex flex-1 flex-col gap-2 pt-1"><Skeleton className="h-4 w-3/4" /><Skeleton className="h-3 w-1/2" /></div>
      </div>
      <Skeleton className="h-6 w-28 rounded-full" />
      <Skeleton className="h-10 w-24" />
      <Skeleton className="h-16 w-full" />
      <div className="grid grid-cols-2 gap-3"><Skeleton className="h-9" /><Skeleton className="h-9" /><Skeleton className="h-9" /><Skeleton className="h-9" /></div>
    </div>
  )
}

function LocalBody({ thing }: { thing: LocalThing }) {
  return (
    <div className="flex flex-col gap-3 px-4 pb-4">
      {thing.lines.length > 0 && (
        <ul className="flex flex-col gap-1 text-[13.5px] leading-snug">
          {thing.lines.map((l, i) => <li key={i}>{l}</li>)}
        </ul>
      )}
      <p className="text-[12.5px] leading-snug text-muted-foreground">
        Drawn by an answer, so the twin keeps no record of its own for it. Ask in the conversation to change or explain it.
      </p>
      <Button variant="outline" size="sm" className="self-start" onClick={() => flyToThing(thing.center)}>
        <CrosshairIcon data-icon="inline-start" />
        Fly to it
      </Button>
    </div>
  )
}

// The inspector: whatever was clicked, searched or named, in one panel. It takes the map panel's
// place on the right (the map panel comes back when it closes) and is a sheet on a phone.
export function Inspector() {
  const { open, entity, local, note, status, error, target, series, seriesStatus, focusAt } = useInspector()
  const g = useGeometry()
  const titleRef = useRef<HTMLHeadingElement>(null)
  const panelRef = useRef<HTMLElement>(null)

  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (typing(e) || e.ctrlKey || e.metaKey || e.altKey) return
      if (e.key === "Escape") { e.preventDefault(); close() }
      const { entity: e2, local: l2 } = useInspector.getState()
      const c = e2?.center ?? l2?.center
      if (e.key.toLowerCase() === "f" && c) { e.preventDefault(); flyToThing(c, e2?.kind) }
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [open])

  // Opened from the keyboard (search, a chip, a relation): the keyboard follows it here.
  useEffect(() => {
    if (focusAt) (titleRef.current ?? panelRef.current)?.focus({ preventScroll: true })
  }, [focusAt, status])

  if (!open) return null

  const style = g.desktop
    ? { top: STRIP_H + GAP, right: GAP, width: g.inspectorW, maxHeight: `calc(100% - ${STRIP_H + 2 * GAP + g.layersBottom}px)` }
    : { left: 8, right: 8, bottom: 8, height: g.sheetH }

  let body: React.ReactNode
  if (local) {
    const meta = kindMeta(local.kind)
    body = (
      <>
        <Header icon={meta.icon} color={local.color} title={local.title} subtitle={meta.label} titleRef={titleRef} />
        <div className="min-h-0 overflow-y-auto pt-3">
          <LocalBody thing={local} />
        </div>
      </>
    )
  } else if (status === "loading" && !entity) {
    body = <Loading />
  } else if (status === "missing" || (status === "error" && !entity)) {
    const missing = status === "missing"
    body = (
      <>
        <div className="flex justify-end p-2">
          <Button variant="ghost" size="icon-sm" aria-label="Close the inspector" onClick={close}><XIcon /></Button>
        </div>
        {missing ? (
          <Empty className="pt-0">
            <EmptyHeader>
              <EmptyMedia variant="icon"><SearchIcon /></EmptyMedia>
              <EmptyTitle>Nothing in the twin is called {target?.id}</EmptyTitle>
              <EmptyDescription>It may have left the city's inventory, or the id is mistyped. Search by street, place or register file number instead.</EmptyDescription>
            </EmptyHeader>
            <EmptyContent>
              <Button variant="outline" size="sm" onClick={() => openSearch(target?.id ?? "")}><SearchIcon data-icon="inline-start" />Search everything</Button>
            </EmptyContent>
          </Empty>
        ) : (
          <div className="flex flex-col gap-3 px-4 pb-4">
            <Alert variant="destructive">
              <AlertTitle>Couldn't open {target?.id}</AlertTitle>
              <AlertDescription>{error}. The city's API may be slow; try again in a moment.</AlertDescription>
            </Alert>
            <Button variant="outline" size="sm" className="self-start" onClick={retryInspector}><RotateCwIcon data-icon="inline-start" />Try again</Button>
          </div>
        )}
      </>
    )
  } else if (entity) {
    const meta = kindMeta(entity.kind)
    body = (
      <>
        <Header icon={meta.icon} color={entity.state?.color} title={entity.name} subtitle={[entity.kind_label, entity.subtitle !== entity.kind_label ? entity.subtitle : null].filter(Boolean).join(", ")} titleRef={titleRef} />
        <div className="pt-2.5"><Status entity={entity} /></div>
        {note && (
          <section aria-label="On the map" className="mx-4 mt-3 rounded-lg border border-line bg-night/50 px-3 py-2">
            <h3 className="text-[12px] text-muted-foreground">On the map, as the answer drew it</h3>
            <ul className="mt-1 flex flex-col gap-0.5 text-[13px] leading-snug">
              {note.lines.map((l, i) => <li key={i}>{l}</li>)}
            </ul>
          </section>
        )}
        <div className="flex min-h-0 flex-col gap-5 overflow-y-auto px-4 pt-4 pb-4">
          {sectionsFor(entity).map(({ id, Component }) => (
            <Component key={id} entity={entity} series={series} seriesStatus={seriesStatus} />
          ))}
        </div>
        <Actions entity={entity} />
      </>
    )
  }

  return (
    <aside
      ref={panelRef}
      tabIndex={-1}
      data-inspector
      aria-labelledby={entity || local ? "inspector-title" : undefined}
      aria-label={entity || local ? undefined : "Inspector"}
      className={cn("panel-float absolute flex flex-col overflow-hidden rounded-xl outline-none", g.desktop ? "z-10" : "z-20")}
      style={style}
    >
      {body}
    </aside>
  )
}
