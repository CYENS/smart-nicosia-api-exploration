/* The counting cameras walkthrough: one camera at a time, the map flown to it with a pin, its
   counting lines sketched where they are drawn on its picture, what each line counted yesterday
   (by kind, by way across, hour by hour), and the line's role in the counting-lines register to
   confirm: which street it watches, which road users it is for, which way, and whether it may
   be added into the camera's total (chatbot/traffic_lines.py). Saving needs the admin password.
   It takes the map panel's place on the right (a sheet on a phone), like the other
   workspaces. */
import { CctvIcon, ChevronLeftIcon, ChevronRightIcon, CircleCheckIcon, MapPinOffIcon, XIcon } from "lucide-react"
import { useEffect, useRef } from "react"
import { toast } from "sonner"

import { UnlockButton } from "@/components/admin/unlock"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Field, FieldDescription, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Kbd } from "@/components/ui/kbd"
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Separator } from "@/components/ui/separator"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { useGeometry } from "@/lib/geometry"
import { typing } from "@/lib/hooks"
import { GAP, STRIP_H } from "@/lib/layout"
import { flyToThing, getCamera, onCameraMoved, setSelection } from "@/lib/map/engine"
import { openWorkspace } from "@/lib/registries/workspaces"
import {
  cameraById, editLine, lineValue, loadReview, saveCamera, showCamera, step, useEdited, useReview,
  type AddsUp, type Direction, type Measures, type ReviewCamera, type ReviewLine,
} from "@/lib/review/store"
import { cn } from "@/lib/utils"

const COLOR: Record<Measures, string> = { people: "var(--chart-2)", vehicles: "var(--chart-3)", both: "var(--chart-4)" }
const MEASURE_WORDS: Record<Measures, string> = { people: "People", vehicles: "Vehicles", both: "Both" }
const DIRECTION_WORDS: Record<Direction, string> = { in: "In", out: "Out", both: "Both ways", "one way": "One way (unnamed)" }
const ADDS_WORDS: Record<AddsUp, string> = {
  yes: "Yes, add it into the camera's total",
  no: "No, it overlaps another line",
  review: "Not decided yet",
}
const n = (v: number) => v.toLocaleString("en-GB")

function leave() {
  setSelection(null)
  openWorkspace("live")
  document.querySelector<HTMLElement>(".maplibregl-canvas")?.focus({ preventScroll: true })
}

// Where the lines are drawn on the camera's picture: 16:9, ends as fractions of it.
function Sketch({ cam }: { cam: ReviewCamera }) {
  return (
    <figure className="flex flex-col gap-1.5">
      <svg viewBox="0 0 160 90" role="img" aria-label={`Where ${cam.lines.length === 1 ? "the line is" : `the ${cam.lines.length} lines are`} drawn on the camera's picture`}
        className="w-full rounded-md border border-line bg-night">
        <rect x="0" y="0" width="160" height="90" fill="none" />
        {cam.lines.map((line, i) => {
          const [x1, y1, x2, y2] = line.ends
          if ([x1, y1, x2, y2].some((v) => v === null)) return null
          const v = lineValue(cam, line)
          const ax = x1! * 160, ay = y1! * 90, bx = x2! * 160, by = y2! * 90
          return (
            <g key={line.line_name}>
              <line x1={ax} y1={ay} x2={bx} y2={by} stroke={COLOR[v.measures]} strokeWidth="1.6"
                strokeDasharray={v.adds_up === "yes" ? undefined : "3 2"} strokeLinecap="round" />
              <circle cx={(ax + bx) / 2} cy={(ay + by) / 2} r="4.2" fill="var(--console)" stroke={COLOR[v.measures]} strokeWidth="0.8" />
              <text x={(ax + bx) / 2} y={(ay + by) / 2 + 2} textAnchor="middle" fontSize="5.5" fill="var(--foreground)">{i + 1}</text>
            </g>
          )
        })}
      </svg>
      <figcaption className="text-xs text-muted-foreground">
        Where the lines are drawn on the camera's picture. Dashed lines aren't added into totals.
        The picture itself stays on the city's private network.
      </figcaption>
    </figure>
  )
}

// What one line counted yesterday, hour by hour, as small bars.
function Hours({ values, hours, color }: { values: number[]; hours: string[]; color: string }) {
  const max = Math.max(1, ...values)
  const busiest = values.indexOf(Math.max(...values))
  return (
    <div className="flex flex-col gap-1">
      <svg viewBox={`0 0 ${values.length * 4} 20`} preserveAspectRatio="none" aria-hidden="true" className="h-6 w-full">
        {values.map((v, i) => (
          <rect key={i} x={i * 4 + 0.5} y={20 - (v / max) * 20} width="3" height={(v / max) * 20} fill={color} opacity={i === busiest ? 1 : 0.65} />
        ))}
      </svg>
      <p className="text-xs text-muted-foreground">Busiest hour yesterday: {hours[busiest]}</p>
    </div>
  )
}

