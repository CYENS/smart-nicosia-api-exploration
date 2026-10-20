import { MessageSquareTextIcon, PanelLeftCloseIcon, RotateCcwIcon } from "lucide-react"
import { useEffect, useState } from "react"

import { Composer } from "@/components/chat/composer"
import { TurnView } from "@/components/chat/turn"
import { Welcome } from "@/components/chat/welcome"
import { WallsMark } from "@/components/walls-mark"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import {
  MessageScroller, MessageScrollerButton, MessageScrollerContent, MessageScrollerItem,
  MessageScrollerProvider, MessageScrollerViewport,
} from "@/components/ui/message-scroller"
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { newChat, useChat } from "@/lib/chat"
import { useGeometry } from "@/lib/geometry"
import { useLayout } from "@/lib/layout"

const SOURCES = [
  "Smart Nicosia city sensors", "OpenStreetMap", "Open-Meteo", "Valhalla routing",
  "DLS Cyprus cadastre and 3D buildings", "The ΕΟΑ dangerous-building registers",
]

function useCredentialsMissing() {
  const [missing, setMissing] = useState(false)
  useEffect(() => {
    fetch("/api/health").then((r) => r.json()).then((h) => setMissing(!h.has_credentials)).catch(() => {})
  }, [])
  return missing
}

export function ChatPanel() {
  const open = useLayout((s) => s.chatOpen)
  const turns = useChat((s) => s.turns)
  const busy = useChat((s) => s.busy)
  const noKey = useCredentialsMissing()
  const { desktop, chatW } = useGeometry()

  if (!open) {
    return (
      <Button
        variant="secondary"
        className="panel-float absolute top-[64px] left-3 z-10 h-10 gap-2 rounded-xl px-3.5"
        onClick={() => useLayout.setState({ chatOpen: true })}
      >
        {busy ? <WallsMark working className="size-4 text-sodium" /> : <MessageSquareTextIcon data-icon="inline-start" />}
        Conversation
      </Button>
    )
  }

  return (
    <aside
      aria-label="Conversation"
      className="panel-float absolute inset-x-2 bottom-2 z-10 flex h-[56%] flex-col overflow-hidden rounded-xl md:inset-x-auto md:top-[64px] md:bottom-3 md:left-3 md:h-auto"
      style={desktop ? { width: chatW } : undefined}
    >
      <div className="flex h-12 shrink-0 items-center justify-between gap-2 border-b border-line pr-2 pl-4">
        <h2 className="text-[14px] font-semibold">Conversation</h2>
        <div className="flex items-center gap-1">
          <Button variant="ghost" size="sm" onClick={newChat} disabled={busy || !turns.length}>
            <RotateCcwIcon data-icon="inline-start" />
            New chat
          </Button>
          <Tooltip>
            <TooltipTrigger
              render={<Button variant="ghost" size="icon-sm" aria-label="Hide the conversation" onClick={() => useLayout.setState({ chatOpen: false })} />}
            >
              <PanelLeftCloseIcon />
            </TooltipTrigger>
            <TooltipContent>Hide the conversation</TooltipContent>
          </Tooltip>
        </div>
      </div>

      {noKey && (
        <Alert variant="destructive" className="mx-3 mt-3 w-auto">
          <AlertTitle>No Anthropic API key</AlertTitle>
          <AlertDescription>
            Copy <code>.env.example</code> to <code>.env</code>, set <code>ANTHROPIC_API_KEY</code> and restart the
            chatbot. The map and the layers work without it.
          </AlertDescription>
        </Alert>
      )}

      {turns.length === 0 ? (
        // Read from the top: the scroller below follows the end, which suits a conversation only.
        <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4">
          <Welcome />
        </div>
      ) : (
        <MessageScrollerProvider autoScroll>
          <MessageScroller className="min-h-0 flex-1">
            <MessageScrollerViewport>
              <MessageScrollerContent className="gap-8 px-4 py-4">
                {turns.map((t) => (
                  <MessageScrollerItem key={t.id} messageId={t.id} scrollAnchor>
                    <TurnView turn={t} />
                  </MessageScrollerItem>
                ))}
              </MessageScrollerContent>
            </MessageScrollerViewport>
            <MessageScrollerButton />
          </MessageScroller>
        </MessageScrollerProvider>
      )}

      <div className="flex shrink-0 flex-col gap-2 border-t border-line p-3">
        <Composer />
        <p className="px-1 text-[12px] text-muted-foreground">
          Answers can be wrong, so check what matters.{" "}
          <Popover>
            <PopoverTrigger className="underline decoration-line underline-offset-3 hover:text-foreground">Sources</PopoverTrigger>
            <PopoverContent side="top" align="start" className="w-72">
              <ul className="flex flex-col gap-1 text-[13px]">
                {SOURCES.map((s) => <li key={s}>{s}</li>)}
              </ul>
            </PopoverContent>
          </Popover>
        </p>
      </div>
    </aside>
  )
}
