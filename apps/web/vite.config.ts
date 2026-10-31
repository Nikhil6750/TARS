import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import { VitePWA } from 'vite-plugin-pwa';
import path from 'path';

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [
    react(),
    tailwindcss(),
    VitePWA({
      registerType: 'autoUpdate',
      // Registered manually in main.tsx and never inside the Tauri desktop app: a precaching
      // service worker served a stale UI bundle after rebuilds.
      injectRegister: false,
      includeAssets: ['favicon.ico', 'robots.txt', 'apple-touch-icon.png', 'icons/*.png', 'icons/*.svg'],
      manifest: {
        name: 'TARS Trading Companion',
        short_name: 'TARS',
        description: 'Intelligent Real-time Trading Companion for Setups, Risk & Conversational Intelligence',
        theme_color: '#080c14',
        background_color: '#04060a',
        display: 'standalone',
        orientation: 'portrait',
        scope: '/',
        start_url: '/',
        icons: [
          {
            src: '/icons/icon-192.svg',
            sizes: '192x192',
            type: 'image/svg+xml',
            purpose: 'any maskable'
          },
          {
            src: '/icons/icon-512.svg',
            sizes: '512x512',
            type: 'image/svg+xml',
            purpose: 'any maskable'
          }
        ]
      },
      workbox: {
        globPatterns: ['**/*.{js,css,html,ico,png,svg,woff2}']
      }
    })
  ],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src')
    }
  },
  build: {
    rollupOptions: {
      input: {
        // The existing companion app, unchanged.
        main: path.resolve(__dirname, 'index.html'),
        // Market X-Ray dashboard, served at /market.html so it shares the
        // dev server and build pipeline without touching the companion shell.
        market: path.resolve(__dirname, 'market.html')
      }
    }
  },
  server: {
    port: 5173,
    host: '0.0.0.0'
  }
});
