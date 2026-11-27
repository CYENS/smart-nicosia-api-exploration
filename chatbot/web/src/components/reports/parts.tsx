/* The road reports' pieces on the page: a kind's mark (the same shape the map draws), the
   legend of shapes, the rows that open a report, the Traffic card's section and the inspector's
   "As published" section with the operators' own words. */
import { ChevronRightIcon } from "lucide-react"
import { useEffect, useRef } from "react"

import { Lead, LeadSkeleton } from "@/components/topics/builtin"
import { Row, RowList } from "@/components/mobility/parts"
import { FromSources } from "@/components/sources/from-sources"
import { ItemContent, ItemTitle } from "@/components/ui/item"
import { useClock } from "@/lib/clock"
import { inspect } from "@/lib/inspector"
import { getMap } from "@/lib/map/engine"
import { REPORT_COLORS, REPORT_WORDS, drawMark, setReportsOn } from "@/lib/reports/map"
import { REPORT_KINDS, countWords, reportTitle, useReports, type ReportItem, type ReportKind } from "@/lib/reports/store"
import type { SectionProps } from "@/lib/registries/inspector-sections"
import { cn } from "@/lib/utils"

export const REPORT_SOURCES = ["waze_alerts", "traffic_events", "waze_traffic"]

// A kind's mark, as on the map. Decorative: the words beside it say the kind.
export function ReportMark({ kind, size = 18, className, faint }: { kind: ReportKind; size?: number; className?: string; faint?: boolean }) {
  const ref = useRef<HTMLCanvasElement>(null)
  useEffect(() => {
    const c = ref.current
    if (!c) return
    const dpr = window.devicePixelRatio || 1
    c.width = Math.round(size * dpr)
    c.height = Math.round(size * dpr)
    const ctx = c.getContext("2d")
    if (!ctx) return
    ctx.setTransform((size * dpr) / 48, 0, 0, (size * dpr) / 48, 0, 0)
    drawMark(ctx, kind, REPORT_COLORS[kind])
  }, [kind, size])
  return <canvas ref={ref} aria-hidden="true" className={cn("shrink-0", faint && "opacity-70", className)} style={{ width: size, height: size }} />
}

export function ReportLegend({ className, kinds = REPORT_KINDS }: { className?: string; kinds?: ReportKind[] }) {
  return (
    <div className={cn("flex flex-col gap-2", className)}>
      <ul aria-label="Road report marks" className="grid grid-cols-2 gap-x-3 gap-y-1.5 text-[12.5px] text-muted-foreground">
        {kinds.map((k) => (
          <li key={k} className="flex items-center gap-2">
            <ReportMark kind={k} size={16} />
            <span>{REPORT_WORDS[k]}</span>
          </li>
        ))}
      </ul>
      <p className="flex items-center gap-2 text-[12px] leading-snug text-muted-foreground">
        <span className="size-3.5 shrink-0 rounded-full border-2 border-sodium" aria-hidden="true" />
        Reported in the last 30 minutes. Fainter: reported over a week ago.
      </p>
    </div>
  )
}

// Open a report from a floating card: the camera centres it in the map the card leaves open
// (the card floats over the map's left part), once the inspector has taken its column.
function openBeside(r: ReportItem, card: HTMLElement | null) {
  const placed = Number.isFinite(r.lon) && Number.isFinite(r.lat)
  const beside = !!card && placed && card.getBoundingClientRect().width < window.innerWidth * 0.6 // not a phone's sheet
  inspect(placed ? { id: r.id, at: [r.lon, r.lat] } : { id: r.id }, { fly: !beside, focus: true })
  if (!beside || !card) return
  window.setTimeout(() => {
    const map = getMap()
    if (!map) return
    const box = card.getBoundingClientRect()
    const canvas = map.getCanvas().getBoundingClientRect()
    const dx = Math.max(0, (box.right - canvas.left + 12 - (map.getPadding().left ?? 0)) / 2)
    const opts = { center: [r.lon, r.lat] as [number, number], zoom: Math.max(map.getZoom(), 16.5), offset: [dx, 0] as [number, number], essential: true }
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) map.jumpTo({ center: opts.center, zoom: opts.zoom })
    else map.flyTo({ ...opts, duration: 1400 })
  }, 260)
}

// Reports that each open in the inspector: arrows move between them, Enter opens. Inside a
// floating card (`beside`), the camera keeps the report clear of the card.
export function ReportRows({ items, label, beside }: { items: ReportItem[]; label: string; beside?: React.RefObject<HTMLElement | null> }) {
  return (
    <RowList label={label}>
      {items.map((r) => (
        <Row key={r.id} rowKey={r.id} label={`${reportTitle(r)}, ${r.age ?? ""}`.replace(/, $/, "")}
             onSelect={() => openBeside(r, beside?.current?.closest("section[aria-labelledby='topic-card-title']") as HTMLElement | null ?? null)}>
          <ReportMark kind={r.kind} faint={r.old || r.status === "planned"} />
          <ItemContent className="min-w-0">
            <ItemTitle className="line-clamp-1 w-full">{reportTitle(r)}</ItemTitle>
            {r.age && <span className="text-[12.5px] text-muted-foreground">{r.age}</span>}
          </ItemContent>
          <ChevronRightIcon className="size-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
        </Row>
      ))}
    </RowList>
  )
}

