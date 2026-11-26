/* The incident inbox: what the twin noticed is wrong in the city, grouped into incidents, to see
   to. A list filtered by status and severity (arrow keys between rows, Enter opens one), and an
   incident's own view: what it is, the actions (acknowledge, snooze, resolve) with who did what,
   its devices as chips that open the inspector, the evidence over time and its place on the
   map. Under the list, the watches people saved. It takes the right column (a sheet on a phone);
   the inspector still opens over it. */
import {
  ArrowLeftIcon, BellRingIcon, CheckIcon, ClockIcon, MapIcon, MessageSquareTextIcon, RotateCcwIcon, SirenIcon,
  SunriseIcon, Trash2Icon, XIcon,
} from "lucide-react"
import { useEffect, useRef, type ReactNode } from "react"

import { IncidentRowView, SeverityBadge, SeverityLegend } from "@/components/watch/parts"
import { RowList } from "@/components/mobility/parts"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuGroup, DropdownMenuItem, DropdownMenuLabel, DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty"
import { Field, FieldContent, FieldDescription, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Kbd } from "@/components/ui/kbd"
import { Separator } from "@/components/ui/separator"
import { Skeleton } from "@/components/ui/skeleton"
import { Switch } from "@/components/ui/switch"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { send, useChat } from "@/lib/chat"
import { toWall } from "@/lib/clock"
import { kindMeta } from "@/lib/entities"
import { useGeometry } from "@/lib/geometry"
import { typing } from "@/lib/hooks"
import { inspect } from "@/lib/inspector"
import { GAP, STRIP_H, useLayout } from "@/lib/layout"
import { useRovingList } from "@/lib/roving"
import { cn } from "@/lib/utils"
import { leaveInbox, takeInboxFocus, wantInboxFocus } from "@/lib/watch/focus"
import { frame } from "@/lib/watch/map"
import {
  STATUS_WORD, act, actionWords, countsWords, deleteWatch, loadDetail, loadIncidents, selectIncident, setFilter, setName,
  useWatch, when, type ActionName, type IncidentDetail, type SeverityFilter, type StatusFilter,
} from "@/lib/watch/store"
import { setNotify, useNotify } from "@/lib/watch/stream"

// The device chip or row that opened the inspector: the keyboard goes back to it when the
// inspector closes (the panel draws nothing while the inspector has the column).
let returnTo: string | null = null

function Heading({ children, aside }: { children: ReactNode; aside?: ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-2">
      <h3 className="text-[13px] font-semibold">{children}</h3>
      {aside}
    </div>
  )
}

function Rows({ n = 5 }: { n?: number }) {
  return (
    <div className="flex flex-col gap-2" aria-label="Loading">
      {Array.from({ length: n }, (_, i) => <Skeleton key={i} className="h-11 w-full" />)}
    </div>
  )
}

// -- the filters -----------------------------------------------------------------------------------

const STATUSES: { id: StatusFilter; label: string }[] = [
  { id: "unresolved", label: "To see to" }, { id: "snoozed", label: "Snoozed" },
  { id: "resolved", label: "Resolved" }, { id: "all", label: "All" },
]
const SEVERITIES: { id: SeverityFilter | "any"; label: string }[] = [
  { id: "any", label: "Any severity" }, { id: "medium", label: "Medium and high" }, { id: "high", label: "High" },
]

const chip = cn(
  "h-7 rounded-full border-line px-2.5 text-[12.5px] font-medium text-foreground shadow-none",
  "hover:bg-raised aria-pressed:bg-sodium/[0.16] aria-pressed:inset-ring-1 aria-pressed:inset-ring-sodium/55 aria-pressed:hover:bg-sodium/[0.22]",
  "focus-visible:ring-3 focus-visible:ring-ring/60",
)

