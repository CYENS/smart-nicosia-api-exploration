/* The ask bar: where the conversation stands while it is hidden. Typing and pressing Enter asks
   the assistant and opens the conversation; Ctrl K searches; once there is a conversation, a
   button brings it back. "/" anywhere outside a field puts the keyboard here. */
import { ArrowUpIcon, MessageSquareTextIcon, SearchIcon } from "lucide-react"
import { useEffect, useRef, useState } from "react"

import { MOD_KEY } from "@/components/chat/composer"
import { Kbd } from "@/components/ui/kbd"
import { InputGroup, InputGroupAddon, InputGroupButton, InputGroupInput } from "@/components/ui/input-group"
import { Spinner } from "@/components/ui/spinner"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { WallsMark } from "@/components/walls-mark"
import { send, useChat } from "@/lib/chat"
import { useGeometry } from "@/lib/geometry"
import { GAP, PHONE_GAP, PHONE_ROW, ROW_H, STRIP_H, openSearch, setChatOpen, useLayout } from "@/lib/layout"

export function AskBar() {
  const open = useLayout((s) => s.chatOpen)
  const turns = useChat((s) => s.turns.length)
  const busy = useChat((s) => s.busy)
  const g = useGeometry()
  const [text, setText] = useState("")
  const input = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (open) return
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement
      if (e.key === "/" && !e.ctrlKey && !e.metaKey && !t.closest("input, textarea, [contenteditable]")) {
        e.preventDefault()
        input.current?.focus()
      }
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [open])

  if (open) return null

  const submit = () => {
    if (busy || !text.trim()) return
    void send(text)
    setText("")
    // The conversation opens; on a phone it takes over from the Layers sheet.
    useLayout.setState(g.desktop ? { chatOpen: true } : { chatOpen: true, layersOpen: false })
  }

  const place = g.desktop
    ? { left: GAP, top: STRIP_H + GAP, width: g.chatW, height: ROW_H + 4 }
    : { left: PHONE_GAP, right: PHONE_GAP, top: PHONE_GAP, height: PHONE_ROW }

  return (
    <form
      role="search"
      aria-label="Ask about Nicosia"
      className="absolute z-20"
      style={place}
      onSubmit={(e) => { e.preventDefault(); submit() }}
    >
      <InputGroup className="panel-float h-full rounded-xl bg-console dark:bg-console has-[[data-slot=input-group-control]:focus-visible]:ring-3">
        <InputGroupAddon align="inline-start" className="pl-3">
          <WallsMark working={busy} className="size-5 text-sodium" />
        </InputGroupAddon>
        <InputGroupInput
          ref={input}
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="Ask about Nicosia"
          aria-label="Ask about Nicosia"
          enterKeyHint="send"
          className="text-[15px] placeholder:text-muted-foreground"
        />
        <InputGroupAddon align="inline-end" className="gap-1 pr-1.5">
          {text.trim() ? (
            <InputGroupButton type="submit" variant="default" size="icon-sm" className="rounded-lg" disabled={busy} aria-label={busy ? "Working" : "Ask"}>
              {busy ? <Spinner /> : <ArrowUpIcon />}
            </InputGroupButton>
          ) : (
            <Tooltip>
              <TooltipTrigger render={<InputGroupButton size="sm" className="h-8 gap-1.5 px-2 text-muted-foreground" aria-label="Search the twin" onClick={() => openSearch()} />}>
                <SearchIcon />
                <Kbd className="max-md:hidden">{MOD_KEY} K</Kbd>
              </TooltipTrigger>
              <TooltipContent>Search devices, streets, places and commands</TooltipContent>
            </Tooltip>
          )}
          {turns > 0 && (
            <Tooltip>
              <TooltipTrigger
                render={
                  <InputGroupButton
                    size="sm"
                    variant="secondary"
                    className="h-8 gap-1.5 px-2.5"
                    aria-label="Show the conversation"
                    onClick={() => setChatOpen(true)}
                  />
                }
              >
                {busy ? <WallsMark working className="size-4 text-sodium" /> : <MessageSquareTextIcon />}
                <span className="max-sm:sr-only">Conversation</span>
              </TooltipTrigger>
              <TooltipContent>Show the conversation, {turns === 1 ? "one question" : `${turns} questions`} so far</TooltipContent>
            </Tooltip>
          )}
        </InputGroupAddon>
      </InputGroup>
    </form>
  )
}
