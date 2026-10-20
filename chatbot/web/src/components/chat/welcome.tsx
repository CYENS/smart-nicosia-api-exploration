import { MOD_KEY } from "@/components/chat/composer"
import { Button } from "@/components/ui/button"
import { Kbd } from "@/components/ui/kbd"
import { send } from "@/lib/chat"
import { useLayout } from "@/lib/layout"
import { STARTERS } from "@/lib/questions"

export function Welcome() {
  return (
    <div className="flex flex-col gap-5 pt-1">
      <div className="flex flex-col gap-2">
        <h2 className="text-[21px] leading-tight font-semibold tracking-[-0.015em]">Ask about the city</h2>
        <p className="text-[14.5px] leading-relaxed text-muted-foreground">
          Street lights, parking, bins, air and traffic cameras as they are now; places and walking
          routes; the dangerous-building registers and every building in 3D. Answers come back as
          layers on the map and charts on the stage.
        </p>
      </div>
      <ul className="flex flex-col gap-1.5">
        {STARTERS.map(({ icon: Icon, group, question }) => (
          <li key={question}>
            <button
              type="button"
              onClick={() => send(question)}
              className="group flex w-full items-start gap-3 rounded-lg border border-line px-3 py-2.5 text-left transition-colors outline-none hover:border-sodium/40 hover:bg-accent/50 focus-visible:ring-3 focus-visible:ring-ring/50"
            >
              <Icon className="mt-0.5 size-4 shrink-0 text-muted-foreground transition-colors group-hover:text-sodium" />
              <span className="flex flex-col gap-0.5">
                <span className="font-condensed text-[12px] text-muted-foreground">{group}</span>
                <span className="text-[14px] leading-snug">{question}</span>
              </span>
            </button>
          </li>
        ))}
      </ul>
      <Button variant="ghost" size="sm" className="self-start text-muted-foreground" onClick={() => useLayout.setState({ questionsOpen: true })}>
        All questions
        <Kbd className="max-md:hidden">{MOD_KEY} K</Kbd>
      </Button>
    </div>
  )
}
