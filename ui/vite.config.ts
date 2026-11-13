import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

// `npm run dev` forwards /api and /ws to the Python server that serves the built files in production.
// JARVIS_BACKEND=127.0.0.1:<port> points it at another one (the installed Jarvis keeps port 8765).
export default defineConfig(({ mode }) => {
  const backend = loadEnv(mode, ".", "JARVIS_").JARVIS_BACKEND ?? "127.0.0.1:8765";
  return {
    base: "./", // relative asset paths, so the build works from "/" or any sub-path
    plugins: [react()],
    server: {
      proxy: {
        "/api": `http://${backend}`,
        "/ws": { target: `ws://${backend}`, ws: true },
      },
    },
    build: {
      chunkSizeWarningLimit: 1000, // the lazily loaded orb chunk carries the 3D library
    },
  };
});
