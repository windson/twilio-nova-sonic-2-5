import path from "path";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The compiled dashboard is served by the SAME FastAPI app:
//   - GET /            -> web/index.html
//   - /static/*        -> StaticFiles mounted at web/
// So we build INTO ../web with `base: "/static/"` which makes the emitted
// index.html reference its hashed JS/CSS at /static/assets/*. The REST API is
// called at absolute /api/* paths from the app, unaffected by `base`.
export default defineConfig({
  base: "/static/",
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  build: {
    outDir: path.resolve(__dirname, "../web"),
    emptyOutDir: true,
    assetsDir: "assets",
  },
});
