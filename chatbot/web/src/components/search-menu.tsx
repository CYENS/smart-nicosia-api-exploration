import { useEffect, useMemo, useState } from "react"

import "@/components/search-providers"
import {
  Command, CommandDialog, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList,
} from "@/components/ui/command"
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
export function SearchMenu() {
  const open = useLayout((s) => s.questionsOpen)
  const seed = useLayout((s) => s.searchSeed)
  const busy = useChat((s) => s.busy)
  const [q, setQ] = useState("")
  const [openedWith, setOpenedWith] = useState<string | null>(null)
  // Each time it opens, it starts from what it was opened with (a street from the inspector).
  if (open && openedWith === null) { setOpenedWith(seed); setQ(seed) }
  if (!open && openedWith !== null) setOpenedWith(null)
  const { found, waiting, failed } = useSearch(q, open)
  const setOpen = (questionsOpen: boolean) => useLayout.setState({ questionsOpen })

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

  const run = (item: SearchItem) => {
    setOpen(false)
    item.run()
  }
  const empty = !groups.length && !waiting

  return (
    <CommandDialog open={open} onOpenChange={setOpen} title="Search the twin" description="Search devices, streets, places, register buildings, commands and questions">
      <Command shouldFilter={false} loop>
        <CommandInput value={q} onValueChange={setQ} placeholder="Search devices, streets, places, file numbers" />
        <CommandList className="max-h-[min(62vh,480px)]">
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
                  disabled={item.disabled || (busy && item.key.startsWith("q:"))}
                  onSelect={() => run(item)}
                >
                  <item.icon />
                  <span className="flex min-w-0 flex-1 flex-col">
                    <span className="flex items-center gap-1.5">
                      {item.color && <span className="size-2 shrink-0 rounded-full" style={{ background: item.color }} aria-hidden="true" />}
                      <span className="whitespace-normal" lang={/[Ͱ-Ͽ]/.test(item.title) ? "el" : undefined}>{item.title}</span>
                    </span>
                    {item.detail && <span className="truncate text-[12px] text-muted-foreground">{item.detail}</span>}
                  </span>
                </CommandItem>
              ))}
            </CommandGroup>
          ))}
        </CommandList>
      </Command>
    </CommandDialog>
  )
}
