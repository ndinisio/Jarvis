import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The backend serves the built interface from ../frontend/dist in production.
// In development, Vite proxies API and WebSocket traffic to the Python server.
export default defineConfig({
  plugins: [react()],
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
