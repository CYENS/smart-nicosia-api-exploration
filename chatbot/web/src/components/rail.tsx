import { KeyboardIcon, SearchIcon } from "lucide-react"
import { useEffect } from "react"

import { MOD_KEY } from "@/components/chat/composer"
import { Kbd } from "@/components/ui/kbd"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { typing } from "@/lib/hooks"
import { STRIP_H, openSearch, useLayout } from "@/lib/layout"
import { openWorkspace, useWorkspace } from "@/lib/registries/workspaces"
import { cn } from "@/lib/utils"

function RailButton({ label, hint, active, onClick, id, children }: {
  label: string; hint?: React.ReactNode; active?: boolean; onClick: () => void; id?: string; children: React.ReactNode
}) {
  return (
    <Tooltip>
      <TooltipTrigger
        render={
          <button
            type="button"
            aria-label={label}
            data-workspace-id={id}
            aria-current={active ? "page" : undefined}
            onClick={onClick}
            className={cn(
              "relative flex size-10 items-center justify-center rounded-lg text-muted-foreground transition-colors outline-none hover:bg-accent/70 hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50 [&_svg]:size-[18px]",
              active && "bg-accent/60 text-foreground",
            )}
          />
        }
      >
        {active && <span className="absolute top-2 bottom-2 -left-1 w-[3px] rounded-full bg-sodium" aria-hidden="true" />}
        {children}
      </TooltipTrigger>
      <TooltipContent side="right">{label}{hint && <> {hint}</>}</TooltipContent>
    </Tooltip>
  )
}

// The twin's workspaces down the left edge (Live today; later features add theirs), with search
// and the keyboard shortcuts at the foot. Wider screens only: a phone has the composer's Search.
export function Rail() {
  const { active, list } = useWorkspace()

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.altKey && !e.ctrlKey && !e.metaKey) {
        const w = useWorkspace.getState().list.find((x) => x.key === e.key)
        if (w) { e.preventDefault(); openWorkspace(w.id) }
      }
      if (e.key === "?" && !typing(e)) { e.preventDefault(); useLayout.setState({ shortcutsOpen: true }) }
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [])

  return (
    <nav
      aria-label="Workspaces"
      className="absolute bottom-0 left-0 z-20 hidden w-12 flex-col items-center gap-1 border-r border-line bg-night py-2 md:flex"
      style={{ top: STRIP_H }}
    >
      {list.map((w) => (
        <RailButton key={w.id} id={w.id} label={w.description} hint={w.key && <Kbd>Alt {w.key}</Kbd>} active={w.id === active} onClick={() => openWorkspace(w.id)}>
          <w.icon />
        </RailButton>
      ))}
      <div className="mt-auto flex flex-col items-center gap-1">
        <RailButton label="Search everything" hint={<Kbd>{MOD_KEY} K</Kbd>} onClick={() => openSearch()}>
          <SearchIcon />
        </RailButton>
        <RailButton label="Keyboard shortcuts" hint={<Kbd>?</Kbd>} onClick={() => useLayout.setState({ shortcutsOpen: true })}>
          <KeyboardIcon />
        </RailButton>
      </div>
    </nav>
  )
}
