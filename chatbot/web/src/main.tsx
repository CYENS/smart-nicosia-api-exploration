import { StrictMode, Suspense, lazy } from "react"
import { createRoot } from "react-dom/client"

import "./index.css"
import App from "./App.tsx"

// /admin is the collectors' admin page; it loads on its own and never starts the map.
const CollectorsPage = lazy(() =>
  import("@/components/admin/collectors-page").then((m) => ({ default: m.CollectorsPage })))
const admin = /^\/admin\/?$/.test(location.pathname)

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    {admin ? (
      <Suspense fallback={<div className="h-full bg-night" />}>
        <CollectorsPage />
      </Suspense>
    ) : (
      <App />
    )}
  </StrictMode>
)
