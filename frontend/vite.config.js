import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

// 资源前缀保持 /v3-admin-vite/，后端把 frontend/dist 挂在这个路径上。
// 页面路由在站点根路径（/influencers 等），不放进这个前缀。
export default defineConfig({
  base: "/v3-admin-vite/",
  plugins: [vue()],
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8000",
    },
  },
});
