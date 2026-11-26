/* Marks on the city clock's track from other features: incidents where they began, today. Each
   set has a name; the track draws every set's marks as small flags along its top edge, its
   hover label names them, and [ and ] stop at them. */
import { create } from "zustand"

export type TrackMark = {
  t: number // the moment (ms)
  color: string
  label: string // for the hover label: "INC-12: Probable power cut near Evagorou"
  id?: string
}

export const useTrackMarks = create<{ sets: Record<string, TrackMark[]> }>(() => ({ sets: {} }))

export function setTrackMarks(set: string, marks: TrackMark[]) {
  useTrackMarks.setState((s) => ({ sets: { ...s.sets, [set]: marks } }))
}

export function allTrackMarks(sets = useTrackMarks.getState().sets) {
  return Object.values(sets).flat()
}
