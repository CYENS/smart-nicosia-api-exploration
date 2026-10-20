import { resolve } from "node:path"
import tailwindcss from "@tailwindcss/vite"
import react from "@vitejs/plugin-react"
import { defineConfig } from "vite"

// FastAPI serves the built page at / and its assets under /static (chatbot/static is committed,
// so `uv run chatbot` works without Node). In development, Vite proxies the API to a running
// chatbot: CHATBOT_URL, or the usual http://127.0.0.1:8000.
const backend = process.env.CHATBOT_URL ?? "http://127.0.0.1:8000"

export default defineConfig(({ command }) => ({
  base: command === "build" ? "/static/" : "/",
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": resolve(import.meta.dirname, "./src"),
    },
  },
  build: {
    outDir: resolve(import.meta.dirname, "../static"),
    emptyOutDir: true,
    chunkSizeWarningLimit: 2500, // MapLibre and Vega are big on their own
  },
  server: {
    port: 5180,
    proxy: { "/api": { target: backend, changeOrigin: true } },
  },
}))
