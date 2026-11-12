import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The Python server that serves the built files in production; `npm run dev` forwards to it.
const BACKEND = "127.0.0.1:8765";

export default defineConfig({
  base: "./", // relative asset paths, so the build works from "/" or any sub-path
  plugins: [react()],
  server: {
    proxy: {
      "/api": `http://${BACKEND}`,
      "/ws": { target: `ws://${BACKEND}`, ws: true },
    },
  },
  build: {
    chunkSizeWarningLimit: 1000, // the lazily loaded orb chunk carries the 3D library
  },
});
