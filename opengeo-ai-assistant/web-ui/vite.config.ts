// Copyright (c) Microsoft Corporation.
// Licensed under the MIT license.

import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig(({ command, mode }) => {
  // Load env variables from .env files
  const env = loadEnv(mode, process.cwd(), '');

  // For local development proxy
  // Set LOCAL_BACKEND_URL in .env.local to point to your deployed backend
  const LOCAL_BACKEND = env.LOCAL_BACKEND_URL || 'http://localhost:8000';

  const isDev = command === 'serve';
  const isProd = mode === 'production';

  return {
    plugins: [react()],
    base: isProd ? './' : '/',
    server: isDev ? {
      port: 5173,
      host: true, // Allow external connections for debugging
      cors: true, // Enable CORS for debugging
      open: false, // Don't auto-open browser during debugging
      proxy: {
        '/health': {
          target: LOCAL_BACKEND,
          changeOrigin: true,
          configure: (proxy, _options) => {
            proxy.on('error', (_err, _req, _res) => {});
            proxy.on('proxyReq', (_proxyReq, _req, _res) => {});
          }
        },
        '/debug': LOCAL_BACKEND,
        '/maps-config': LOCAL_BACKEND,
        '/collections': LOCAL_BACKEND,
        '/unified-chat': LOCAL_BACKEND,
        '/chat': LOCAL_BACKEND,
        '/enhanced-chat': LOCAL_BACKEND,
        '/api/v2/chat': LOCAL_BACKEND,
        '/query': LOCAL_BACKEND,
        '/api/chat': LOCAL_BACKEND,
        '/api/health': {
          target: LOCAL_BACKEND,
          changeOrigin: true,
          configure: (proxy, _options) => {
            proxy.on('error', (_err, _req, _res) => {});
          }
        },
        '/api/query': {
          target: LOCAL_BACKEND,
          changeOrigin: true,
          configure: (proxy, _options) => {
            proxy.on('error', (_err, _req, _res) => {});
            proxy.on('proxyReq', (_proxyReq, _req, _res) => {});
          }
        },
        '/stac-search': {
          target: LOCAL_BACKEND,
          changeOrigin: true,
          configure: (proxy, _options) => {
            proxy.on('error', (_err, _req, _res) => {});
          }
        },
        '/api/stac-search': {
          target: LOCAL_BACKEND,
          changeOrigin: true,
          configure: (proxy, _options) => {
            proxy.on('error', (_err, _req, _res) => {});
            proxy.on('proxyReq', (_proxyReq, _req, _res) => {});
          }
        },
        // '/mcp-query': LOCAL_BACKEND, // DISABLED - MCP server functionality
        // '/mcp-status': LOCAL_BACKEND, // DISABLED - MCP server functionality
        '/intelligent-route': LOCAL_BACKEND,
        '/veda': LOCAL_BACKEND,
        '/search': LOCAL_BACKEND,
        '/api/config': {
          target: LOCAL_BACKEND,
          changeOrigin: true,
          configure: (proxy, _options) => {
            proxy.on('proxyReq', (_proxyReq, _req, _res) => {});
          }
        }
      }
    } : undefined,
    build: {
      outDir: 'dist',
      sourcemap: !isProd, // Disable source maps in production
      minify: isProd ? 'esbuild' : false,
      rollupOptions: {
        output: {
          manualChunks: isProd ? {
            vendor: ['react', 'react-dom'],
            query: ['@tanstack/react-query'],
            utils: ['axios']
          } : undefined
        }
      }
    },
    esbuild: isProd ? {
      drop: ['console', 'debugger'],
    } : undefined,
    define: {
      // Environment-specific configurations
      __DEV__: JSON.stringify(isDev),
      'process.env.DEBUG_MODE': JSON.stringify(isDev),
      // VITE_API_BASE_URL is set by the deployment workflow at build time
      // For local development, use localhost backend
      'import.meta.env.VITE_API_BASE_URL': JSON.stringify(
        isDev ? (env.BACKEND_URL || 'http://localhost:8000') : process.env.VITE_API_BASE_URL || ''
      ),
      'import.meta.env.BACKEND_URL': JSON.stringify(env.BACKEND_URL || 'http://localhost:8000')
    }
  };
});
