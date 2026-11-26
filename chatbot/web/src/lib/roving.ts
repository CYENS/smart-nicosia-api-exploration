/* Arrow keys inside a list of rows (WAI-ARIA's roving tab index): Tab enters the list on one row
   and leaves it on the next; Up and Down move between rows, Home and End to the first and
   last; Enter or Space opens the row (it is a button). Rows are the elements marked `data-row`
   (give each a stable `data-key`), inside the element the hook's ref is on. A group of chips
   that wraps onto lines (`orientation: "both"`) also moves with Left and Right. */
import { useCallback, useLayoutEffect, useRef, type FocusEvent, type KeyboardEvent } from "react"

function rowsOf(el: HTMLElement | null) {
  return el ? [...el.querySelectorAll<HTMLElement>("[data-row]")] : []
}

export function useRovingList<T extends HTMLElement = HTMLElement>(
  { orientation = "vertical" }: { orientation?: "vertical" | "both" } = {},
) {
  const ref = useRef<T>(null)
  const active = useRef<string | null>(null)

  const sync = useCallback(() => {
    const rows = rowsOf(ref.current)
    if (!rows.length) return
    const current = rows.find((r) => r.dataset.key === active.current) ?? rows[0]
    for (const r of rows) r.tabIndex = r === current ? 0 : -1
  }, [])

  // After every render: rows come and go as the data refreshes, and exactly one stays tabbable.
  useLayoutEffect(sync)

  const onKeyDown = useCallback((e: KeyboardEvent) => {
    const rows = rowsOf(ref.current)
    const i = rows.indexOf(document.activeElement as HTMLElement)
    if (i < 0 || e.altKey || e.ctrlKey || e.metaKey) return
    const both = orientation === "both"
    const next = e.key === "ArrowDown" || (both && e.key === "ArrowRight")
    const prev = e.key === "ArrowUp" || (both && e.key === "ArrowLeft")
    const j = next ? Math.min(i + 1, rows.length - 1)
      : prev ? Math.max(i - 1, 0)
        : e.key === "Home" ? 0
          : e.key === "End" ? rows.length - 1
            : null
    if (j === null) return
    e.preventDefault()
    rows[j].focus()
  }, [orientation])

  const onFocus = useCallback((e: FocusEvent) => {
    const row = (e.target as HTMLElement).closest<HTMLElement>("[data-row]")
    if (!row || !ref.current?.contains(row)) return
    active.current = row.dataset.key ?? null
    sync()
  }, [sync])

  return { ref, onKeyDown, onFocus }
}