// What the reports say, in a sentence, now or at the clock's moment; null while loading.
export function useReportsLine(): { lead: string; detail?: string; tone?: "signal" } | null {
  const status = useReports((s) => s.status)
  const body = useReports((s) => s.body)
  const error = useReports((s) => s.error)
  const past = useClock((s) => s.at !== null)
  if (status === "error" && !body) return { lead: "The road report feeds aren't answering. Try again in a minute.", detail: error ?? undefined, tone: "signal" }
  if (!body) return null
  const m = body.meta
  if (m.status === "unknown") return { lead: "The twin's memory holds no road reports for this moment.", detail: "It records them while a page or the watch reads them." }
  if (m.status === "unavailable") return { lead: "The road report feeds aren't answering. Try again in a minute.", detail: m.error ?? undefined, tone: "signal" }
  if (!m.total) return { lead: past ? "No road reports then." : "No road reports in Greater Nicosia right now." }
  const jam = m.newest_jam
  const jams = m.counts.jam ?? 0
  const recent = m.recent_words
  let lead: string
  if (jams && jam?.age) {
    lead = past ? `${jams} jam${jams === 1 ? " was" : "s were"} reported then, the latest ${jam.age.replace(" ago", "")} before.`
      : `Waze drivers report ${jams === 1 ? "a jam" : `${jams} jams`} now, the latest ${jam.age}.`
  } else if (recent && !past) {
    lead = `${recent.charAt(0).toUpperCase()}${recent.slice(1)} reported in the last 30 minutes.`
  } else {
    lead = `${countWords(m.counts, m.kinds).replace(/^./, (c) => c.toUpperCase())} ${past ? "reported then" : "reported now"}.`
  }
  const parts: string[] = []
  if (jams || recent) parts.push(`In all: ${countWords(m.counts, m.kinds)}`)
  if (m.long_closures) parts.push(`${m.long_closures} of the closures ${m.long_closures === 1 ? "is a long one" : "are long ones"}, reported over a week ago`)
  const stale = Object.values(m.feeds).find((f) => f.state === "stale" || (f.state === "unavailable" && f.name !== "Waze traffic"))
  if (stale && !past) parts.push(`${stale.name}: ${stale.state === "stale" ? stale.note ?? "from its last good read" : "not answering"}`)
  return { lead, detail: parts.join(". ") || undefined }
}

// -- the Traffic card's section ----------------------------------------------------------------------

export function TrafficReportsSection() {
  const status = useReports((s) => s.status)
  const body = useReports((s) => s.body)
  const line = useReportsLine()
  // The reports come onto the map while the card shows, and go back as they were after.
  useEffect(() => {
    const was = useReports.getState().on
    setReportsOn(true)
    return () => setReportsOn(was)
  }, [])
  const list = body?.meta.list ?? []
  const self = useRef<HTMLElement>(null)
  return (
    <section ref={self} aria-labelledby="traffic-reports-title" className="flex flex-col gap-2.5 border-t border-line pt-3">
      <h3 id="traffic-reports-title" className="text-[12.5px] text-muted-foreground">Road reports</h3>
      {line ? <Lead detail={line.detail} tone={line.tone}>{line.lead}</Lead> : status === "loading" || status === "idle" ? <LeadSkeleton /> : null}
      {list.length > 0 && <ReportRows items={list.slice(0, 4)} label="Road reports, newest and loudest first" beside={self} />}
      {body && body.meta.total > 0 && <ReportLegend kinds={REPORT_KINDS.filter((k) => body.meta.counts[k])} />}
      <FromSources ids={REPORT_SOURCES.slice(0, 2)} />
    </section>
  )
}

// -- the inspector's "As published" ---------------------------------------------------------------------

type ReportEntity = { report?: { text: string | null; text_en: string | null; source: string } }

export function AsPublished({ entity }: SectionProps) {
  const r = (entity as unknown as ReportEntity).report
  if (!r || r.source !== "traffic_events" || !r.text) return null
  const greek = /[Ͱ-Ͽἀ-῿]/.test(r.text)
  return (
    <section aria-label="As published" className="flex flex-col gap-2">
      <h3 className="text-[12.5px] text-muted-foreground">As published</h3>
      <blockquote lang={greek ? "el" : "en"} className="border-l-2 border-line pl-3 text-[13.5px] leading-snug">{r.text}</blockquote>
      {r.text_en && r.text_en !== r.text && (
        <blockquote lang="en" className="border-l-2 border-line pl-3 text-[13px] leading-snug text-muted-foreground">{r.text_en}</blockquote>
      )}
    </section>
  )
}
