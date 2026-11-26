import { useEffect, useMemo, useRef, useState } from "react"

import "@/components/search-providers"
import {
  Command, CommandDialog, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList,
} from "@/components/ui/command"
import { Kbd, KbdGroup } from "@/components/ui/kbd"
import { Skeleton } from "@/components/ui/skeleton"
import { useChat } from "@/lib/chat"
import { useLayout } from "@/lib/layout"
import { searchProviders, type SearchItem } from "@/lib/registries/search"

type Found = { provider: string; heading: string; order: number; items: SearchItem[] }

// What every provider has for a query: local ones at once, the server's after a short pause.
function useSearch(q: string, open: boolean) {
  const [found, setFound] = useState<Record<string, Found>>({})
  const [waiting, setWaiting] = useState(false)
  const [failed, setFailed] = useState(false)
  useEffect(() => {
    if (!open) return
    const ctrl = new AbortController()
    const providers = searchProviders()
    const local: Record<string, Found> = {}
    for (const p of providers.filter((p) => !p.remote)) {
      const items = p.search(q, ctrl.signal)
      if (Array.isArray(items)) local[p.id] = { provider: p.id, heading: p.heading, order: p.order, items }
    }
    const remote = providers.filter((p) => p.remote)
    // Local results show at once (a callback, so React batches it with what follows).
    queueMicrotask(() => {
      if (ctrl.signal.aborted) return
      setFound((prev) => ({ ...Object.fromEntries(Object.entries(prev).filter(([id]) => remote.some((p) => p.id === id) && q.trim().length >= 2)), ...local }))
      setWaiting(remote.length > 0 && q.trim().length >= 2)
      setFailed(false)
    })
    const timer = window.setTimeout(() => {
      Promise.all(remote.map(async (p) => {
        const items = await p.search(q, ctrl.signal)
        return { provider: p.id, heading: p.heading, order: p.order, items } satisfies Found
      }))
        .then((res) => {
          if (ctrl.signal.aborted) return
          setFound((prev) => ({ ...prev, ...Object.fromEntries(res.map((r) => [r.provider, r])) }))
          setWaiting(false)
        })
        .catch((e: Error) => {
          if (ctrl.signal.aborted || e.name === "AbortError") return
          setWaiting(false)
          setFailed(true)
        })
    }, 140)
    return () => { ctrl.abort(); clearTimeout(timer) }
  }, [q, open])
  return { found, waiting, failed }
}

// Search everything, a keystroke away (Ctrl K or Cmd K): devices by id or street, register
// buildings by address or file number, places, streets, commands and the prepared questions.
//
// The keyboard follows the WAI-ARIA combobox and listbox pattern: the arrow keys move the
// selection (Home and End to the ends, Page Up and Page Down by eight), Enter opens the selected
// row, Esc closes and gives the keyboard back to where it was.
//
// The list changes under the user as the server's results arrive, so the selection is ours, not
// cmdk's (cmdk re-selects the first row whenever the selected one goes, and reports it as a
// change). Until the user moves, the first row is selected. Once they have moved, the row they
// chose stays selected wherever new results put it; if it is gone (a row from the previous
// keystroke's results), the row at the position they reached is. Keys pressed before any result
// arrives count too. The selected row is scrolled into view after every change.
const PAGE = 8

type Choice = { key: string | null; index: number } | null // null: the user hasn't moved

