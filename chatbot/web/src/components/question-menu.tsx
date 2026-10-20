import { useEffect } from "react"

import {
  Command, CommandDialog, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList,
} from "@/components/ui/command"
import { send, useChat } from "@/lib/chat"
import { useLayout } from "@/lib/layout"
import { QUESTION_GROUPS } from "@/lib/questions"

// Every prepared question, a keystroke away (Ctrl K or Cmd K): the quickest way through a demo.
export function QuestionMenu() {
  const open = useLayout((s) => s.questionsOpen)
  const busy = useChat((s) => s.busy)
  const setOpen = (questionsOpen: boolean) => useLayout.setState({ questionsOpen })

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key.toLowerCase() === "k" && (e.ctrlKey || e.metaKey)) {
        e.preventDefault()
        useLayout.setState((s) => ({ questionsOpen: !s.questionsOpen }))
      }
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [])

  return (
    <CommandDialog open={open} onOpenChange={setOpen} title="Questions" description="Pick a question to ask">
      <Command>
        <CommandInput placeholder="Search the questions" />
        <CommandList className="max-h-[min(60vh,440px)]">
          <CommandEmpty>No question matches that.</CommandEmpty>
          {QUESTION_GROUPS.map((g) => (
            <CommandGroup key={g.title} heading={g.title}>
              {g.questions.map((q) => (
                <CommandItem
                  key={q}
                  value={q}
                  disabled={busy}
                  onSelect={() => { setOpen(false); useLayout.setState({ chatOpen: true }); send(q) }}
                >
                  <g.icon />
                  <span className="whitespace-normal">{q}</span>
                </CommandItem>
              ))}
            </CommandGroup>
          ))}
        </CommandList>
      </Command>
    </CommandDialog>
  )
}
