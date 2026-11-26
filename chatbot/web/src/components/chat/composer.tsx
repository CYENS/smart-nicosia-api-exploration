import { ArrowUpIcon, SearchIcon, XIcon } from "lucide-react"
import { useEffect, useRef, useState } from "react"

import { InputGroup, InputGroupAddon, InputGroupButton, InputGroupTextarea } from "@/components/ui/input-group"
import { Kbd } from "@/components/ui/kbd"
import { Spinner } from "@/components/ui/spinner"
import { send, useChat, useComposer } from "@/lib/chat"
import { kindMeta, type EntityRef } from "@/lib/entities"
import { openSearch, useLayout } from "@/lib/layout"

export const MOD_KEY = typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform) ? "⌘" : "Ctrl"

// The thing a question is about, as a chip: in the composer (with a way to take it off) and in
// the question it was sent with.
export function SubjectChip({ subject, onRemove }: { subject: EntityRef; onRemove?: () => void }) {
  const Icon = kindMeta(subject.kind).icon
  return (
    <span className="inline-flex max-w-full items-center gap-1.5 rounded-md border border-sodium/40 bg-sodium/[0.08] py-0.5 pr-1 pl-1.5 text-[12.5px] leading-tight">
      <Icon className="size-3.5 shrink-0 text-sodium" aria-hidden="true" />
      <span className="truncate" lang={/[Ͱ-Ͽ]/.test(subject.name ?? "") ? "el" : undefined}>{subject.name ?? subject.id}</span>
      {subject.name && subject.name !== subject.id && <span className="shrink-0 font-condensed text-muted-foreground">{subject.id}</span>}
      {onRemove && (
        <button
          type="button"
          onClick={onRemove}
          aria-label={`Stop asking about ${subject.name ?? subject.id}`}
          className="ml-0.5 flex size-5 items-center justify-center rounded text-muted-foreground outline-none hover:bg-accent hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring/60"
        >
          <XIcon className="size-3.5" />
        </button>
      )}
    </span>
  )
}

export function Composer() {
  const busy = useChat((s) => s.busy)
  const subject = useComposer((s) => s.subject)
  const focusAt = useComposer((s) => s.focusAt)
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

  // "Ask about this" puts a chip here and the keyboard after it.
  useEffect(() => {
    if (focusAt) ref.current?.focus()
  }, [focusAt])

  const submit = () => {
    if (busy || !text.trim()) return
    send(text, { subject })
    setText("")
    useComposer.setState({ subject: null })
  }

  return (
    <form onSubmit={(e) => { e.preventDefault(); submit() }}>
      <InputGroup className="rounded-xl bg-night/70 dark:bg-night/70">
        {subject && (
          <InputGroupAddon align="block-start" className="pt-2.5 pb-0">
            <SubjectChip subject={subject} onRemove={() => { useComposer.setState({ subject: null }); ref.current?.focus() }} />
          </InputGroupAddon>
        )}
        <InputGroupTextarea
          ref={ref}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); submit() }
            if (e.key === "Backspace" && !text && subject) useComposer.setState({ subject: null })
          }}
          rows={1}
          placeholder={subject ? "Ask about it: how has it been today?" : "Ask about Nicosia"}
          aria-label={subject ? `Message about ${subject.name ?? subject.id}` : "Message"}
          className="max-h-40 min-h-12 px-3.5 pt-3 text-[15px]"
        />
        <InputGroupAddon align="block-end" className="justify-between pb-2">
          <InputGroupButton
            variant="ghost"
            size="xs"
            className="text-muted-foreground"
            onClick={() => openSearch()}
          >
            <SearchIcon />
            Search
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
