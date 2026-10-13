import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// base "./" so the build works at any URL path; in dev, /api goes to the backtest service.
export default defineConfig({
  base: "./",
  plugins: [react()],
  server: { host: "127.0.0.1", port: 5173, proxy: { "/api": "http://127.0.0.1:8000" } },
});
