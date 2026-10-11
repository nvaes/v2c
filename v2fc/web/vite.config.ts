import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// En développement, le frontend appelle l'API sur le même hôte relatif
// (/api/...) ; le proxy évite les soucis de CORS pendant qu'on itère,
// même si l'API pose déjà ses propres en-têtes CORS (api/main.py).
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": {
        target: process.env.VITE_API_PROXY_TARGET || "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
});