function LineCard({ cam, line, index, hours, locked }: {
  cam: ReviewCamera; line: ReviewLine; index: number; hours: string[]; locked: boolean
}) {
  useReview((s) => s.drafts[cam.analytics_id]?.[line.line_name]) // redraw on edits
  const v = lineValue(cam, line)
  const id = `rl-${cam.analytics_id}-${index}`
  const y = line.yesterday
  const kinds = y ? Object.entries(y.kinds).filter(([, c]) => c > 0).sort((a, b) => b[1] - a[1]) : []
  return (
    <section aria-labelledby={`${id}-name`} className="flex flex-col gap-3 rounded-lg border border-line p-3">
      <div className="flex items-start gap-2">
        <span className="mt-0.5 flex size-5 shrink-0 items-center justify-center rounded-full border text-xs font-medium" style={{ borderColor: COLOR[v.measures] }}>
          {index + 1}
        </span>
        <div className="min-w-0 flex-1">
          <h4 id={`${id}-name`} className="text-sm font-medium break-words">{line.line_name}</h4>
          <p className="text-xs text-muted-foreground">
            {line.setting === 0 ? "Counts both ways across" : line.setting ? "Counts one way across only" : "Its way across isn't known"}
            {line.types.length ? `; set to count ${line.types.map((t) => ["people", "bikes", "cars", "motorcycles", "buses", "trucks"][t]).join(", ")}` : ""}.
          </p>
        </div>
        {line.reviewed ? <Badge variant="outline">Confirmed {line.reviewed}</Badge> : <Badge variant="secondary">Suggested</Badge>}
      </div>

      {y ? (
        <div className="flex flex-col gap-1.5 text-sm">
          <p>
            Yesterday: {kinds.length ? kinds.map(([k, c]) => `${n(c)} ${k}`).join(", ") : "nothing counted"}.
          </p>
          {line.setting === 0 && (y.way_1 || y.way_2) ? (
            <p className="text-xs text-muted-foreground">{n(y.way_1)} crossed one way and {n(y.way_2)} the other.</p>
          ) : null}
          <Hours values={y.hours} hours={hours} color={COLOR[v.measures]} />
        </div>
      ) : (
        <p className="text-sm text-muted-foreground">Nothing counted on this line yesterday.</p>
      )}

      <Field>
        <FieldLabel htmlFor={`${id}-street`}>Street it watches</FieldLabel>
        <Input id={`${id}-street`} value={v.street} disabled={locked} maxLength={120}
          onChange={(e) => editLine(cam, line, { street: e.target.value })} />
      </Field>
      <Field>
        <FieldLabel id={`${id}-measures`}>What it counts</FieldLabel>
        <ToggleGroup aria-labelledby={`${id}-measures`} variant="outline" size="sm" spacing={0} className="w-full"
          value={[v.measures]} disabled={locked}
          onValueChange={(x) => { if (x[0]) editLine(cam, line, { measures: x[0] as Measures }) }}>
          {(["people", "vehicles", "both"] as Measures[]).map((m) => (
            <ToggleGroupItem key={m} value={m} className="flex-1">{MEASURE_WORDS[m]}</ToggleGroupItem>
          ))}
        </ToggleGroup>
        <FieldDescription>Only these road users are taken from this line.</FieldDescription>
      </Field>
      <div className="grid gap-3 sm:grid-cols-2">
        <Field>
          <FieldLabel htmlFor={`${id}-direction`}>Which way</FieldLabel>
          <Select value={v.direction} disabled={locked} onValueChange={(x) => editLine(cam, line, { direction: x as Direction })}>
            <SelectTrigger id={`${id}-direction`} size="sm" className="w-full"><SelectValue>{DIRECTION_WORDS[v.direction]}</SelectValue></SelectTrigger>
            <SelectContent>
              <SelectGroup>
                {(Object.keys(DIRECTION_WORDS) as Direction[]).map((d) => <SelectItem key={d} value={d}>{DIRECTION_WORDS[d]}</SelectItem>)}
              </SelectGroup>
            </SelectContent>
          </Select>
        </Field>
        <Field>
          <FieldLabel htmlFor={`${id}-adds`}>Add into the total?</FieldLabel>
          <Select value={v.adds_up} disabled={locked} onValueChange={(x) => editLine(cam, line, { adds_up: x as AddsUp })}>
            <SelectTrigger id={`${id}-adds`} size="sm" className="w-full">
              <SelectValue>{{ yes: "Yes", no: "No, it overlaps", review: "Not decided" }[v.adds_up]}</SelectValue>
            </SelectTrigger>
            <SelectContent>
              <SelectGroup>
                {(Object.keys(ADDS_WORDS) as AddsUp[]).map((a) => <SelectItem key={a} value={a}>{ADDS_WORDS[a]}</SelectItem>)}
              </SelectGroup>
            </SelectContent>
          </Select>
        </Field>
      </div>
      <Field>
        <FieldLabel htmlFor={`${id}-note`}>Note (optional)</FieldLabel>
        <Input id={`${id}-note`} value={v.note} disabled={locked} maxLength={300} placeholder="What you saw, for whoever looks next"
          onChange={(e) => editLine(cam, line, { note: e.target.value })} />
      </Field>
    </section>
  )
}

