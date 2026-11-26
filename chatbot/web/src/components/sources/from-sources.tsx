/* "From Smart Nicosia and the twin's memory": a quiet line naming where something's data comes
   from. Each name opens the data sources panel at that source. */
import { useEffect } from "react"

import { loadSources, openSources, shortName, useSources } from "@/lib/sources"
import { cn } from "@/lib/utils"

function joined(parts: React.ReactNode[]): React.ReactNode[] {
  return parts.flatMap((p, i) => {
    if (i === 0) return [p]
    return [i === parts.length - 1 ? " and " : ", ", p]
  })
}

export function FromSources({ ids, className, label = "From" }: { ids?: string[] | null; className?: string; label?: string }) {
  const list = useSources((s) => s.list)
  const status = useSources((s) => s.status)
  useEffect(() => { if (status === "idle") void loadSources() }, [status])
  if (!ids?.length) return null
  const names = ids.map((id) => (
    <button
      key={id}
      type="button"
      className="rounded-sm underline decoration-line decoration-dotted underline-offset-3 outline-none hover:text-foreground hover:decoration-sodium focus-visible:text-foreground focus-visible:ring-2 focus-visible:ring-ring/60"
      onClick={(e) => openSources(id, e.currentTarget)}
    >
      {shortName(id, list)}
    </button>
  ))
  return (
    <p className={cn("text-[12px] leading-snug text-muted-foreground", className)}>
      {label} {joined(names)}
    </p>
  )
}
