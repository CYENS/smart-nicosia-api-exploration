/* Small pieces the incident inbox, the inspector's Incidents section and the Layers row share:
   an incident's severity as a mark and a badge, one incident as a row, and the legend. */
import { DotLegend } from "@/components/topics/builtin"
import { Row } from "@/components/mobility/parts"
import { linkHover } from "@/lib/map/engine"
import {
  SEVERITY_COLOR, SEVERITY_WORD, STATUS_WORD, actionWords, when, type IncidentRow, type Severity,
} from "@/lib/watch/store"
import { cn } from "@/lib/utils"

// Severity as a bar beside the row: signal red for high, amber for medium, ink for low.
export function SeverityMark({ severity, className }: { severity: Severity; className?: string }) {
  return (
    <span
      aria-hidden="true"
      className={cn("w-1 shrink-0 self-stretch rounded-full", className)}
      style={{ background: SEVERITY_COLOR[severity] }}
    />
  )
}

const BADGE: Record<Severity, string> = {
  high: "border-signal/45 bg-signal/15 text-signal",
  medium: "border-amber/45 bg-amber/15 text-amber",
  low: "border-line bg-muted text-muted-foreground",
}

export function SeverityBadge({ severity }: { severity: Severity }) {
  return (
    <span className={cn("inline-flex h-5 items-center rounded-full border px-2 font-condensed text-[12px] font-semibold", BADGE[severity])}>
      {SEVERITY_WORD[severity]} severity
    </span>
  )
}

// What a row says under its title: its id, where, since when, and who saw to it.
export function rowMeta(r: IncidentRow) {
  const parts = [r.id, r.place, `since ${when(r.start)}`]
  if (r.status === "snoozed" && r.snoozed_until) parts.push(`snoozed until ${when(r.snoozed_until)}`)
  else if (r.status !== "open" && r.last_action) parts.push(actionWords(r.last_action).toLowerCase())
  else if (!r.active && r.status !== "resolved") parts.push("ended")
  return parts.join(", ")
}

export function IncidentRowView({ r, onSelect, current }: { r: IncidentRow; onSelect: () => void; current?: boolean }) {
  const label = `${r.id}: ${r.title}. ${SEVERITY_WORD[r.severity]} severity, ${STATUS_WORD[r.status].toLowerCase()}, ${r.place}, since ${when(r.start)}${r.active ? "" : ", ended"}`
  const hover = (on: boolean) => linkHover("id", on ? r.id : null, "inbox")
  return (
    <div onPointerEnter={() => hover(true)} onPointerLeave={() => hover(false)} onFocus={() => hover(true)} onBlur={() => hover(false)}>
      <Row rowKey={r.id} onSelect={onSelect} current={current} label={label} className="items-stretch gap-2.5 py-2">
        <SeverityMark severity={r.severity} />
        <span className="flex min-w-0 flex-1 flex-col gap-0.5">
          <span className={cn("text-[13.5px] leading-snug", r.status === "resolved" && "text-muted-foreground")}>{r.title}</span>
          <span className="font-condensed text-[12.5px] leading-snug text-muted-foreground">{rowMeta(r)}</span>
        </span>
        {r.status !== "open" && (
          <span className="shrink-0 self-start rounded-full border border-line px-1.5 font-condensed text-[11.5px] leading-[18px] text-muted-foreground">
            {STATUS_WORD[r.status]}
          </span>
        )}
      </Row>
    </div>
  )
}

export function SeverityLegend() {
  return (
    <DotLegend
      label="Incidents by severity"
      items={[[SEVERITY_COLOR.high, "High"], [SEVERITY_COLOR.medium, "Medium"], [SEVERITY_COLOR.low, "Low"]]}
    />
  )
}