function Position({ cam }: { cam: ReviewCamera }) {
  if (cam.lat === null || cam.lon === null) {
    return (
      <Alert>
        <MapPinOffIcon />
        <AlertTitle>Not on the map yet</AlertTitle>
        <AlertDescription>
          This camera has no position. Add one to data/camera_locations.csv (analytics id {cam.analytics_id}).
        </AlertDescription>
      </Alert>
    )
  }
  return (
    <p className="text-xs text-muted-foreground">
      {cam.location_source === "device" ? "Placed by its own camera device on the city platform." : "Placed by hand in data/camera_locations.csv."}{" "}
      Check that the pin stands on the street the lines are named after.
    </p>
  )
}

export function CameraReview() {
  const g = useGeometry()
  const { data, status, error, order, current, saving } = useReview()
  const title = useRef<HTMLHeadingElement>(null)
  const cam = data?.cameras.find((c) => c.analytics_id === current)
  const edited = useEdited(current)
  const locked = !data?.admin.unlocked

  useEffect(() => { void loadReview() }, [])
  useEffect(() => { title.current?.focus({ preventScroll: true }) }, [])
  // The map follows the camera shown: a pin on it, flown to (once the map exists). On a fresh
  // page the twin's own opening move can land after that flight, so for a few seconds a move
  // that ends away from the camera is followed by one more flight back to it.
  useEffect(() => {
    if (!cam || cam.lat === null || cam.lon === null) { setSelection(null); return }
    const center: [number, number] = [cam.lon, cam.lat]
    let tries = 0
    let corrected = false
    let stopWatching = () => {}
    const go = () => {
      if (getCamera() === null && tries++ < 40) { timer = window.setTimeout(go, 250); return }
      setSelection({ center, pin: true })
      flyToThing(center)
      const until = performance.now() + 6000
      stopWatching = onCameraMoved(() => {
        const at = getCamera()?.center
        if (!at || corrected || performance.now() > until) return
        if (Math.abs(at[0] - center[0]) > 0.002 || Math.abs(at[1] - center[1]) > 0.002) {
          corrected = true
          flyToThing(center)
        }
      })
    }
    let timer = window.setTimeout(go, 0)
    return () => { window.clearTimeout(timer); stopWatching() }
  }, [cam?.analytics_id, cam?.lat, cam?.lon]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => () => setSelection(null), [])
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (typing(e) || !document.activeElement?.closest("[data-workspace]")) return
      if (e.key === "Escape") leave()
      else if (e.key === "[") step(-1)
      else if (e.key === "]") step(1)
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [])

  async function confirm() {
    if (!cam || locked) return
    if (await saveCamera(cam.analytics_id)) {
      toast.success(`${cam.name} confirmed`)
      step(1)
    } else toast.error(useReview.getState().error ?? "It wasn't saved.")
  }

  if (g.inspecting) return null
  const style = g.desktop
    ? { top: STRIP_H + GAP, right: GAP, width: Math.max(g.workspaceW, 380), maxHeight: `calc(100% - ${STRIP_H + 2 * GAP + g.layersBottom}px)` }
    : { left: 8, right: 8, bottom: 8, height: g.sheetH }
  const position = cam ? order.indexOf(cam.analytics_id) + 1 : 0

  return (
    <aside data-workspace aria-labelledby="review-title" className={cn("panel-float absolute flex flex-col overflow-hidden rounded-xl", g.desktop ? "z-10" : "z-20")} style={style}>
      <div className="flex h-14 shrink-0 items-center gap-3 border-b border-line pr-2 pl-4">
        <CctvIcon className="size-[18px] text-sodium" aria-hidden="true" />
        <div className="min-w-0 flex-1 leading-tight">
          <h2 ref={title} tabIndex={-1} id="review-title" className="text-[14.5px] font-semibold outline-none">Counting cameras</h2>
          <p className="truncate text-[12.5px] text-muted-foreground" aria-live="polite">
            {data ? `${data.reviewed} of ${data.cameras.length} confirmed` : "Each camera and what its lines count"}
          </p>
        </div>
        <Tooltip>
          <TooltipTrigger render={<Button variant="ghost" size="icon-sm" aria-label="Close the walkthrough" onClick={leave} />}>
            <XIcon />
          </TooltipTrigger>
          <TooltipContent>Back to Live <Kbd>Esc</Kbd></TooltipContent>
        </Tooltip>
      </div>

      {status === "loading" && !data && (
        <div className="flex flex-col gap-3 p-4" aria-busy="true" aria-label="Loading the cameras">
          <Skeleton className="h-8" /><Skeleton className="h-24" /><Skeleton className="h-40" />
        </div>
      )}
      {status === "error" && !data && (
        <div className="p-4">
          <Alert variant="destructive">
            <AlertTitle>The cameras couldn't be read</AlertTitle>
            <AlertDescription>{error}</AlertDescription>
          </Alert>
          <Button variant="outline" size="sm" className="mt-3" onClick={() => void loadReview()}>Try again</Button>
        </div>
      )}

      {data && cam && (
        <>
          <div className="flex shrink-0 items-center gap-2 border-b border-line px-3 py-2">
            <Tooltip>
              <TooltipTrigger render={<Button variant="ghost" size="icon-sm" aria-label="Previous camera" onClick={() => step(-1)} />}>
                <ChevronLeftIcon />
              </TooltipTrigger>
              <TooltipContent>Previous camera <Kbd>[</Kbd></TooltipContent>
            </Tooltip>
            <Select value={String(cam.analytics_id)} onValueChange={(v) => showCamera(Number(v))}>
              <SelectTrigger size="sm" aria-label="Go to a camera" className="min-w-0 flex-1">
                <SelectValue>{`${position} of ${order.length}: ${cam.name}`}</SelectValue>
              </SelectTrigger>
              <SelectContent>
                <SelectGroup>
                  {order.map((id, i) => {
                    const c = cameraById(id)
                    return c ? (
                      <SelectItem key={id} value={String(id)}>
                        {c.reviewed ? <CircleCheckIcon aria-hidden="true" /> : null}
                        {`${i + 1}. ${c.name}`}
                      </SelectItem>
                    ) : null
                  })}
                </SelectGroup>
              </SelectContent>
            </Select>
            <Tooltip>
              <TooltipTrigger render={<Button variant="ghost" size="icon-sm" aria-label="Next camera" onClick={() => step(1)} />}>
                <ChevronRightIcon />
              </TooltipTrigger>
              <TooltipContent>Next camera <Kbd>]</Kbd></TooltipContent>
            </Tooltip>
          </div>

          <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto p-4">
            <div className="flex flex-col gap-1">
              <div className="flex flex-wrap items-center gap-2">
                <h3 className="font-medium">{cam.name}</h3>
                <Badge variant={cam.status === "started" ? "outline" : "secondary"}>{cam.status === "started" ? "Counting" : "Stopped"}</Badge>
                {cam.reviewed && <Badge variant="outline"><CircleCheckIcon data-icon="inline-start" />Confirmed</Badge>}
              </div>
              <Position cam={cam} />
            </div>
            <Sketch cam={cam} />
            {data.yesterday_missed_hours > 0 && (
              <p className="text-xs text-muted-foreground">{data.yesterday_missed_hours} of yesterday's hours couldn't be read, so its figures may be short.</p>
            )}
            <Separator />
            {cam.lines.map((line, i) => (
              <LineCard key={line.line_name} cam={cam} line={line} index={i} hours={data.hours} locked={locked} />
            ))}
          </div>

          <div className="flex shrink-0 flex-wrap items-center gap-2 border-t border-line bg-muted/40 p-3">
            {locked ? (
              data.admin.configured ? (
                <UnlockButton what="Confirming the cameras changes how every traffic answer is counted, so it needs the admin password." onUnlocked={() => void loadReview()} />
              ) : (
                <p className="text-xs text-muted-foreground">Read only: set CHATBOT_ADMIN_PASSWORD on the server to confirm cameras.</p>
              )
            ) : (
              <Button onClick={() => void confirm()} aria-disabled={saving || undefined}>
                {saving && <Spinner data-icon="inline-start" />}
                {cam.reviewed && !edited ? "Confirm again and go on" : "Confirm and go to the next"}
              </Button>
            )}
            <Button variant="ghost" onClick={() => step(1)}>Skip for now</Button>
          </div>
        </>
      )}
    </aside>
  )
}
