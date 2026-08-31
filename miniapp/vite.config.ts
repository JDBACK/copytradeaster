import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// The Mini App is served by the bot process under /miniapp/ (aiohttp static
// mount), so every asset path must be rooted at that subpath.  The dev server
// proxies API calls to the local aiohttp transport.
// https://vite.dev/config/
export default defineConfig({
  base: '/miniapp/',
  plugins: [react()],
  server: {
    proxy: {
      '/miniapp/api': {
        target: process.env.VITE_API_TARGET ?? 'http://127.0.0.1:8080',
        changeOrigin: true,
      },
    },
  },
  build: {
    target: 'es2020',
  },
})
