/* The counting cameras walkthrough plugs into the twin here: its workspace (Alt 6), a command in
   Ctrl K, and the /#review link the collectors' admin page opens it with (/#review/<analytics
   id> starts at that camera). */
import { CctvIcon } from "lucide-react"

import { CameraReview } from "@/components/review/camera-review"
import { matches, registerSearchProvider, type SearchItem } from "@/lib/registries/search"
import { openWorkspace, registerWorkspace } from "@/lib/registries/workspaces"
import { showCamera } from "@/lib/review/store"

registerWorkspace({
  id: "cameras", label: "Cameras",
  description: "Counting cameras: where each one is, what its lines count, and how they add up",
  icon: CctvIcon, order: 50, key: "6", Side: CameraReview,
})

registerSearchProvider({
  id: "review-commands",
  heading: "Commands",
  order: 22,
  search: (q) => {
    const items: (SearchItem & { words: string })[] = [
      { key: "cmd:review-cameras", icon: CctvIcon, group: "Commands", title: "Review the counting cameras", opens: true,
        detail: "Go to each camera, see what its lines count, confirm how they add up",
        run: () => openWorkspace("cameras"), words: "cameras counting lines traffic review confirm walkthrough" },
    ]
    return q ? items.filter((c) => matches(q, c.title, c.detail, c.words)) : []
  },
})

// /#review or /#review/<analytics id>: open the walkthrough (at that camera), on load and when
// the address changes to it later.
function followReviewLink() {
  const link = /^#\/?review(?:\/(\d+))?$/.exec(location.hash)
  if (!link) return
  if (link[1]) showCamera(Number(link[1]))
  history.replaceState(null, "", location.pathname)
  window.setTimeout(() => openWorkspace("cameras"), 0)
}
followReviewLink()
window.addEventListener("hashchange", followReviewLink)
