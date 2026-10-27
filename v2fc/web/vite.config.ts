import fs from "node:fs";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// HTTPS (27/10/2026) : certificat de v2fc/certs, monté sur /certs par
// docker-compose.yml et créé par install/gen-cert.sh (auto-signé). Sans
// certificat, repli en HTTP avec un avertissement dans les logs.
const certDir = process.env.V2FC_TLS_DIR || "/certs";
const https = fs.existsSync(`${certDir}/v2fc.crt`) && fs.existsSync(`${certDir}/v2fc.key`)
  ? { cert: fs.readFileSync(`${certDir}/v2fc.crt`), key: fs.readFileSync(`${certDir}/v2fc.key`) }
  : undefined;
if (!https) console.warn(`[v2fc] pas de certificat dans ${certDir} : interface servie en HTTP`);

// En développement, le frontend appelle l'API sur le même hôte relatif
// (/api/...) ; le proxy évite les soucis de CORS pendant qu'on itère,
// même si l'API pose déjà ses propres en-têtes CORS (api/main.py).
export default defineConfig({
  plugins: [react()],
  server: {
    https,
    proxy: {
      "/api": {
        target: process.env.VITE_API_PROXY_TARGET || "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
});
