import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Never forward an unmocked browser-test request to live Production on port 8000.
export default defineConfig({ plugins: [react()], server: { proxy: {} } })
