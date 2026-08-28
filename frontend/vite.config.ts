import { defineConfig } from "vitest/config";
import { svelte } from "@sveltejs/vite-plugin-svelte";
import path from "path";

// The bundle is served by Orpheus's own Datasette plugin, not by Datasette's
// static-plugin mount: a plugin loaded with --plugins-dir gets `static_path =
// None`, so /-/static-plugins/ never resolves for it. The plugin serves
// /-/orpheus/static/ from the directory below instead, which keeps the
// single-file --plugins-dir deployment the docs and the compose file describe.
export default defineConfig({
  plugins: [svelte()],
  base: "/-/orpheus/static/",
  build: {
    target: "esnext",
    // Beside the plugin that serves it. Not committed -- `npm run build`
    // produces it, and the page falls back to the no-build map when it is
    // absent, so a source checkout with no toolchain still draws a graph.
    outDir: path.resolve(__dirname, "../plugins/static"),
    assetsDir: "gen",
    emptyOutDir: true,
    manifest: "manifest.json",
    rollupOptions: {
      input: { map: path.resolve(__dirname, "src/main.ts") },
    },
  },
  server: {
    port: 5173,
    strictPort: true,
    cors: true,
    origin: "http://localhost:5173",
  },
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.ts"],
  },
});
