import { ArrowUpIcon, ListIcon } from "lucide-react"
import { useEffect, useRef, useState } from "react"

import { InputGroup, InputGroupAddon, InputGroupButton, InputGroupTextarea } from "@/components/ui/input-group"
import { Kbd } from "@/components/ui/kbd"
import { Spinner } from "@/components/ui/spinner"
import { send, useChat } from "@/lib/chat"
import { useLayout } from "@/lib/layout"

export const MOD_KEY = typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform) ? "⌘" : "Ctrl"

export function Composer() {
  const busy = useChat((s) => s.busy)
  const [text, setText] = useState("")
  const ref = useRef<HTMLTextAreaElement>(null)

  // "/" anywhere outside a field jumps to the composer.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement
      if (e.key === "/" && !e.ctrlKey && !e.metaKey && !t.closest("input, textarea, [contenteditable]")) {
        e.preventDefault()
        useLayout.setState({ chatOpen: true })
        ref.current?.focus()
      }
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [])

  const submit = () => {
    if (busy || !text.trim()) return
    send(text)
    setText("")
  }

  return (
    <form onSubmit={(e) => { e.preventDefault(); submit() }}>
      <InputGroup className="rounded-xl bg-night/70 dark:bg-night/70">
        <InputGroupTextarea
          ref={ref}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); submit() }
          }}
          rows={1}
          placeholder="Ask about Nicosia"
          aria-label="Message"
          className="max-h-40 min-h-12 px-3.5 pt-3 text-[15px]"
        />
        <InputGroupAddon align="block-end" className="justify-between pb-2">
          <InputGroupButton
            variant="ghost"
            size="xs"
            className="text-muted-foreground"
            onClick={() => useLayout.setState({ questionsOpen: true })}
          >
            <ListIcon />
            Questions
            <Kbd className="max-md:hidden">{MOD_KEY} K</Kbd>
          </InputGroupButton>
          <InputGroupButton
            type="submit"
            variant="default"
            size="icon-sm"
            className="rounded-lg"
            disabled={busy || !text.trim()}
            aria-label={busy ? "Working" : "Send"}
          >
            {busy ? <Spinner /> : <ArrowUpIcon />}
          </InputGroupButton>
        </InputGroupAddon>
      </InputGroup>
    </form>
  )
}
