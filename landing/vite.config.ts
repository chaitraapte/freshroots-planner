import path from 'node:path'
import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  // The built site is served straight from the repo's /public folder (no build step on Vercel).
  // emptyOutDir is off because /public/dashboard lives there too; the build script clears only the landing files.
  build: { outDir: '../public', emptyOutDir: false },
  resolve: {
    alias: { '@': path.resolve(import.meta.dirname, './src') },
  },
})
