import { defineConfig } from 'vite'

// Builds src/files-action.js into js/files-action.js (one self-contained IIFE file).
export default defineConfig({
  build: {
    outDir: 'js',
    emptyOutDir: false,
    lib: { entry: 'src/files-action.js', name: 'MailVaultFilesAction', formats: ['iife'], fileName: () => 'files-action.js' },
  },
})
