import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  // Preserve Host and Origin together so the backend's same-origin check also works in dev.
  server: {
    cors: false,
    strictPort: true,
    proxy: { '/api': { target: 'http://127.0.0.1:8765', changeOrigin: false } },
  },
});
