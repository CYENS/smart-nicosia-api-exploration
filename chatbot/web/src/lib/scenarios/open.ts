/* Open a stored run in the Scenario lab: from a link (#/s/<run>), the chat's open_scenario, or
   Ctrl K. Kept apart from the lab's components so the chat and the links can call it. */
import { openWorkspace } from "@/lib/registries/workspaces"
import { openRun, useLab } from "@/lib/scenarios/store"

export function openScenarioRun(id: string, compare: string | null = null) {
  openWorkspace("scenarios")
  const { run, compare: other } = useLab.getState()
  if (run?.id === id && (other?.id ?? null) === compare) return
  void openRun(id, compare)
}