function Filters() {
  const status = useWatch((s) => s.status)
  const severity = useWatch((s) => s.severity)
  const counts = useWatch((s) => s.counts)
  const n = (id: StatusFilter) => (counts ? { unresolved: counts.unresolved - counts.snoozed, snoozed: counts.snoozed, resolved: null, all: null }[id] : null)
  return (
    <div className="flex flex-col gap-2">
      <ToggleGroup
        aria-label="Which incidents"
        value={[status]}
        onValueChange={(v) => { if (v[0]) setFilter({ status: v[0] as StatusFilter }) }}
        spacing={1}
        className="flex-wrap"
      >
        {STATUSES.map((s) => (
          <ToggleGroupItem key={s.id} value={s.id} variant="outline" className={chip}>
            {s.label}{n(s.id) ? <span className="font-readout tabular-nums text-muted-foreground">{n(s.id)}</span> : null}
          </ToggleGroupItem>
        ))}
      </ToggleGroup>
      <ToggleGroup
        aria-label="Least severity"
        value={[severity || "any"]}
        onValueChange={(v) => { if (v[0]) setFilter({ severity: (v[0] === "any" ? "" : v[0]) as SeverityFilter }) }}
        spacing={1}
        className="flex-wrap"
      >
        {SEVERITIES.map((s) => (
          <ToggleGroupItem key={s.id} value={s.id} variant="outline" className={chip}>{s.label}</ToggleGroupItem>
        ))}
      </ToggleGroup>
    </div>
  )
}

// -- the list --------------------------------------------------------------------------------------

function List() {
  const rows = useWatch((s) => s.rows)
  const status = useWatch((s) => s.rowsStatus)
  const error = useWatch((s) => s.rowsError)
  const filter = useWatch((s) => s.status)
  const severity = useWatch((s) => s.severity)
  if (status === "error" && !rows.length) {
    return (
      <Alert>
        <AlertTitle>Couldn't read the inbox</AlertTitle>
        <AlertDescription className="flex flex-col items-start gap-2">
          <span>{error}. The twin's memory may be busy; try again in a moment.</span>
          <Button size="sm" variant="outline" onClick={() => void loadIncidents()}><RotateCcwIcon data-icon="inline-start" />Try again</Button>
        </AlertDescription>
      </Alert>
    )
  }
  if (status !== "ready" && !rows.length) return <Rows />
  if (!rows.length) {
    const words = filter === "resolved" ? "Nothing resolved in the time the twin keeps."
      : filter === "snoozed" ? "Nothing is snoozed."
        : severity ? "Nothing at that severity. Try any severity."
          : "Nothing needs attention. The twin looks at the city every few minutes; what it finds, and the city's own alarms, appear here."
    return (
      <Empty className="gap-3 border border-dashed border-line p-5">
        <EmptyHeader>
          <EmptyMedia variant="icon"><CheckIcon /></EmptyMedia>
          <EmptyTitle className="text-[14px]">All clear</EmptyTitle>
          <EmptyDescription className="text-[12.5px]">{words}</EmptyDescription>
        </EmptyHeader>
        {filter !== "resolved" && (
          <Button size="sm" variant="outline" onClick={() => setFilter({ status: "resolved", severity: "" })}>Show the resolved</Button>
        )}
      </Empty>
    )
  }
  return (
    <RowList label="Incidents">
      {rows.map((r) => (
        <IncidentRowView key={r.id} r={r} onSelect={() => openFromList(r.id)} />
      ))}
    </RowList>
  )
}

let backTo: string | null = null // the row an incident was opened from

function openFromList(id: string) {
  backTo = id
  wantInboxFocus("detail")
  selectIncident(id)
}

// -- watches ---------------------------------------------------------------------------------------

