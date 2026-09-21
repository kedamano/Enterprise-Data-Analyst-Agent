import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import path from "path";

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(import.meta.dirname, "./src"),
    },
  },
  server: {
    port: 5173,
    // 排除产物/测试报告目录：Windows 下 Node 扫描这些目录会触发 scandir UNKNOWN(-4094) 崩溃
    watch: {
      ignored: [
        "**/dist/**",
        "**/playwright-report/**",
        "**/test-results/**",
        "**/shots/**",
        "**/.git/**",
        "**/node_modules/**",
      ],
    },
    proxy: {
      // 开发期把后端 API 代理到 FastAPI 服务
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "dist",
  },
});
