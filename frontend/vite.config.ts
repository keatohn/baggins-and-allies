import path from 'node:path'
import fs from 'node:fs'
import { defineConfig, type Plugin } from 'vite'
import react from '@vitejs/plugin-react'

/** Expose filenames from a public/ folder (those files are not in import.meta.glob). */
function publicFilenamesPlugin(opts: {
  name: string
  virtualId: string
  relDir: string
  ext: string
}): Plugin {
  const dir = path.resolve(__dirname, opts.relDir)
  const resolvedId = `\0${opts.virtualId}`
  const ext = opts.ext.toLowerCase()

  const list = (): string[] => {
    if (!fs.existsSync(dir)) return []
    return fs
      .readdirSync(dir)
      .filter((f) => f.toLowerCase().endsWith(ext))
      .sort((a, b) => a.localeCompare(b, undefined, { sensitivity: 'base' }))
  }

  return {
    name: opts.name,
    resolveId(id) {
      if (id === opts.virtualId) return resolvedId
    },
    load(id) {
      if (id === resolvedId) {
        return `export default ${JSON.stringify(list())}`
      }
    },
    configureServer(server) {
      if (fs.existsSync(dir)) server.watcher.add(dir)
      const invalidate = (file: string) => {
        if (path.dirname(file) !== dir || !file.toLowerCase().endsWith(ext)) return
        const mod = server.moduleGraph.getModuleById(resolvedId)
        if (mod) void server.reloadModule(mod)
      }
      server.watcher.on('add', invalidate)
      server.watcher.on('unlink', invalidate)
    },
  }
}

// https://vite.dev/config/
export default defineConfig({
  appType: 'spa',
  plugins: [
    publicFilenamesPlugin({
      name: 'turn-music-m4a',
      virtualId: 'virtual:turn-music-m4a',
      relDir: 'public/assets/audio/turn',
      ext: '.m4a',
    }),
    publicFilenamesPlugin({
      name: 'menu-music-m4a',
      virtualId: 'virtual:menu-music-m4a',
      relDir: 'public/assets/audio/menu',
      ext: '.m4a',
    }),
    publicFilenamesPlugin({
      name: 'lobby-music-m4a',
      virtualId: 'virtual:lobby-music-m4a',
      relDir: 'public/assets/audio/lobby',
      ext: '.m4a',
    }),
    publicFilenamesPlugin({
      name: 'sfx-m4a',
      virtualId: 'virtual:sfx-m4a',
      relDir: 'public/assets/audio/sfx',
      ext: '.m4a',
    }),
    publicFilenamesPlugin({
      name: 'unit-icon-png',
      virtualId: 'virtual:unit-icon-png',
      relDir: 'public/assets/units',
      ext: '.png',
    }),
    react(),
    // GitHub Pages has no server rewrite for /login etc.; unknown paths get 404.html (same shell as index.html).
    {
      name: 'github-pages-spa-404',
      apply: 'build',
      closeBundle() {
        const outDir = path.resolve(__dirname, 'dist')
        const indexHtml = path.join(outDir, 'index.html')
        const notFoundHtml = path.join(outDir, '404.html')
        if (fs.existsSync(indexHtml)) fs.copyFileSync(indexHtml, notFoundHtml)
      },
    },
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
    // Allow same-network devices (phone, tablet) to open http://<this-machine-LAN-IP>:5173
    host: true,
    // Single /api proxy so path /games (SPA route) is not forwarded; only /api/* hits the backend (with header).
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
    },
  },
})