function Watches() {
  const watches = useWatch((s) => s.watches)
  const status = useWatch((s) => s.watchesStatus)
  const notify = useNotify()
  const busy = useChat((s) => s.busy)
  const g = useGeometry()
  return (
    <section aria-labelledby="watches-title" className="flex flex-col gap-2.5">
      <Heading aside={<span className="text-[12px] text-muted-foreground">raised as toasts</span>}>
        <span id="watches-title" className="flex items-center gap-1.5"><BellRingIcon className="size-4 text-muted-foreground" aria-hidden="true" />Your watches</span>
      </Heading>
      {status === "loading" && <Rows n={2} />}
      {status === "error" && <p className="text-[13px] text-muted-foreground">Couldn't read the watches. They come back with the next change.</p>}
      {status !== "loading" && !watches.length && (
        <div className="flex flex-col items-start gap-1.5 text-[13px] text-muted-foreground">
          <p>No watches yet. Ask the assistant, for example "Tell me when bin YW1394 passes 90%", and press Save on its card.</p>
          <Button variant="ghost" size="sm" disabled={busy} className="h-auto py-1.5 text-left whitespace-normal" onClick={() => {
            useLayout.setState({ chatOpen: true })
            if (!g.desktop) leaveInbox()
            send("Tell me when any new high-severity incident starts")
          }}>
            <MessageSquareTextIcon data-icon="inline-start" />Tell me when any new high-severity incident starts
          </Button>
        </div>
      )}
      {!!watches.length && (
        <ul className="flex flex-col gap-1" aria-label="Your watches">
          {watches.map((w) => (
            <li key={w.id} className="flex items-start gap-2 rounded-lg px-1.5 py-1.5 hover:bg-muted/60">
              <span className="min-w-0 flex-1">
                <span className="block text-[13px] leading-snug">{w.label}</span>
                <span className="block font-condensed text-[12px] text-muted-foreground">
                  {w.true_now.length ? `True now for ${w.true_now.slice(0, 3).join(", ")}` : w.last_fired ? `Last raised ${when(w.last_fired)}` : "Not raised yet"}
                  {w.by ? `, set by ${w.by}` : ""}
                </span>
              </span>
              <Tooltip>
                <TooltipTrigger render={<Button variant="ghost" size="icon-sm" aria-label={`Delete the watch: ${w.label}`} onClick={() => void deleteWatch(w.id)} />}>
                  <Trash2Icon />
                </TooltipTrigger>
                <TooltipContent>Delete the watch</TooltipContent>
              </Tooltip>
            </li>
          ))}
        </ul>
      )}
      {notify.supported && (
        <Field orientation="horizontal" className="mt-1">
          <FieldContent>
            <FieldLabel htmlFor="watch-notify" className="text-[13px] font-normal">Also as browser notifications</FieldLabel>
            <FieldDescription className="text-[12px]">
              {notify.denied ? "The browser blocks them for this site; allow them in its site settings." : "While this page is in the background"}
            </FieldDescription>
          </FieldContent>
          <Switch id="watch-notify" checked={notify.on} disabled={notify.denied} onCheckedChange={(on) => void setNotify(on)} />
        </Field>
      )}
    </section>
  )
}

// -- one incident ----------------------------------------------------------------------------------

function wallIn(ms: number) {
  return toWall(Date.now() + ms)
}

function tomorrowAt8() {
  return `${toWall(Date.now() + 24 * 3600e3).slice(0, 10)}T08:00`
}

// Whether someone acknowledged it since it last opened (a snoozed incident shows as snoozed).
function acknowledged(d: IncidentDetail) {
  let on = false
  for (const a of d.actions) {
    if (a.action === "acknowledged") on = true
    else if (a.action === "reopened" || a.action === "resolved") on = false
  }
  return on || d.status === "acknowledged"
}

function Actions({ d }: { d: IncidentDetail }) {
  const acting = useWatch((s) => s.acting)
  const error = useWatch((s) => s.actError)
  const name = useWatch((s) => s.name)
  const busy = acting !== null
  const box = useRef<HTMLElement>(null)
  // The button pressed goes away with the change (Acknowledge once acknowledged): the keyboard
  // moves on to the first action left rather than falling to the page.
  const run = (a: ActionName, until?: string) => {
    void act(d.id, a, { until }).then(() => requestAnimationFrame(() => {
      const a = document.activeElement
      if (!a || a === document.body || !a.isConnected) box.current?.querySelector<HTMLElement>("button:not([disabled])")?.focus({ preventScroll: true })
    }))
  }
  const resolved = d.status === "resolved"
  return (
    <section ref={box} aria-label="What to do about it" className="flex flex-col gap-2.5">
      <div className="flex flex-wrap gap-1.5">
        {!resolved && !acknowledged(d) && (
          <Button size="sm" disabled={busy} onClick={() => run("acknowledge")}>
            <CheckIcon data-icon="inline-start" />{acting === "acknowledge" ? "Acknowledging" : "Acknowledge"}
          </Button>
        )}
        {!resolved && (d.status === "snoozed" ? (
          <Button size="sm" variant="outline" disabled={busy} onClick={() => run("unsnooze")}>
            <SunriseIcon data-icon="inline-start" />Wake it now
          </Button>
        ) : (
          <DropdownMenu>
            <DropdownMenuTrigger render={<Button size="sm" variant="outline" disabled={busy} className="data-popup-open:bg-accent" />}>
              <ClockIcon data-icon="inline-start" />Snooze
            </DropdownMenuTrigger>
            <DropdownMenuContent align="start" className="w-52">
              <DropdownMenuGroup>
                <DropdownMenuLabel>Snooze it until</DropdownMenuLabel>
                <DropdownMenuItem onClick={() => run("snooze", wallIn(3600e3))}>An hour from now</DropdownMenuItem>
                <DropdownMenuItem onClick={() => run("snooze", wallIn(4 * 3600e3))}>Four hours from now</DropdownMenuItem>
                <DropdownMenuItem onClick={() => run("snooze", tomorrowAt8())}>Tomorrow at 08:00</DropdownMenuItem>
                <DropdownMenuItem onClick={() => run("snooze", wallIn(7 * 24 * 3600e3 - 60e3))}>A week from now</DropdownMenuItem>
              </DropdownMenuGroup>
            </DropdownMenuContent>
          </DropdownMenu>
        ))}
        {resolved ? (
          <Button size="sm" variant="outline" disabled={busy} onClick={() => run("reopen")}>
            <RotateCcwIcon data-icon="inline-start" />Reopen
          </Button>
        ) : (
          <Button size="sm" variant="outline" disabled={busy} onClick={() => run("resolve")}>
            <CheckIcon data-icon="inline-start" />{acting === "resolve" ? "Resolving" : "Resolve"}
          </Button>
        )}
      </div>
      {error && <p role="alert" className="text-[12.5px] text-signal">Couldn't do that: {error}. Try again.</p>}
      <Field className="gap-1">
        <FieldLabel htmlFor="watch-name" className="text-[12.5px] font-normal text-muted-foreground">Your name in its history (optional)</FieldLabel>
        <Input id="watch-name" value={name} maxLength={60} placeholder="For example, Maria at the control room" autoComplete="name"
          className="h-8 text-[13px]" onChange={(e) => setName(e.target.value)} />
      </Field>
    </section>
  )
}

function Devices({ d }: { d: IncidentDetail }) {
  const { ref, onKeyDown, onFocus } = useRovingList<HTMLDivElement>({ orientation: "both" })
  return (
    <section aria-labelledby="incident-devices" className="flex flex-col gap-2">
      <Heading aside={<span className="text-[12px] text-muted-foreground">open one in the inspector</span>}>
        <span id="incident-devices">{d.members.length === 1 ? "The device" : `${d.members.length} devices`}</span>
      </Heading>
      <div ref={ref} role="list" aria-labelledby="incident-devices" onKeyDown={onKeyDown} onFocus={onFocus} className="flex flex-wrap gap-1.5">
        {d.members.map((m) => {
          const Icon = kindMeta(m.kind).icon
          return (
            <div role="listitem" key={m.id}>
              <Button
                variant="outline"
                size="sm"
                data-row=""
                data-key={m.id}
                className="h-auto max-w-full gap-1.5 rounded-full py-1 text-left hover:bg-raised active:bg-accent"
                aria-label={`${m.label} (${m.id}): ${m.what}${m.active ? "" : ", ended"}`}
                onClick={() => {
                  returnTo = m.id
                  inspect({ id: m.id, at: m.lon !== null && m.lat !== null ? [m.lon, m.lat] : undefined }, { fly: true, focus: true })
                }}
              >
                <Icon data-icon="inline-start" className={m.active ? "text-sodium" : "text-muted-foreground"} />
                <span className="font-readout font-semibold tabular-nums">{m.id}</span>
                <span className="truncate font-normal text-muted-foreground">{m.what}</span>
              </Button>
            </div>
          )
        })}
      </div>
    </section>
  )
}

function Timeline({ d }: { d: IncidentDetail }) {
  const items = d.evidence.slice(-40)
  return (
    <section aria-labelledby="incident-evidence" className="flex flex-col gap-2">
      <Heading aside={<span className="text-[12px] text-muted-foreground">{d.evidence.length > items.length ? `the last ${items.length} of ${d.evidence.length}` : "oldest first"}</span>}>
        <span id="incident-evidence">Evidence</span>
      </Heading>
      <ol aria-labelledby="incident-evidence" className="relative ml-1.5 flex flex-col gap-2.5 border-l border-line pl-4">
        {items.map((e, i) => (
          <li key={`${e.time}-${i}`} className="relative text-[13px] leading-snug">
            <span
              aria-hidden="true"
              className={cn("absolute top-1.5 -left-[21px] size-2 rounded-full ring-2 ring-console", e.kind === "event" ? "bg-signal" : e.kind === "detection" ? "bg-amber" : "bg-muted-foreground")}
            />
            <span className="block font-condensed text-[12px] text-muted-foreground tabular-nums">
              {when(e.time.slice(0, 16))}{e.device ? `, ${e.device}` : ""}
            </span>
            <span>{e.text}</span>
          </li>
        ))}
      </ol>
    </section>
  )
}

function History({ d }: { d: IncidentDetail }) {
  return (
    <section aria-labelledby="incident-history" className="flex flex-col gap-2">
      <Heading><span id="incident-history">Who did what</span></Heading>
      {d.actions.length ? (
        <ol aria-labelledby="incident-history" className="flex flex-col gap-1.5 text-[13px]">
          {[...d.actions].reverse().map((a) => (
            <li key={`${a.t_ms}-${a.action}`} className="leading-snug">
              <span>{actionWords(a)}</span>
              <span className="text-muted-foreground">, {when(a.at)}{a.note ? `: ${a.note}` : ""}</span>
            </li>
          ))}
        </ol>
      ) : (
        <p className="text-[13px] text-muted-foreground">Nobody has acted on it yet. The twin opened it {when(d.created)}.</p>
      )}
    </section>
  )
}

function Detail() {
  const d = useWatch((s) => s.detail)
  const status = useWatch((s) => s.detailStatus)
  const error = useWatch((s) => s.detailError)
  const selected = useWatch((s) => s.selected)
  const busy = useChat((s) => s.busy)
  const g = useGeometry()
  const head = useRef<HTMLHeadingElement>(null)
  const shown = d && d.id === selected ? d.id : null
  // Opened by keyboard (a row, a chip in an answer), the keyboard goes to its heading.
  useEffect(() => {
    if (shown && takeInboxFocus("detail")) head.current?.focus({ preventScroll: true })
  }, [shown])

  const back = (
    <Button variant="ghost" size="sm" className="-ml-1.5 self-start" onClick={backToList}>
      <ArrowLeftIcon data-icon="inline-start" />All incidents
    </Button>
  )
  if (status === "missing") {
    return (
      <div className="flex flex-col gap-3">
        {back}
        <Alert>
          <AlertTitle>No incident {selected}</AlertTitle>
          <AlertDescription>It may have been merged into another one, or is older than the twin keeps.</AlertDescription>
        </Alert>
      </div>
    )
  }
  if (status === "error") {
    return (
      <div className="flex flex-col gap-3">
        {back}
        <Alert>
          <AlertTitle>Couldn't read {selected}</AlertTitle>
          <AlertDescription className="flex flex-col items-start gap-2">
            <span>{error}</span>
            <Button size="sm" variant="outline" onClick={() => selected && void loadDetail(selected)}><RotateCcwIcon data-icon="inline-start" />Try again</Button>
          </AlertDescription>
        </Alert>
      </div>
    )
  }
  if (!d || d.id !== selected) {
    return (
      <div className="flex flex-col gap-3" aria-label="Loading">
        {back}
        <Skeleton className="h-6 w-4/5" /><Skeleton className="h-4 w-1/2" /><Skeleton className="h-16 w-full" /><Skeleton className="h-24 w-full" />
      </div>
    )
  }
  return (
    <article aria-labelledby="incident-title" className="flex flex-col gap-4">
      <div className="flex flex-col gap-2">
        {back}
        <h3 ref={head} id="incident-title" tabIndex={-1} data-incident-head="" className="text-[16px] leading-snug font-semibold outline-none focus-visible:ring-2 focus-visible:ring-ring/60 rounded-sm">
          {d.title}
        </h3>
        <div className="flex flex-wrap items-center gap-1.5">
          <SeverityBadge severity={d.severity} />
          <span className="inline-flex h-5 items-center rounded-full border border-line px-2 font-condensed text-[12px]" aria-live="polite">{STATUS_WORD[d.status]}{d.status === "snoozed" && d.snoozed_until ? ` until ${when(d.snoozed_until)}` : ""}</span>
          <span className="inline-flex h-5 items-center rounded-full border border-line px-2 font-condensed text-[12px] text-muted-foreground">{d.category}</span>
          {d.active
            ? <span className="inline-flex h-5 items-center gap-1 rounded-full border border-sodium/45 bg-sodium/10 px-2 font-condensed text-[12px] text-sodium">Still happening</span>
            : <span className="inline-flex h-5 items-center rounded-full border border-line px-2 font-condensed text-[12px] text-muted-foreground">Ended</span>}
        </div>
        <p className="font-condensed text-[13px] text-muted-foreground">
          {d.id}, {d.place}. Since {when(d.start)}, last seen {when(d.last_seen)}.
        </p>
        <p className="text-[13.5px] leading-snug">{d.summary}</p>
        <div className="flex flex-wrap gap-1.5">
          <Button variant="ghost" size="sm" className="-ml-1.5" onClick={() => frame(d)}><MapIcon data-icon="inline-start" />Show on the map</Button>
          <Button variant="ghost" size="sm" disabled={busy} onClick={() => {
            useLayout.setState({ chatOpen: true })
            if (!g.desktop) leaveInbox()
            send(`Why did ${d.id} happen, and is it only here?`)
          }}>
            <MessageSquareTextIcon data-icon="inline-start" />Ask why
          </Button>
        </div>
      </div>
      <Actions d={d} />
      <Separator />
      <Devices d={d} />
      <Separator />
      <Timeline d={d} />
      <Separator />
      <History d={d} />
    </article>
  )
}

function backToList() {
  const key = useWatch.getState().selected ?? backTo
  selectIncident(null)
  // The keyboard back to the row it came from (rendered on the next frame).
  requestAnimationFrame(() => {
    const row = key ? document.querySelector<HTMLElement>(`[data-workspace] [data-row][data-key="${CSS.escape(key)}"]`) : null
    ;(row ?? document.getElementById("incidents-title"))?.focus({ preventScroll: true })
  })
}

// -- the panel -------------------------------------------------------------------------------------

export function IncidentInbox() {
  const g = useGeometry()
  const selected = useWatch((s) => s.selected)
  const counts = useWatch((s) => s.counts)
  const title = useRef<HTMLHeadingElement>(null)

  // Opened from the Layers sheet, Ctrl K or Alt 4, the keyboard comes in at the title (or at the
  // incident's heading when one was opened by keyboard).
  useEffect(() => {
    if (!g.inspecting && takeInboxFocus("title")) title.current?.focus({ preventScroll: true })
  }, [g.inspecting, selected])
  // Back from the inspector, the keyboard goes to the chip or row that opened it.
  useEffect(() => {
    if (g.inspecting || !returnTo) return
    const key = returnTo
    returnTo = null
    requestAnimationFrame(() => document.querySelector<HTMLElement>(`[data-workspace] [data-row][data-key="${CSS.escape(key)}"]`)?.focus({ preventScroll: true }))
  }, [g.inspecting])
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || typing(e) || g.inspecting || !document.activeElement?.closest("[data-workspace]")) return
      e.preventDefault()
      if (useWatch.getState().selected) backToList()
      else leaveInbox()
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [g.inspecting])

  if (g.inspecting) return null // the inspector takes the column (or the sheet) while open
  const style = g.desktop
    ? { top: STRIP_H + GAP, right: GAP, width: g.workspaceW, maxHeight: `calc(100% - ${STRIP_H + 2 * GAP + g.layersBottom}px)` }
    : { left: 8, right: 8, bottom: 8, height: g.sheetH }

  return (
    <aside data-workspace aria-labelledby="incidents-title" className={cn("panel-float absolute flex flex-col overflow-hidden rounded-xl", g.desktop ? "z-10" : "z-20")} style={style}>
      <div className="flex h-14 shrink-0 items-center gap-3 border-b border-line pr-2 pl-4">
        <SirenIcon className="size-[18px] text-sodium" aria-hidden="true" />
        <div className="min-w-0 flex-1 leading-tight">
          <h2 ref={title} tabIndex={-1} id="incidents-title" className="text-[14.5px] font-semibold outline-none">Incidents</h2>
          <p className="truncate text-[12.5px] text-muted-foreground" aria-live="polite">{countsWords(counts) ?? "What the twin noticed is wrong"}</p>
        </div>
        <Tooltip>
          <TooltipTrigger render={<Button variant="ghost" size="icon-sm" aria-label="Close the incidents" onClick={leaveInbox} />}>
            <XIcon />
          </TooltipTrigger>
          <TooltipContent>Close <Kbd>Esc</Kbd></TooltipContent>
        </Tooltip>
      </div>
      <div className="flex min-h-0 flex-col gap-4 overflow-y-auto p-4">
        {selected ? <Detail /> : (
          <>
            <Filters />
            <List />
            <SeverityLegend />
            <Separator />
            <Watches />
          </>
        )}
      </div>
    </aside>
  )
}
