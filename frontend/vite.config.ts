import { readFileSync } from 'node:fs'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The ledger version (the number in each commit's title) is the one the status bar shows. The
// interface carries the value it was built from, so it can tell when it is older than the backend:
// backend/jarvis/ledger.py is the single place it is set, in every commit.
const ledger =
  /^LEDGER_VERSION = "(v\d+\.\d+)"$/m.exec(
    readFileSync(new URL('../backend/jarvis/ledger.py', import.meta.url), 'utf8'),
  )?.[1] ?? 'unknown'

// The backend serves the built interface from ../frontend/dist in production.
// In development, Vite proxies API and WebSocket traffic to the Python server.
export default defineConfig({
  plugins: [react()],
  define: { __UI_LEDGER__: JSON.stringify(ledger) },
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8765', changeOrigin: true },
      '/ws': { target: 'ws://127.0.0.1:8765', ws: true },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    chunkSizeWarningLimit: 900,
  },
})
