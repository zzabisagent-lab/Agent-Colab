import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// Development proxy: the console talks to the Agent-Colab server on :8080 with cookies.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8080',
      '/setup': 'http://127.0.0.1:8080',
    },
  },
})
