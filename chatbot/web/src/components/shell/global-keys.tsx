/* The page's own keys, wherever the keyboard is: Alt and a number opens a workspace (the rail used
   to hold these), ? shows every shortcut. */
import { useEffect } from "react"

import { typing } from "@/lib/hooks"
import { useLayout } from "@/lib/layout"
import { openWorkspace, useWorkspace } from "@/lib/registries/workspaces"

export function GlobalKeys() {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.altKey && !e.ctrlKey && !e.metaKey) {
        // Alt changes the character on some layouts (Alt 2 is ™ on a Mac): read the key's place.
        const digit = /^Digit(\d)$/.exec(e.code)?.[1] ?? e.key
        const w = useWorkspace.getState().list.find((x) => x.key === digit)
        if (w) { e.preventDefault(); openWorkspace(w.id) }
      }
      if (e.key === "?" && !typing(e)) { e.preventDefault(); useLayout.setState({ shortcutsOpen: true }) }
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [])
  return null
}
