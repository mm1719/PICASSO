import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Source-showcase default: never connect to an existing inference service.
// The interface can be inspected without a checkpoint; API requests will fail
// clearly until an isolated backend is explicitly configured by the developer.
export default defineConfig({
  plugins: [react()],
  server: { host: '127.0.0.1', proxy: {} },
  preview: { host: '127.0.0.1' },
})
