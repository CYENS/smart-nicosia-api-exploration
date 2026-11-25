import { MOD_KEY } from "@/components/chat/composer"
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Kbd } from "@/components/ui/kbd"
import { useLayout } from "@/lib/layout"
import { useWorkspace } from "@/lib/registries/workspaces"

// Every shortcut on the page, in one place (? opens it).
export function Shortcuts() {
  const open = useLayout((s) => s.shortcutsOpen)
  const workspaces = useWorkspace((s) => s.list)
  const rows: [React.ReactNode, string][] = [
    [<Kbd key="k">{MOD_KEY} K</Kbd>, "Search devices, streets, places, file numbers, commands and questions"],
    [<Kbd key="/">/</Kbd>, "Write a question"],
    [<Kbd key="esc">Esc</Kbd>, "Close the inspector, or the menu that is open"],
    [<Kbd key="f">F</Kbd>, "Fly to the thing in the inspector"],
    [<><Kbd>←</Kbd> <Kbd>→</Kbd></>, "Read the 24-hour sparkline moment by moment, once it has the focus"],
    ...workspaces.filter((w) => w.key).map((w): [React.ReactNode, string] => [<Kbd key={w.id}>Alt {w.key}</Kbd>, `Open ${w.description.split(":")[0]}`]),
    [<Kbd key="?">?</Kbd>, "Show these shortcuts"],
  ]
  return (
    <Dialog open={open} onOpenChange={(shortcutsOpen) => useLayout.setState({ shortcutsOpen })}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Keyboard shortcuts</DialogTitle>
          <DialogDescription>Everything on the page also works with Tab, Enter and the arrow keys.</DialogDescription>
        </DialogHeader>
        <dl className="grid grid-cols-[auto_1fr] items-baseline gap-x-4 gap-y-2.5 text-[14px]">
          {rows.map(([keys, what], i) => (
            <div key={i} className="contents">
              <dt className="flex justify-end gap-1">{keys}</dt>
              <dd className="text-muted-foreground">{what}</dd>
            </div>
          ))}
        </dl>
      </DialogContent>
    </Dialog>
  )
}
