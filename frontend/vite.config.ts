import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Backend runs on 127.0.0.1:8000 (see backend/.env.example). The frontend
// talks to it directly via VITE_API_URL (see src/services/api.ts) rather
// than a dev-server proxy, so it works the same in `vite dev` and a static
// build served separately.
export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
  },
});
