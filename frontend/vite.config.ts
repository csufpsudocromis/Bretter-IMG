/// <reference types="node" />
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { existsSync, readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const rootDir = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const certPath = resolve(rootDir, '.certs', 'bretter-img.crt')
const keyPath = resolve(rootDir, '.certs', 'bretter-img.key')
const https = existsSync(certPath) && existsSync(keyPath)
  ? {
      cert: readFileSync(certPath),
      key: readFileSync(keyPath),
    }
  : undefined

export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,
    https,
    proxy: {
      '/api': {
        target: 'https://localhost:8000',
        changeOrigin: true,
        secure: false,
      },
    },
  },
})
