import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // The proxy keeps the dev build free of hard-coded hosts and avoids CORS
    // entirely: the browser only ever talks to the Vite origin.
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
  test: {
    environment: 'jsdom',
    globals: false,
    setupFiles: './src/test/setup.js',
    css: false,
    include: ['src/**/*.test.{js,jsx}'],
  },
})
