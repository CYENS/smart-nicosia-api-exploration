/* The topic chips: four at most, one picked at a time. A toggle group, so Tab enters it once,
   the arrow keys move along it, Enter or Space picks, and Esc lets the picked topic go. */
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { PHONE_CHIPS_H, useGeometry } from "@/lib/geometry"
import { GAP, PHONE_GAP, ROW_H, setChatOpen } from "@/lib/layout"
import { chipTopics, selectTopic, useTopics } from "@/lib/registries/topics"
import { cn } from "@/lib/utils"

export const CHIPS_ID = "topic-chips"

// Give the keyboard back to the chips (to the picked one, else the first): where closing a
// topic's card returns it.
export function focusChips() {
  requestAnimationFrame(() => {
    const box = document.getElementById(CHIPS_ID)
    const el = box?.querySelector<HTMLElement>("[aria-pressed=true]") ?? box?.querySelector<HTMLElement>("button")
    el?.focus()
  })
}

export function TopicChips() {
  const list = useTopics((s) => s.list)
  const active = useTopics((s) => s.active)
  const g = useGeometry()
  const chips = chipTopics(list)
  if (!chips.length) return null

  const pick = (id: string | null) => {
    selectTopic(id)
    // A phone has room for one sheet: the topic's card takes over from the conversation.
    if (id && !g.desktop) setChatOpen(false)
  }

  return (
    <div
      className={cn(
        "absolute z-20",
        // On a phone the row scrolls sideways under the ask bar, edge to edge.
        !g.desktop && "overflow-x-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden",
      )}
      style={g.desktop ? { left: GAP, top: g.chipsTop, maxWidth: g.vw - 2 * GAP - 360 } : { left: 0, right: 0, top: g.chipsTop, paddingInline: PHONE_GAP }}
    >
      <ToggleGroup
        id={CHIPS_ID}
        aria-label="Topics on the map"
        value={active ? [active] : []}
        onValueChange={(v) => pick((v[0] as string | undefined) ?? null)}
        loopFocus
        spacing={2}
        onKeyDown={(e) => {
          if (e.key === "Escape" && active) { e.preventDefault(); e.stopPropagation(); pick(null) }
        }}
        className="flex-nowrap"
      >
        {chips.map((t) => (
          <Tooltip key={t.id}>
            <TooltipTrigger
              render={
                <ToggleGroupItem
                  value={t.id}
                  variant="outline"
                  className={cn(
                    "panel-float gap-1.5 rounded-full border-line bg-console px-3.5 text-[14px] font-medium text-foreground shadow-none",
                    "hover:bg-raised aria-pressed:bg-[color-mix(in_srgb,var(--sodium)_16%,var(--console))] aria-pressed:hover:bg-[color-mix(in_srgb,var(--sodium)_22%,var(--console))]",
                    "focus-visible:ring-3 focus-visible:ring-ring/60 [&_svg:not([class*='size-'])]:size-4",
                  )}
                  style={{ height: g.desktop ? ROW_H : PHONE_CHIPS_H }}
                />
              }
            >
              <t.icon aria-hidden="true" className="text-muted-foreground" />
              {t.label}
            </TooltipTrigger>
            <TooltipContent side="bottom">{t.description}</TooltipContent>
          </Tooltip>
        ))}
      </ToggleGroup>
    </div>
  )
}
