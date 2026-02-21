import { execSync } from 'node:child_process';
import tailwindcss from '@tailwindcss/vite';
import react from '@vitejs/plugin-react';
import { defineConfig, type Plugin } from 'vite';

// Derive version from nearest git tag: e.g. "v1.0.0" on the tag itself,
// "v1.0.0-3-gabcdef" for commits after the tag.
// Falls back to "v0.0.0" if git is unavailable (e.g. shallow clone).
function gitVersion(): string {
  try {
    return execSync('git describe --tags --always', { encoding: 'utf8' }).trim();
  } catch {
    return 'v0.0.0';
  }
}

const APP_VERSION = gitVersion();

/** Emits dist/version.json so the version poller can detect new deployments. */
function versionJsonPlugin(): Plugin {
  return {
    name: 'version-json',
    apply: 'build',
    generateBundle() {
      this.emitFile({
        type: 'asset',
        fileName: 'version.json',
        source: JSON.stringify({ version: APP_VERSION }),
      });
    },
  };
}

export default defineConfig({
  plugins: [react(), tailwindcss(), versionJsonPlugin()],
  worker: { format: 'es' },
  define: {
    __APP_VERSION__: JSON.stringify(APP_VERSION),
    __BUILD_DATE__: JSON.stringify(new Date().toISOString().slice(0, 16).replace('T', ' ')),
  },
  server: {
    proxy: {
      '/ws/iq': { target: 'ws://localhost:1236', ws: true },
    },
  },
});
