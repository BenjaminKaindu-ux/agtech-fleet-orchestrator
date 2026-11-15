import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// The dev server proxies /api to Spring Boot, so the browser stays same-origin.
// Override the target with BACKEND_URL=http://host:port npm run dev
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': { target: process.env.BACKEND_URL ?? 'http://localhost:8080', changeOrigin: true },
    },
  },
  preview: {
    proxy: {
      '/api': { target: process.env.BACKEND_URL ?? 'http://localhost:8080', changeOrigin: true },
    },
  },
})
