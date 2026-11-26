/* The card under an answer when the assistant proposes a watch (create_watch). Nothing is saved
   until the reader presses Save; Not now lets it go. What the card shows: what the watch looks
   at, what holds now, and how it will tell. Its outcome is kept per proposal, so it survives
   the conversation panel closing and opening again. */
import { BellPlusIcon, BellRingIcon, CheckIcon } from "lucide-react"
import { useRef } from "react"
import { create } from "zustand"

import { Button } from "@/components/ui/button"
import { openWorkspace } from "@/lib/registries/workspaces"
import { wantInboxFocus } from "@/lib/watch/focus"
import type { TurnCardProps } from "@/lib/registries/turn-cards"
import { confirmWatch, selectIncident, type WatchProposal } from "@/lib/watch/store"

type Outcome = { state: "saving" | "saved" | "declined" | "error"; message?: string }
const useOutcomes = create<Record<string, Outcome>>(() => ({}))

const UNITS: Record<string, string> = {
  fill_pct: "%", caqi: "", noise_db: " dB", pm2_5: " µg/m³", pm10: " µg/m³", no2: " µg/m³", power_w: " W",
}
const NOUNS: Record<string, [string, string]> = {
  waste_bin: ["bin", "bins"], street_light: ["lamp", "lamps"], parking_sensor: ["parking bay", "parking bays"],
  air_station: ["air station", "air stations"], info_screen: ["info screen", "info screens"], camera: ["camera", "cameras"],
}
const STATE_WORDS: Record<string, string> = {
  very_low: "very good", low: "good", medium: "moderate", high: "poor", very_high: "very poor",
  offline: "offline", on: "on", off: "off", free: "free", occupied: "taken", full: "full",
}

function valueText(p: WatchProposal, v: { id: string; value: number | null; state: string }) {
  const metric = String(p.rule.metric ?? "")
  if (metric && v.value !== null) return `${v.id} at ${Math.round(v.value * 10) / 10}${UNITS[metric] ?? ""}`
  return `${v.id} ${STATE_WORDS[v.state] ?? v.state.replace(/_/g, " ")}`
}

function nowWords(p: WatchProposal) {
  const kind = String(p.rule.kind ?? "")
  if (kind === "incident") return "It looks at every incident the twin opens from now on."
  const [one, many] = NOUNS[kind] ?? ["device", "devices"]
  const watching = `It looks at ${p.watching} ${p.watching === 1 ? one : many}.`
  if (p.true_now.length) {
    return `${watching} True already for ${p.true_now.slice(0, 3).map((v) => valueText(p, v)).join(", ")}${p.true_now.length > 3 ? ` and ${p.true_now.length - 3} more` : ""}; it tells you the next time it comes true.`
  }
  const now = p.values_now.slice(0, 3).map((v) => valueText(p, v))
  return `${watching}${now.length ? ` Now: ${now.join(", ")}.` : ""}`
}

// The inbox's list, at its watches.
function showWatches() {
  selectIncident(null)
  openWorkspace("incidents")
  wantInboxFocus(null)
  requestAnimationFrame(() => requestAnimationFrame(() => {
    const el = document.getElementById("watches-section")
    el?.scrollIntoView({ block: "nearest" })
    el?.focus({ preventScroll: true })
  }))
}

export function WatchCard({ data }: TurnCardProps) {
  const p = data.proposal as WatchProposal | undefined
  const outcome = useOutcomes((s) => (p ? s[p.proposal_id] : undefined))
  const box = useRef<HTMLDivElement>(null)
  if (!p) return null
  const id = p.proposal_id
  const save = async () => {
    useOutcomes.setState({ [id]: { state: "saving" } })
    try {
      await confirmWatch(id)
      useOutcomes.setState({ [id]: { state: "saved" } })
    } catch (e) {
      useOutcomes.setState({ [id]: { state: "error", message: (e as Error).message } })
    }
    // The buttons are gone: the keyboard stays on the card.
    requestAnimationFrame(() => box.current?.focus({ preventScroll: true }))
  }
  const decline = () => {
    useOutcomes.setState({ [id]: { state: "declined" } })
    requestAnimationFrame(() => box.current?.focus({ preventScroll: true }))
  }
  const state = outcome?.state
  return (
    <div
      ref={box}
      tabIndex={-1}
      role="group"
      aria-labelledby={`watch-${id}`}
      className="flex flex-col gap-2.5 rounded-xl border border-line bg-raised/60 p-3.5 outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
    >
      <div className="flex items-start gap-2.5">
        {state === "saved"
          ? <BellRingIcon className="mt-0.5 size-4 shrink-0 text-sodium" aria-hidden="true" />
          : <BellPlusIcon className="mt-0.5 size-4 shrink-0 text-muted-foreground" aria-hidden="true" />}
        <div className="flex min-w-0 flex-col gap-1">
          <p id={`watch-${id}`} className="text-[14px] leading-snug font-semibold">{p.label}</p>
          <p className="text-[13px] leading-snug text-muted-foreground">{nowWords(p)}</p>
        </div>
      </div>
      <div aria-live="polite" className="flex flex-wrap items-center gap-2">
        {(state === undefined || state === "saving") && (
          <>
            <Button size="sm" disabled={state === "saving"} onClick={() => void save()}>
              <CheckIcon data-icon="inline-start" />{state === "saving" ? "Saving" : "Save the watch"}
            </Button>
            <Button size="sm" variant="ghost" disabled={state === "saving"} onClick={decline}>Not now</Button>
            <span className="text-[12.5px] text-muted-foreground">Nothing is watched until you save it.</span>
          </>
        )}
        {state === "saved" && (
          <>
            <span className="text-[13px]">Saved. A toast tells you when it comes true, on every open page.</span>
            <Button size="sm" variant="ghost" onClick={showWatches}>See your watches</Button>
          </>
        )}
        {state === "declined" && <span className="text-[13px] text-muted-foreground">Not saved.</span>}
        {state === "error" && (
          <span role="alert" className="text-[13px] text-signal">Couldn't save it: {outcome?.message}. Ask for the watch again.</span>
        )}
      </div>
    </div>
  )
}
