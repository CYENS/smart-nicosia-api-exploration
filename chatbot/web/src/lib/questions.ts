import { Building2, Clock3, Footprints, Radio, type LucideIcon } from "lucide-react"

// Questions that show the city off, grouped by what the answer brings back.
export type QuestionGroup = { title: string; icon: LucideIcon; questions: string[] }

export const QUESTION_GROUPS: QuestionGroup[] = [
  {
    title: "Live now",
    icon: Radio,
    questions: [
      "Which street lights are on right now?",
      "Where can I park near Ledras Street?",
      "Which bins are full? Show them in 3D",
      "How are air quality and noise across the city?",
      "Is it a good afternoon for a walk?",
    ],
  },
  {
    title: "Replays",
    icon: Clock3,
    questions: [
      "Replay the street lights over the last 24 hours as a time-lapse, with a chart of lit lamps",
      "Show me where people walked the most yesterday",
      "Draw a radial 24-hour clock of how parking occupancy changed since yesterday",
    ],
  },
  {
    title: "The 3D city",
    icon: Building2,
    questions: [
      "Show the dangerous buildings in the old town in 3D",
      "Which communities have the most dangerous buildings? Chart it and map it",
      "How tall are the buildings around Eleftheria Square? Show the skyline in 3D",
      "Are there dangerous high-rise buildings in Strovolos?",
    ],
  },
  {
    title: "On foot",
    icon: Footprints,
    questions: [
      "Plan a 3-hour walking tour of museums in the old town, starting at Eleftheria Square",
      "What can I reach on foot in 10 and 20 minutes from Eleftheria Square?",
    ],
  },
]

// One question per group opens the empty conversation: the ones that land best in a demo.
const STARTER_PICKS = [2, 0, 0, 1]
export const STARTERS = QUESTION_GROUPS.map((g, i) => ({ icon: g.icon, group: g.title, question: g.questions[STARTER_PICKS[i]] }))
