import tailwindcss from '@tailwindcss/vite';
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

export default defineConfig({
  plugins: [react(), tailwindcss()],
  worker: { format: 'es' },
  server: {
    proxy: {
      '/ws/cw': { target: 'ws://localhost:8765', ws: true },
      '/ws/sstv': { target: 'ws://localhost:8766', ws: true },
    },
  },
});