export function SearchMenu() {
  const open = useLayout((s) => s.questionsOpen)
  const seed = useLayout((s) => s.searchSeed)
  const busy = useChat((s) => s.busy)
  const [q, setQ] = useState("")
  const [choice, setChoice] = useState<Choice>(null)
  const [openedWith, setOpenedWith] = useState<string | null>(null)
  // Each time it opens, it starts from what it was opened with (a street from the inspector).
  if (open && openedWith === null) { setOpenedWith(seed); setQ(seed); setChoice(null) }
  if (!open && openedWith !== null) setOpenedWith(null)
  const { found, waiting, failed } = useSearch(q, open)
  const setOpen = (questionsOpen: boolean) => useLayout.setState({ questionsOpen })
  const list = useRef<HTMLDivElement>(null)
  const opens = useRef(false) // the chosen row takes the keyboard elsewhere (the inspector)

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key.toLowerCase() === "k" && (e.ctrlKey || e.metaKey)) {
        e.preventDefault()
        useLayout.setState((s) => ({ questionsOpen: !s.questionsOpen, searchSeed: "" }))
      }
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [])

  // Groups in provider order; a provider's items may carry their own group (Devices, Places).
  const groups = useMemo(() => {
    const out: { heading: string; items: SearchItem[] }[] = []
    for (const f of Object.values(found).sort((a, b) => a.order - b.order)) {
      for (const item of f.items) {
        const heading = item.group ?? f.heading
        let g = out.find((x) => x.heading === heading)
        if (!g) out.push((g = { heading, items: [] }))
        g.items.push(item)
      }
    }
    return out
  }, [found])

  const disabled = (item: SearchItem) => item.disabled || (busy && item.key.startsWith("q:"))
  const keys = groups.flatMap((g) => g.items.filter((i) => !disabled(i)).map((i) => i.key))
  const at = choice === null ? 0 : choice.key !== null && keys.includes(choice.key)
    ? keys.indexOf(choice.key) : Math.min(choice.index, keys.length - 1)
  const selected = keys[at] ?? ""

  // The selected row stays in sight after every change, found by its key once the list is drawn.
  useEffect(() => {
    if (!selected) return
    const frame = requestAnimationFrame(() => {
      list.current?.querySelector<HTMLElement>(`[cmdk-item][data-value="${CSS.escape(selected)}"]`)
        ?.scrollIntoView({ block: "nearest" })
    })
    return () => cancelAnimationFrame(frame)
  }, [selected, groups])

  // Move to a row by position. While results are still coming the position may run past the
  // rows there are; once they have all come, the arrows wrap round.
  const moveTo = (index: number, wrap: boolean) => {
    const n = keys.length
    const i = wrap && n ? ((index % n) + n) % n : Math.max(0, n && !waiting ? Math.min(index, n - 1) : index)
    setChoice({ key: keys[i] ?? null, index: i })
  }
  const run = (item: SearchItem) => {
    opens.current = !!item.opens
    setOpen(false)
    item.run()
  }
  const empty = !groups.length && !waiting

  return (
    <CommandDialog
      open={open}
      onOpenChange={setOpen}
      title="Search the twin"
      description="Search devices, streets, places, register buildings, commands and questions"
      finalFocus={() => {
        const back = !opens.current // Esc, or a command: back to where the keyboard was
        opens.current = false
        return back
      }}
    >
      <Command
        shouldFilter={false}
        disablePointerSelection
        vimBindings={false}
        value={selected}
        // cmdk reports its own re-selection here; the selection is ours (above), so it is ignored.
        onValueChange={() => {}}
        onKeyDown={(e) => {
          // From the row shown; while results are still coming, from the position reached so far.
          const here = choice === null ? 0 : waiting && !(choice.key !== null && keys.includes(choice.key)) ? choice.index : at
          const step = { ArrowDown: 1, ArrowUp: -1, PageDown: PAGE, PageUp: -PAGE }[e.key]
          if (step !== undefined) moveTo(here + step, Math.abs(step) === 1 && !waiting)
          else if (e.key === "Home") moveTo(0, false)
          else if (e.key === "End") moveTo(Math.max(0, keys.length - 1), false)
          else return
          e.preventDefault() // cmdk leaves a key it sees handled alone
        }}
      >
        <CommandInput
          value={q}
          onValueChange={(v) => { setQ(v); setChoice(null); list.current?.scrollTo({ top: 0 }) }}
          placeholder="Search devices, streets, places, file numbers"
        />
        <CommandList
          ref={list}
          className="max-h-[min(62vh,480px)]"
          // The pointer selects what it rests on, like the keys (one selection for both).
          onPointerMove={(e) => {
            const row = (e.target as HTMLElement).closest<HTMLElement>("[cmdk-item]")
            const key = row?.getAttribute("data-value")
            if (key && key !== selected && keys.includes(key)) setChoice({ key, index: keys.indexOf(key) })
          }}
        >
          {empty && (
            <CommandEmpty>
              {failed ? "Search couldn't reach the twin. Commands and questions still work." : (
                <>Nothing matches {`"${q.trim()}"`}. Try a device id like YL1015, a street, or a register file number like 540/2024.</>
              )}
            </CommandEmpty>
          )}
          {waiting && !groups.some((g) => ["Devices", "Places", "Streets", "Dangerous buildings"].includes(g.heading)) && (
            <div className="flex flex-col gap-2 px-3 py-2.5" aria-label="Searching">
              <Skeleton className="h-3 w-20" />
              <Skeleton className="h-7 w-full" />
              <Skeleton className="h-7 w-4/5" />
            </div>
          )}
          {groups.map((g) => (
            <CommandGroup key={g.heading} heading={g.heading}>
              {g.items.map((item) => (
                <CommandItem
                  key={item.key}
                  value={item.key}
                  disabled={disabled(item)}
                  onSelect={() => run(item)}
                >
                  <item.icon />
                  <span className="flex min-w-0 flex-1 flex-col">
                    <span className="flex items-center gap-1.5">
                      {item.color && <span className="size-2 shrink-0 rounded-full" style={{ background: item.color }} aria-hidden="true" />}
                      <span className="whitespace-normal" lang={/[\u0370-\u03ff]/.test(item.title) ? "el" : undefined}>{item.title}</span>
                    </span>
                    {item.detail && <span className="truncate text-[12px] text-muted-foreground">{item.detail}</span>}
                  </span>
                </CommandItem>
              ))}
            </CommandGroup>
          ))}
        </CommandList>
        <div className="flex items-center gap-4 border-t border-line px-3 py-2 text-[12px] text-muted-foreground max-md:hidden">
          <span className="flex items-center gap-1.5"><KbdGroup><Kbd>↑</Kbd><Kbd>↓</Kbd></KbdGroup>to move</span>
          <span className="flex items-center gap-1.5"><Kbd>Enter</Kbd>to choose</span>
          <span className="flex items-center gap-1.5"><Kbd>Esc</Kbd>to close</span>
        </div>
      </Command>
    </CommandDialog>
  )
}
