import path from 'node:path'
import fs from 'node:fs'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  appType: 'spa',
  plugins: [
    react(),
    // SPA fallback: for non-file routes, serve index.html ourselves so we never 500
    {
      name: 'spa-fallback',
      configureServer(server) {
        return () => {
          server.middlewares.use((req, res, next) => {
            const url = (req.url ?? '').split('?')[0]
            if (url.startsWith('/api') || /\.[a-zA-Z0-9]+$/.test(url)) return next()
            const indexPath = path.join(server.config.root, 'index.html')
            fs.readFile(indexPath, (err, data) => {
              if (err) {
                next(err)
                return
              }
              res.setHeader('Content-Type', 'text/html')
              res.statusCode = 200
              res.end(data)
            })
          })
        }
      },
    },
  ],
  server: {
    port: 5173,
    strictPort: true, // Fail if 5173 is in use so you always use the same URL
  },
})
